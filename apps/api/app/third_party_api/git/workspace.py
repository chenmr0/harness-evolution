"""Isolated git workspaces for candidate creation and publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from app.db.models.evolution import EvolutionRunRecord
from app.utils.redaction import sanitize_message

GitStage = Literal[
    "workspace",
    "clone",
    "reference",
    "subdirectory",
    "patch",
    "commit",
    "push",
]


class GitOperationError(RuntimeError):
    """A sanitized git failure carrying a stage and retry classification."""

    def __init__(
        self,
        message: str,
        *,
        stage: GitStage,
        retryable: bool = False,
    ) -> None:
        """Store safe diagnostics without retaining raw process output."""
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GitProcessResult:
    """Captured output from one argument-array git process."""

    returncode: int
    stdout: str
    stderr: str


type GitRunner = Callable[[tuple[str, ...], Path | None], Awaitable[GitProcessResult]]


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    """Paths and immutable refs created for one evolution run."""

    repo_path: Path
    path: Path
    ref: str
    base_commit: str
    branch: str


@dataclass(frozen=True, slots=True)
class Patch:
    """One deterministic patch applied inside an isolated workspace."""

    file: str
    lines: tuple[str, ...] = ()
    changes: dict[str, str | None] | None = None

    @property
    def sha256(self) -> str:
        """Return the stable digest recorded in candidate trailers."""
        if self.changes is not None:
            return hashlib.sha256(json.dumps(self.changes, sort_keys=True).encode()).hexdigest()
        body = "\n".join(self.lines)
        return hashlib.sha256(
            f"{self.file}\0{body}".encode(),
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    """A committed candidate branch ready to push."""

    workspace_path: Path
    run_branch: str
    local_branch: str
    ref: str
    commit: str
    message: str
    run_id: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class CandidateAcceptance:
    """Published accepted commit plus non-blocking candidate cleanup warnings."""

    commit: str
    message: str
    cleanup_error: str | None = None


class GitWorkspaceManager:
    """Own repository caches, isolated workspaces, and candidate refs."""

    supports_candidate_lifecycle = True

    @property
    def push_enabled(self) -> bool:
        """Return whether composition permits remote Git mutations."""
        return self._push_enabled

    _RUN_BRANCH = re.compile(r"^evo/\d{8}-.+$")
    _TRANSIENT_MARKERS = (
        "could not resolve host",
        "connection timed out",
        "connection reset",
        "failed to connect",
        "operation timed out",
        "remote end hung up",
        "temporarily unavailable",
        "timed out",
    )

    def __init__(
        self,
        *,
        repos_root: Path,
        workspaces_root: Path,
        runner: GitRunner | None = None,
        timeout_seconds: float = 60.0,
        push_enabled: bool = True,
    ) -> None:
        """Bind roots and the sanitized process seam."""
        self.repos_root = repos_root
        self.workspaces_root = workspaces_root
        self._runner = runner or _run_process
        self._timeout_seconds = timeout_seconds
        self._push_enabled = push_enabled

    async def prepare(self, run: EvolutionRunRecord) -> PreparedWorkspace:
        """Clone a cache and create the uniquely named run branch."""
        self.repos_root.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        repo_path = self.repos_root / _safe_component(run.target_config_id or run.id)
        workspace_path = self.workspaces_root / _safe_component(run.id)
        ref = run.target_version
        branch = f"evo/{run.created_at:%Y%m%d}-{run.id}"

        await self._ensure_mirror(repo_path, run.repo_url)

        resolved_ref = await self._git_checked(
            repo_path,
            ("git", "rev-parse", "--verify", f"{ref}^{{commit}}"),
            stage="reference",
            failure="target reference is invalid",
        )
        base_commit = resolved_ref.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
            raise GitOperationError(
                "target reference did not resolve to a commit",
                stage="reference",
            )

        sub_dir = _normalize_sub_dir(run.sub_dir)
        if sub_dir != ".":
            object_type = await self._git_checked(
                repo_path,
                ("git", "cat-file", "-t", f"{base_commit}:{sub_dir}"),
                stage="subdirectory",
                failure="target subdirectory is missing",
            )
            if object_type.stdout.strip() != "tree":
                raise GitOperationError(
                    "target subdirectory is not a directory",
                    stage="subdirectory",
                )

        if workspace_path.exists():
            await self._reuse_or_reject(workspace_path, branch, run.repo_url)
        else:
            try:
                await self._git_checked(
                    None,
                    ("git", "clone", "--no-checkout", "--", str(repo_path), str(workspace_path)),
                    stage="workspace",
                    failure="workspace clone failed",
                )
            except Exception:
                shutil.rmtree(workspace_path, ignore_errors=True)
                raise
            await self._git_checked(
                workspace_path,
                ("git", "remote", "set-url", run.remote_name, run.repo_url),
                stage="workspace",
                failure="workspace remote setup failed",
            )
            local_branch = await self._optional_ref(
                workspace_path,
                ("git", "rev-parse", "--verify", f"refs/heads/{branch}"),
            )
            if local_branch is None:
                remote_branch = await self.remote_ref_commit(
                    workspace_path,
                    run.remote_name,
                    branch,
                )
                if remote_branch is None:
                    await self._git_checked(
                        workspace_path,
                        ("git", "switch", "-c", branch, base_commit),
                        stage="workspace",
                        failure="run branch creation failed",
                    )
                else:
                    await self._git_checked(
                        workspace_path,
                        ("git", "switch", "-c", branch, "--track", f"{run.remote_name}/{branch}"),
                        stage="workspace",
                        failure="existing run branch could not be resumed",
                    )
            else:
                await self._git_checked(
                    workspace_path,
                    ("git", "switch", branch),
                    stage="workspace",
                    failure="existing local run branch could not be resumed",
                )

        await self.validate_for_push(
            PreparedWorkspace(
                repo_path=repo_path,
                path=workspace_path,
                ref=ref,
                base_commit=base_commit,
                branch=branch,
            ),
            run,
        )
        if self._push_enabled:
            await self._push_run_branch(workspace_path, branch)
        return PreparedWorkspace(
            repo_path=repo_path,
            path=workspace_path,
            ref=ref,
            base_commit=base_commit,
            branch=branch,
        )

    async def publish_skill_snapshot(
        self,
        workspace_path: Path,
        *,
        branch: str,
        commit: str,
    ) -> str:
        """Publish a stable evaluation branch without rewriting an existing snapshot."""
        if not re.fullmatch(r"evo-snapshot/[A-Za-z0-9._/-]+", branch):
            raise GitOperationError("invalid snapshot branch", stage="push")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise GitOperationError("invalid snapshot commit", stage="push")
        if not self._push_enabled:
            raise GitOperationError("Skill evaluation requires branch publication", stage="push")
        remote = await self.remote_ref_commit(workspace_path, "origin", branch)
        if remote is not None and remote != commit:
            raise GitOperationError("immutable snapshot branch conflict", stage="push")
        if remote is None:
            await self._push_checked(
                workspace_path,
                (
                    "git",
                    "push",
                    f"--force-with-lease=refs/heads/{branch}:",
                    "origin",
                    f"{commit}:refs/heads/{branch}",
                ),
            )
            if await self.remote_ref_commit(workspace_path, "origin", branch) != commit:
                raise GitOperationError(
                    "snapshot branch publication was not verified", stage="push"
                )
        return branch

    async def create_candidate(
        self,
        prepared: PreparedWorkspace,
        *,
        run_branch: str,
        candidate_ref: str,
        patch: Patch,
        message: str,
        expected_run_id: str | None = None,
    ) -> CandidateWorkspace:
        """Create one candidate commit from the clean, current run branch."""
        await self._require_clean(prepared.path)
        await self._git_checked(
            prepared.path,
            ("git", "switch", run_branch),
            stage="workspace",
            failure="run branch could not be selected",
        )
        if not (
            re.fullmatch(rf"{re.escape(run_branch)}/candidate/r[1-9]\d*", candidate_ref)
            or re.fullmatch(
                rf"evo-candidate/{re.escape(run_branch.removeprefix('evo/'))}/r[1-9]\d*",
                candidate_ref,
            )
        ):
            raise GitOperationError("candidate ref does not match the run", stage="workspace")
        run_id = expected_run_id or _required_trailer(message, "Harness-Run-Id")
        if not _has_trailer(message, "Harness-Run-Id", run_id):
            raise GitOperationError(
                "candidate commit is missing the Harness-Run-Id trailer",
                stage="workspace",
            )
        await self._require_ancestry(prepared.path, prepared.base_commit)
        local_branch = candidate_local_branch(candidate_ref)
        existing_candidate = await self._optional_ref(
            prepared.path,
            ("git", "rev-parse", "--verify", f"refs/heads/{local_branch}"),
        )
        if existing_candidate is not None:
            await self._git_checked(
                prepared.path,
                ("git", "branch", "-D", local_branch),
                stage="workspace",
                failure="stale local candidate branch could not be removed",
            )
        await self._git_checked(
            prepared.path,
            ("git", "switch", "-c", local_branch, run_branch),
            stage="workspace",
            failure="candidate branch creation failed",
        )
        self._write_patch(prepared.path, patch)
        await self._git_checked(
            prepared.path,
            ("git", "add", "--", *(patch.changes or {patch.file: None})),
            stage="patch",
            failure="candidate patch could not be staged",
        )
        await self._git_checked(
            prepared.path,
            (
                "git",
                "-c",
                "user.name=Harness Evolution",
                "-c",
                "user.email=harness-evolution@localhost",
                "commit",
                "-m",
                message,
            ),
            stage="commit",
            failure="candidate commit failed",
        )
        commit = (
            await self._git_checked(
                prepared.path,
                ("git", "rev-parse", "HEAD"),
                stage="commit",
                failure="candidate commit could not be resolved",
            )
        ).stdout.strip()
        return CandidateWorkspace(
            workspace_path=prepared.path,
            run_branch=run_branch,
            local_branch=local_branch,
            ref=candidate_ref,
            commit=commit,
            message=message,
            run_id=run_id,
            base_commit=prepared.base_commit,
        )

    async def push_candidate(
        self,
        candidate: CandidateWorkspace,
        *,
        expected_remote_commit: str | None,
    ) -> None:
        """Push an initial candidate or replace its own prior commit with a lease."""
        await self._validate_candidate(candidate)
        if not self._push_enabled:
            return
        remote = await self.remote_tag_commit(
            candidate.workspace_path,
            "origin",
            candidate.ref,
        )
        if remote == candidate.commit:
            return
        if expected_remote_commit is None:
            if remote is not None and remote != candidate.commit:
                raise GitOperationError(
                    "candidate reference already exists",
                    stage="push",
                )
            args: tuple[str, ...] = (
                "git",
                "push",
                "origin",
                f"{candidate.commit}:{_candidate_git_ref(candidate.ref)}",
            )
        else:
            if remote != expected_remote_commit:
                raise GitOperationError(
                    "candidate reference lease no longer matches",
                    stage="push",
                )
            args = (
                "git",
                "push",
                f"--force-with-lease={_candidate_git_ref(candidate.ref)}:{expected_remote_commit}",
                "origin",
                f"{candidate.commit}:{_candidate_git_ref(candidate.ref)}",
            )
        await self._push_checked(candidate.workspace_path, args)

    async def accept_candidate(
        self,
        candidate: CandidateWorkspace,
        *,
        run_branch: str,
        message: str,
    ) -> CandidateAcceptance:
        """Idempotently amend and publish acceptance, then clean up best effort."""
        local_candidate = await self._optional_ref(
            candidate.workspace_path,
            ("git", "rev-parse", "--verify", f"refs/heads/{candidate.local_branch}"),
        )
        current_branch = (
            await self._git_checked(
                candidate.workspace_path,
                ("git", "branch", "--show-current"),
                stage="workspace",
                failure="workspace branch could not be resolved",
            )
        ).stdout.strip()

        if local_candidate is not None:
            if current_branch != candidate.local_branch:
                await self._git_checked(
                    candidate.workspace_path,
                    ("git", "switch", candidate.local_branch),
                    stage="workspace",
                    failure="candidate branch could not be selected for acceptance",
                )
            head = (
                await self._git_checked(
                    candidate.workspace_path,
                    ("git", "rev-parse", "HEAD"),
                    stage="commit",
                    failure="accepted candidate commit could not be resolved",
                )
            ).stdout.strip()
            if head == candidate.commit:
                await self._validate_candidate(candidate)
                final_message = message
                await self._git_checked(
                    candidate.workspace_path,
                    (
                        "git",
                        "-c",
                        "user.name=Harness Evolution",
                        "-c",
                        "user.email=harness-evolution@localhost",
                        "commit",
                        "--amend",
                        "-m",
                        message,
                    ),
                    stage="commit",
                    failure="accepted candidate amend failed",
                )
                head = (
                    await self._git_checked(
                        candidate.workspace_path,
                        ("git", "rev-parse", "HEAD"),
                        stage="commit",
                        failure="accepted candidate commit could not be resolved",
                    )
                ).stdout.strip()
            else:
                await self._validate_accepted_replay(
                    candidate.workspace_path,
                    evaluated_commit=candidate.commit,
                    accepted_commit=head,
                    run_id=candidate.run_id,
                    base_commit=candidate.base_commit,
                )
                final_message = await self._commit_message(
                    candidate.workspace_path,
                    head,
                )
            final_commit = head
            final_candidate = CandidateWorkspace(
                workspace_path=candidate.workspace_path,
                run_branch=run_branch,
                local_branch=candidate.local_branch,
                ref=candidate.ref,
                commit=final_commit,
                message=message,
                run_id=candidate.run_id,
                base_commit=candidate.base_commit,
            )
            remote_commit = await self.remote_tag_commit(
                candidate.workspace_path,
                "origin",
                candidate.ref,
            )
            if remote_commit not in {None, candidate.commit, final_commit}:
                raise GitOperationError(
                    "candidate reference lease no longer matches",
                    stage="push",
                )
            await self.push_candidate(
                final_candidate,
                expected_remote_commit=candidate.commit if remote_commit is not None else None,
            )
        else:
            run_commit = (
                await self._git_checked(
                    candidate.workspace_path,
                    ("git", "rev-parse", f"refs/heads/{run_branch}"),
                    stage="workspace",
                    failure="accepted run branch could not be resolved",
                )
            ).stdout.strip()
            await self._validate_accepted_replay(
                candidate.workspace_path,
                evaluated_commit=candidate.commit,
                accepted_commit=run_commit,
                run_id=candidate.run_id,
                base_commit=candidate.base_commit,
            )
            final_commit = run_commit
            final_message = await self._commit_message(
                candidate.workspace_path,
                run_commit,
            )

        await self._git_checked(
            candidate.workspace_path,
            ("git", "switch", run_branch),
            stage="workspace",
            failure="run branch could not be selected after acceptance",
        )
        await self._git_checked(
            candidate.workspace_path,
            ("git", "merge", "--ff-only", final_commit),
            stage="workspace",
            failure="run branch is not a fast-forward",
        )
        await self._push_run_branch(
            candidate.workspace_path,
            run_branch,
            expected_run_id=candidate.run_id,
            base_commit=candidate.base_commit,
            require_trailer=True,
        )

        cleanup_errors: list[str] = []
        try:
            await self._delete_remote_tag(candidate.workspace_path, candidate.ref)
        except GitOperationError as exc:
            cleanup_errors.append(str(exc))
        if local_candidate is not None:
            try:
                await self._git_checked(
                    candidate.workspace_path,
                    ("git", "branch", "-D", candidate.local_branch),
                    stage="workspace",
                    failure="accepted local candidate branch could not be removed",
                )
            except GitOperationError as exc:
                cleanup_errors.append(str(exc))
        return CandidateAcceptance(
            commit=final_commit,
            message=final_message,
            cleanup_error="; ".join(dict.fromkeys(cleanup_errors)) or None,
        )

    async def reject_candidate(
        self,
        candidate: CandidateWorkspace,
        *,
        run_branch: str,
    ) -> str | None:
        """Delete rejected candidate refs without changing the run branch."""
        await self._git_checked(
            candidate.workspace_path,
            ("git", "switch", run_branch),
            stage="workspace",
            failure="run branch could not be selected after rejection",
        )
        cleanup_errors: list[str] = []
        try:
            await self._delete_remote_tag(candidate.workspace_path, candidate.ref)
        except GitOperationError as exc:
            cleanup_errors.append(str(exc))
        try:
            await self._git_checked(
                candidate.workspace_path,
                ("git", "branch", "-D", candidate.local_branch),
                stage="workspace",
                failure="rejected local candidate branch could not be removed",
            )
        except GitOperationError as exc:
            cleanup_errors.append(str(exc))
        return "; ".join(dict.fromkeys(cleanup_errors)) or None

    async def remote_ref_commit(
        self,
        workspace_path: Path,
        remote: str,
        ref: str,
    ) -> str | None:
        """Resolve one remote branch without fetching unrelated objects."""
        result = await self._git(
            workspace_path,
            ("git", "ls-remote", "--heads", remote, f"refs/heads/{ref}"),
            stage="push",
        )
        if result.returncode != 0:
            raise self._error_from_result(
                result,
                stage="push",
                failure="remote reference lookup failed",
            )
        for line in result.stdout.splitlines():
            commit, separator, found = line.partition("\t")
            if separator and found == f"refs/heads/{ref}":
                return commit
        return None

    async def remote_tag_commit(
        self,
        workspace_path: Path,
        remote: str,
        ref: str,
    ) -> str | None:
        """Resolve one candidate tag without treating it as a branch."""
        if ref.startswith("evo-candidate/"):
            return await self.remote_ref_commit(workspace_path, remote, ref)
        result = await self._git(
            workspace_path,
            ("git", "ls-remote", "--tags", remote, f"refs/tags/{ref}"),
            stage="push",
        )
        if result.returncode != 0:
            raise self._error_from_result(
                result,
                stage="push",
                failure="remote candidate lookup failed",
            )
        for line in result.stdout.splitlines():
            commit, separator, found = line.partition("\t")
            if separator and found == f"refs/tags/{ref}":
                return commit
        return None

    async def validate_for_push(
        self,
        prepared: PreparedWorkspace,
        run: EvolutionRunRecord,
        *,
        expected_run_id: str | None = None,
        require_trailer: bool = False,
    ) -> None:
        """Validate the complete run-branch snapshot before publication."""
        if not self._RUN_BRANCH.fullmatch(prepared.branch) or prepared.branch in {"main", "master"}:
            raise GitOperationError("run branch name is unsafe", stage="workspace")
        if not re.fullmatch(r"[0-9a-f]{40}", prepared.base_commit):
            raise GitOperationError("base commit is invalid", stage="reference")
        if prepared.branch != f"evo/{run.created_at:%Y%m%d}-{run.id}":
            raise GitOperationError("run branch does not match the run", stage="workspace")
        await self._validate_publication_state(
            prepared.path,
            branch=prepared.branch,
            base_commit=prepared.base_commit,
            expected_run_id=expected_run_id,
            require_trailer=require_trailer,
        )

    async def publish_run_branch(
        self,
        prepared: PreparedWorkspace,
        run: EvolutionRunRecord,
        *,
        expected_run_id: str | None = None,
        require_trailer: bool = False,
    ) -> None:
        """Validate and publish only the fast-forward run branch."""
        await self.validate_for_push(
            prepared,
            run,
            expected_run_id=expected_run_id or run.id,
            require_trailer=require_trailer,
        )
        await self._push_run_branch(
            prepared.path,
            prepared.branch,
            expected_run_id=expected_run_id or run.id,
            base_commit=prepared.base_commit,
            require_trailer=require_trailer,
        )

    def cleanup_workspaces(
        self,
        *,
        active_run_ids: set[str],
        keep: int = 20,
    ) -> tuple[Path, ...]:
        """Delete only inactive workspaces beyond the newest retention window."""
        if keep < 0:
            raise ValueError("workspace retention cannot be negative")
        if not self.workspaces_root.exists():
            return ()
        candidates = sorted(
            (
                path
                for path in self.workspaces_root.iterdir()
                if path.is_dir() and path.name not in active_run_ids
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
            reverse=True,
        )
        removable = candidates[keep:]
        for path in removable:
            path.resolve().relative_to(self.workspaces_root.resolve())
            shutil.rmtree(path)
        return tuple(removable)

    async def _push_run_branch(
        self,
        workspace_path: Path,
        branch: str,
        *,
        expected_run_id: str | None = None,
        base_commit: str | None = None,
        require_trailer: bool = False,
    ) -> None:
        """Publish only a fast-forward run branch and never force it."""
        await self._validate_publication_state(
            workspace_path,
            branch=branch,
            base_commit=base_commit,
            expected_run_id=expected_run_id,
            require_trailer=require_trailer,
        )
        if not self._push_enabled:
            return
        remote_commit = await self.remote_ref_commit(workspace_path, "origin", branch)
        head = (
            await self._git_checked(
                workspace_path,
                ("git", "rev-parse", "HEAD"),
                stage="push",
                failure="run branch commit could not be resolved",
            )
        ).stdout.strip()
        if remote_commit is not None and remote_commit != head:
            ancestor = await self._git(
                workspace_path,
                ("git", "merge-base", "--is-ancestor", remote_commit, head),
                stage="push",
            )
            if ancestor.returncode != 0:
                raise GitOperationError(
                    "run branch update is not a fast-forward",
                    stage="push",
                )
        await self._push_checked(
            workspace_path,
            ("git", "push", "origin", f"{head}:refs/heads/{branch}"),
        )

    async def _delete_remote_tag(self, workspace_path: Path, ref: str) -> None:
        """Delete a transient candidate tag, treating absence as success."""
        if not self._push_enabled:
            return
        if await self.remote_tag_commit(workspace_path, "origin", ref) is None:
            return
        await self._push_checked(
            workspace_path,
            ("git", "push", "origin", f":{_candidate_git_ref(ref)}"),
        )

    async def _reuse_or_reject(
        self,
        workspace_path: Path,
        branch: str,
        expected_repo_url: str,
    ) -> None:
        """Resume an existing safe run workspace without resetting its branch."""
        if not (workspace_path / ".git").is_dir():
            raise GitOperationError("workspace path exists without git metadata", stage="workspace")
        current_url = await self._git(
            workspace_path,
            ("git", "remote", "get-url", "origin"),
            stage="workspace",
        )
        if current_url.returncode != 0 or current_url.stdout.strip() != expected_repo_url:
            raise GitOperationError("workspace remote does not match the run", stage="workspace")
        current_branch = await self._git(
            workspace_path,
            ("git", "branch", "--show-current"),
            stage="workspace",
        )
        if current_branch.stdout.strip() != branch:
            await self._git_checked(
                workspace_path,
                ("git", "switch", branch),
                stage="workspace",
                failure="existing run workspace cannot switch branches",
            )
        await self._require_clean(workspace_path)

    async def _require_clean(self, workspace_path: Path) -> None:
        """Require no tracked or untracked file before ref-changing operations."""
        result = await self._git_checked(
            workspace_path,
            ("git", "status", "--porcelain", "--untracked-files=all"),
            stage="workspace",
            failure="workspace status check failed",
        )
        if result.stdout.strip():
            raise GitOperationError("workspace is not clean", stage="workspace")

    async def _ensure_mirror(self, repo_path: Path, repo_url: str) -> None:
        """Create or refresh a cache only when it still points at the run's repository."""
        if repo_path.exists():  # noqa: ASYNC240
            current = await self._git(
                repo_path,
                ("git", "remote", "get-url", "origin"),
                stage="clone",
            )
            if current.returncode != 0 or current.stdout.strip() != repo_url:
                try:
                    repo_path.resolve().relative_to(self.repos_root.resolve())  # noqa: ASYNC240
                except ValueError:
                    raise GitOperationError(
                        "repository cache escaped its configured root",
                        stage="clone",
                    ) from None
                shutil.rmtree(repo_path)
        if not repo_path.exists():  # noqa: ASYNC240
            try:
                await self._git_checked(
                    None,
                    (
                        "git",
                        "clone",
                        "--mirror",
                        "--no-hardlinks",
                        "--",
                        repo_url,
                        str(repo_path),
                    ),
                    stage="clone",
                    failure="repository cache clone failed",
                )
            except Exception:
                shutil.rmtree(repo_path, ignore_errors=True)
                raise
            return
        await self._git_checked(
            repo_path,
            ("git", "remote", "update", "--prune"),
            stage="clone",
            failure="repository cache refresh failed",
            retryable=True,
        )

    async def _validate_candidate(self, candidate: CandidateWorkspace) -> None:
        """Validate the exact local commit immediately before candidate publication."""
        await self._validate_publication_state(
            candidate.workspace_path,
            branch=candidate.local_branch,
            base_commit=candidate.base_commit,
            expected_commit=candidate.commit,
            expected_run_id=candidate.run_id,
            require_trailer=True,
        )

    async def _validate_accepted_replay(
        self,
        workspace_path: Path,
        *,
        evaluated_commit: str,
        accepted_commit: str,
        run_id: str,
        base_commit: str,
    ) -> None:
        """Reuse only the deterministic accepted amend produced by an earlier attempt."""
        await self._require_clean(workspace_path)
        await self._require_ancestry(
            workspace_path,
            base_commit,
            commit=accepted_commit,
        )
        evaluated_tree = (
            await self._git_checked(
                workspace_path,
                ("git", "rev-parse", f"{evaluated_commit}^{{tree}}"),
                stage="workspace",
                failure="evaluated commit tree could not be resolved",
            )
        ).stdout.strip()
        accepted_tree = (
            await self._git_checked(
                workspace_path,
                ("git", "rev-parse", f"{accepted_commit}^{{tree}}"),
                stage="workspace",
                failure="accepted commit tree could not be resolved",
            )
        ).stdout.strip()
        if accepted_tree != evaluated_tree:
            raise GitOperationError(
                "accepted replay changed the evaluated tree",
                stage="commit",
            )
        evaluated_parent = (
            await self._git_checked(
                workspace_path,
                ("git", "rev-parse", f"{evaluated_commit}^"),
                stage="commit",
                failure="evaluated commit parent could not be resolved",
            )
        ).stdout.strip()
        accepted_parent = (
            await self._git_checked(
                workspace_path,
                ("git", "rev-parse", f"{accepted_commit}^"),
                stage="commit",
                failure="accepted commit parent could not be resolved",
            )
        ).stdout.strip()
        if accepted_parent != evaluated_parent:
            raise GitOperationError(
                "accepted replay does not descend directly from the evaluated parent",
                stage="commit",
            )
        message = await self._commit_message(workspace_path, accepted_commit)
        if not _has_trailer(message, "Harness-Run-Id", run_id):
            raise GitOperationError(
                "accepted replay is missing the Harness-Run-Id trailer",
                stage="commit",
            )
        if not _has_trailer(message, "Harness-Evaluated-Commit", evaluated_commit):
            raise GitOperationError(
                "accepted replay does not reference the evaluated commit",
                stage="commit",
            )
        if not any(line.startswith("Harness-Verdict: accepted ") for line in message.splitlines()):
            raise GitOperationError(
                "accepted replay is missing the accepted verdict",
                stage="commit",
            )

    async def _validate_publication_state(
        self,
        workspace_path: Path,
        *,
        branch: str,
        base_commit: str | None = None,
        expected_commit: str | None = None,
        expected_run_id: str | None = None,
        require_trailer: bool = False,
    ) -> None:
        """Validate one exact branch/commit snapshot through a shared push boundary."""
        candidate_branch = re.fullmatch(r"harness-candidate/[0-9a-f]{16}", branch)
        if not self._RUN_BRANCH.fullmatch(branch) and candidate_branch is None:
            raise GitOperationError("publication branch name is unsafe", stage="workspace")
        current_branch = await self._git_checked(
            workspace_path,
            ("git", "branch", "--show-current"),
            stage="workspace",
            failure="publication branch could not be resolved",
        )
        if current_branch.stdout.strip() != branch:
            raise GitOperationError("publication branch is not selected", stage="workspace")
        await self._require_clean(workspace_path)
        commit = expected_commit or "HEAD"
        if expected_commit is not None:
            head = await self._git_checked(
                workspace_path,
                ("git", "rev-parse", "HEAD"),
                stage="workspace",
                failure="publication commit could not be resolved",
            )
            if head.stdout.strip() != expected_commit:
                raise GitOperationError(
                    "publication commit changed before push",
                    stage="workspace",
                )
        if base_commit is not None:
            await self._require_ancestry(
                workspace_path,
                base_commit,
                commit=commit,
            )
        if require_trailer:
            if expected_run_id is None:
                raise GitOperationError(
                    "publication run id is required for trailer validation",
                    stage="workspace",
                )
            message = await self._commit_message(workspace_path, commit)
            if not _has_trailer(message, "Harness-Run-Id", expected_run_id):
                raise GitOperationError(
                    "publication commit is missing the Harness-Run-Id trailer",
                    stage="workspace",
                )

    async def _require_ancestry(
        self,
        workspace_path: Path,
        base_commit: str,
        *,
        commit: str = "HEAD",
    ) -> None:
        """Require the publication commit to descend from the frozen base."""
        result = await self._git(
            workspace_path,
            ("git", "merge-base", "--is-ancestor", base_commit, commit),
            stage="workspace",
        )
        if result.returncode != 0:
            raise GitOperationError(
                "bounded commit does not share the frozen base ancestry",
                stage="workspace",
            )

    async def _commit_message(self, workspace_path: Path, commit: str) -> str:
        """Read one local commit subject/body for trailer validation."""
        return (
            await self._git_checked(
                workspace_path,
                ("git", "log", "-1", "--format=%B", commit),
                stage="workspace",
                failure="commit message could not be read",
            )
        ).stdout

    async def _optional_ref(self, cwd: Path, argv: tuple[str, ...]) -> str | None:
        """Return a ref only when it resolves, without treating absence as failure."""
        result = await self._git(cwd, argv, stage="workspace")
        return result.stdout.strip() if result.returncode == 0 else None

    async def _push_checked(self, cwd: Path, argv: tuple[str, ...]) -> None:
        """Run one push and retain both failure classification and retryability."""
        result = await self._git(cwd, argv, stage="push")
        if result.returncode != 0:
            raise self._error_from_result(
                result,
                stage="push",
                failure="git push failed",
            )

    async def _git_checked(
        self,
        cwd: Path | None,
        argv: tuple[str, ...],
        *,
        stage: GitStage,
        failure: str,
        retryable: bool = False,
    ) -> GitProcessResult:
        """Run git and replace raw diagnostics with a stable safe error."""
        result = await self._git(cwd, argv, stage=stage)
        if result.returncode != 0:
            raise self._error_from_result(
                result,
                stage=stage,
                failure=failure,
                retryable=retryable,
            )
        return result

    async def _git(
        self,
        cwd: Path | None,
        argv: tuple[str, ...],
        *,
        stage: GitStage,
    ) -> GitProcessResult:
        """Invoke the process seam with one bounded timeout."""
        try:
            return await asyncio.wait_for(
                self._runner(argv, cwd),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, OSError, RuntimeError):
            raise GitOperationError(
                f"{stage} operation failed",
                stage=stage,
                retryable=True,
            ) from None

    def _error_from_result(
        self,
        result: GitProcessResult,
        *,
        stage: GitStage,
        failure: str,
        retryable: bool = False,
    ) -> GitOperationError:
        """Classify transient transport failures without exposing command output."""
        diagnostic = sanitize_message(result.stderr, limit=500).lower()
        is_retryable = retryable or any(marker in diagnostic for marker in self._TRANSIENT_MARKERS)
        return GitOperationError(failure, stage=stage, retryable=is_retryable)

    @staticmethod
    def _write_patch(workspace_path: Path, patch: Patch) -> None:
        """Write a bounded repository-relative patch without path traversal."""
        if patch.changes is not None:
            destinations = []
            for name, content in patch.changes.items():
                from app.service.evolution.skill_safety import validate_path

                validate_path(name)
                destination = workspace_path / name
                if any(
                    parent.is_symlink()
                    for parent in (destination, *destination.parents)
                    if parent != workspace_path.parent
                ):
                    raise GitOperationError("symlink in patch path", stage="patch")
                try:
                    destination.resolve().relative_to(workspace_path.resolve())
                except ValueError:
                    raise GitOperationError("patch escapes workspace", stage="patch") from None
                destinations.append((destination, content))
            for destination, content in destinations:
                if content is None:
                    destination.unlink(missing_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8")
            return
        relative = PurePosixPath(patch.file)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise GitOperationError("patch path is unsafe", stage="patch")
        destination = workspace_path.joinpath(*relative.parts)
        try:
            destination.parent.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            raise GitOperationError("patch path escapes the workspace", stage="patch") from None
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
        separator = "\n" if existing and not existing.endswith("\n") else ""
        destination.write_text(
            existing + separator + "\n".join(patch.lines) + "\n",
            encoding="utf-8",
        )


async def retry_push(
    push: Callable[[], Awaitable[None]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, GitOperationError], Awaitable[None]] | None = None,
    interval_seconds: float = 5.0,
    attempts: int = 3,
) -> int:
    """Retry a transient push immediately and return its successful attempt count."""
    for attempt in range(1, attempts + 1):
        try:
            await push()
            return attempt
        except GitOperationError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            if on_retry is not None:
                await on_retry(attempt, exc)
            await sleep(interval_seconds)
    raise AssertionError("push retry loop exhausted without a result")


def _required_trailer(message: str, name: str) -> str:
    """Return one required trailer value or reject an untraceable commit."""
    prefix = f"{name}: "
    for line in reversed(message.splitlines()):
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            if value:
                return value
    raise GitOperationError(
        f"candidate commit is missing the {name} trailer",
        stage="workspace",
    )


def _has_trailer(message: str, name: str, expected: str) -> bool:
    """Return whether one commit message contains the exact required trailer."""
    prefix = f"{name}: "
    return any(
        line[len(prefix) :].strip() == expected
        for line in message.splitlines()
        if line.startswith(prefix)
    )


async def _run_process(
    argv: Sequence[str],
    cwd: Path | None,
) -> GitProcessResult:
    """Execute git through a sanitized argument array with prompts disabled."""
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        },
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        await _terminate_process_group(process)
        raise
    return GitProcessResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Terminate, escalate, and reap a timed-out Git process and its children."""
    if sys.platform == "win32":
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    if process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=0.5)
    else:
        await asyncio.sleep(0.5)
    try:
        if sys.platform == "win32":
            if process.returncode is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if process.returncode is None:
        await process.wait()


def _safe_component(value: str) -> str:
    """Reduce one database identifier to a safe single path component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not cleaned:
        raise GitOperationError("workspace identifier is empty", stage="workspace")
    return cleaned


def _normalize_sub_dir(value: str) -> str:
    """Normalize and validate a repository-relative target directory."""
    normalized = PurePosixPath((value or ".").strip()).as_posix()
    if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise GitOperationError("target subdirectory is unsafe", stage="subdirectory")
    return normalized or "."


def candidate_local_branch(candidate_ref: str) -> str:
    """Map a remote candidate tag to a legal, non-conflicting local work branch."""
    digest = hashlib.sha256(candidate_ref.encode()).hexdigest()[:16]
    return f"harness-candidate/{digest}"


def _candidate_git_ref(ref: str) -> str:
    """Skill candidates are branches; legacy engines retain their tag protocol."""
    namespace = "heads" if ref.startswith("evo-candidate/") else "tags"
    return f"refs/{namespace}/{ref}"
