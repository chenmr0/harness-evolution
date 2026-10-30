"""Real local-git contracts for preparation, candidate, accept, and reject."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db.models.evolution import EvolutionRunRecord
from app.main import create_app
from app.service.evolution.git_candidate import (
    CandidateTrace,
    build_accepted_message,
    build_candidate_message,
)
from app.third_party_api.git.workspace import (
    GitOperationError,
    GitProcessResult,
    GitWorkspaceManager,
    Patch,
    retry_push,
)

NOW = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)


def _git(cwd: Path, *args: str) -> str:
    """Run one deterministic local git command and return stdout."""
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def remote_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a bare remote with a tagged target directory and one commit."""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "config", "user.name", "Test User")
    _git(seed, "config", "user.email", "test@example.invalid")
    target = seed / "skills" / "deep-research"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Deep Research\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "initial target")
    _git(seed, "tag", "v2.0.0")
    _git(seed, "push", "origin", "HEAD:refs/heads/main", "--tags")
    return bare, seed, _git(seed, "rev-parse", "HEAD")


def _run(repo_url: str, base_commit: str) -> EvolutionRunRecord:
    """Build one run snapshot for real local git operations."""
    return EvolutionRunRecord(
        id="run-m5-09",
        title="deep-research evolution",
        mode="full",
        engine_key="full",
        eval_config={"gateway": "fake"},
        target_config_id="target-m5-09",
        target_type="skill",
        target_name="deep-research",
        target_version="v2.0.0",
        repo_url=repo_url,
        sub_dir="skills/deep-research",
        repo_path=None,
        base_ref="v2.0.0",
        base_commit=base_commit,
        branch_name=None,
        workspace_path=None,
        remote_name="origin",
        push_status="pending",
        pushed_at=None,
        push_error=None,
        window_from=None,
        window_to=None,
        models=[],
        status="preparing",
        blocked_stage=None,
        retry_attempts=0,
        next_retry_at=None,
        eval_status="pending",
        error_stage=None,
        error_message=None,
        error_at=None,
        progress_unit=0,
        stats={},
        created_by=None,
        created_at=NOW,
        finished_at=None,
    )


async def test_prepare_creates_isolated_workspace_and_run_branch(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Preparation clones the tagged commit and creates a clean run branch."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )

    prepared = await manager.prepare(_run(str(bare), base_commit))

    assert prepared.base_commit == base_commit
    assert prepared.branch == "evo/20261028-run-m5-09"
    assert prepared.path == tmp_path / "workspaces" / "run-m5-09"
    assert prepared.repo_path == tmp_path / "repos" / "target-m5-09"
    assert prepared.path.is_dir()
    assert (prepared.path / "skills" / "deep-research" / "SKILL.md").is_file()
    assert _git(prepared.path, "status", "--porcelain") == ""
    assert _git(prepared.path, "rev-parse", "HEAD") == base_commit
    assert _git(prepared.path, "branch", "--show-current") == prepared.branch


async def test_prepare_rejects_missing_ref_and_subdirectory(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Invalid refs and target directories fail before any run branch exists."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )

    with pytest.raises(GitOperationError, match="reference"):
        await manager.prepare(
            replace(_run(str(bare), base_commit), target_version="missing-tag"),
        )

    with pytest.raises(GitOperationError, match="subdirectory"):
        await manager.prepare(
            replace(_run(str(bare), base_commit), sub_dir="skills/missing"),
        )


async def test_prepare_sanitizes_clone_and_workspace_copy_failures(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Clone/copy failures expose safe stages and leave no partial workspace."""
    bare, _seed, base_commit = remote_repo

    class FailingRunner:
        def __init__(self, marker: str) -> None:
            self.marker = marker

        async def __call__(
            self,
            argv: tuple[str, ...],
            cwd: Path | None,
        ) -> GitProcessResult:
            if self.marker in argv:
                return GitProcessResult(
                    returncode=128,
                    stdout="",
                    stderr=f"fatal: token=secret in {self.marker}",
                )
            return await self._real(argv, cwd)

        async def _real(
            self,
            argv: tuple[str, ...],
            cwd: Path | None,
        ) -> GitProcessResult:
            from app.third_party_api.git.workspace import _run_process

            return await _run_process(argv, cwd)

    clone_manager = GitWorkspaceManager(
        repos_root=tmp_path / "clone-repos",
        workspaces_root=tmp_path / "clone-workspaces",
        runner=FailingRunner("clone"),
    )
    with pytest.raises(GitOperationError, match="repository cache clone failed") as clone_error:
        await clone_manager.prepare(_run(str(bare), base_commit))
    assert "secret" not in str(clone_error.value)
    assert not (tmp_path / "clone-repos" / "target-m5-09").exists()

    copy_manager = GitWorkspaceManager(
        repos_root=tmp_path / "copy-repos",
        workspaces_root=tmp_path / "copy-workspaces",
        runner=FailingRunner("--no-checkout"),
    )
    with pytest.raises(GitOperationError, match="workspace clone failed"):
        await copy_manager.prepare(_run(str(bare), base_commit))
    assert not (tmp_path / "copy-workspaces" / "run-m5-09").exists()


async def test_candidate_accept_amends_ffs_run_branch_and_removes_candidate(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Accepted work updates candidate and run refs without rewriting run history."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    trace = CandidateTrace(
        cluster_id="cluster-1",
        cluster_title="检索索引读取失败",
        probes=("probe-1",),
        bad_cases=("case-1",),
        analysis="读取快照前缺少字段校验。",
    )
    candidate_message = build_candidate_message(
        run,
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        web_base_url="https://harness.example",
    )
    assert "https://harness.example/evolution/runs/run-m5-09?step=iteration" in candidate_message
    assert "https://harness.example/evolution/runs/run-m5-09?step=clusters&cluster=cluster-1" in (
        candidate_message
    )

    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(
            file="skills/deep-research/EVOLUTION.md",
            lines=("placeholder candidate",),
        ),
        message=candidate_message,
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)
    remote_candidate = _git(bare, "rev-parse", f"refs/tags/{candidate.ref}")
    assert remote_candidate == candidate.commit

    accepted_message = build_accepted_message(
        run,
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        evaluated_commit=candidate.commit,
        current_score=80.0,
        candidate_score=85.0,
        reason="候选分 85.0 高于基线 80.0（+5.0），按 external-verdict 规则接纳",
        web_base_url="https://harness.example",
    )
    replay_manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    final = await replay_manager.accept_candidate(
        candidate,
        run_branch=prepared.branch,
        message=accepted_message,
    )

    assert final.commit != candidate.commit
    assert _git(bare, "rev-parse", prepared.branch) == final.commit
    run_branch_log = _git(bare, "log", "--format=%s", prepared.branch)
    assert "修复检索索引读取失败" in run_branch_log
    remote_candidate_after = subprocess.run(
        ("git", "ls-remote", "--tags", str(bare), f"refs/tags/{candidate.ref}"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert remote_candidate_after == ""


async def test_accept_is_replayable_after_transient_candidate_push_failure(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed amended-candidate push can resume without re-amending from stale state."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    trace = CandidateTrace("cluster-1", "检索索引读取失败", ("probe-1",), ("case-1",), "分析")
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("candidate",)),
        message=build_candidate_message(
            run,
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)
    accepted_message = build_accepted_message(
        run,
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        evaluated_commit=candidate.commit,
        current_score=80.0,
        candidate_score=85.0,
        reason="accepted",
        web_base_url=None,
    )
    original_push = manager._push_checked
    candidate_push_count = 0

    async def fail_first_candidate_push(cwd: Path, argv: tuple[str, ...]) -> None:
        nonlocal candidate_push_count
        if any(f"refs/tags/{candidate.ref}" in argument for argument in argv):
            candidate_push_count += 1
            if candidate_push_count == 1:
                raise GitOperationError("candidate push timed out", stage="push", retryable=True)
        await original_push(cwd, argv)

    monkeypatch.setattr(manager, "_push_checked", fail_first_candidate_push)
    with pytest.raises(GitOperationError, match="candidate push timed out"):
        await manager.accept_candidate(
            candidate,
            run_branch=prepared.branch,
            message=accepted_message,
        )

    replay_manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    final = await replay_manager.accept_candidate(
        candidate,
        run_branch=prepared.branch,
        message=accepted_message,
    )

    assert final.commit != candidate.commit
    assert _git(bare, "rev-parse", prepared.branch) == final.commit
    assert _git(prepared.path, "rev-parse", "HEAD") == final.commit


async def test_accept_is_replayable_after_run_branch_push_failure(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed run-branch publication resumes from the already amended candidate."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    trace = CandidateTrace("cluster-1", "检索索引读取失败", ("probe-1",), ("case-1",), "分析")
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("candidate",)),
        message=build_candidate_message(
            run,
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)
    accepted_message = build_accepted_message(
        run,
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        evaluated_commit=candidate.commit,
        current_score=80.0,
        candidate_score=85.0,
        reason="accepted",
        web_base_url=None,
    )
    original_push_run = manager._push_run_branch
    run_push_count = 0

    async def fail_first_run_push(*_args: object, **_kwargs: object) -> None:
        nonlocal run_push_count
        run_push_count += 1
        if run_push_count == 1:
            raise GitOperationError("run push timed out", stage="push", retryable=True)
        await original_push_run(*_args, **_kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(manager, "_push_run_branch", fail_first_run_push)
    with pytest.raises(GitOperationError, match="run push timed out"):
        await manager.accept_candidate(
            candidate,
            run_branch=prepared.branch,
            message=accepted_message,
        )

    final = await manager.accept_candidate(
        candidate,
        run_branch=prepared.branch,
        message=accepted_message,
    )

    assert _git(bare, "rev-parse", prepared.branch) == final.commit


async def test_accept_cleanup_failures_are_non_blocking_with_warnings(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote and local candidate cleanup failures never turn a published accept into blocked."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    trace = CandidateTrace("cluster-1", "检索索引读取失败", ("probe-1",), ("case-1",), "分析")
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("candidate",)),
        message=build_candidate_message(
            run,
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)
    accepted_message = build_accepted_message(
        run,
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        evaluated_commit=candidate.commit,
        current_score=80.0,
        candidate_score=85.0,
        reason="accepted",
        web_base_url=None,
    )

    async def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise GitOperationError("delete denied", stage="push")

    monkeypatch.setattr(manager, "_delete_remote_tag", fail_delete)
    remote_cleanup = await manager.accept_candidate(
        candidate,
        run_branch=prepared.branch,
        message=accepted_message,
    )
    assert remote_cleanup.cleanup_error == "delete denied"
    assert _git(bare, "rev-parse", prepared.branch) == remote_cleanup.commit

    local_manager = GitWorkspaceManager(
        repos_root=tmp_path / "local-repos",
        workspaces_root=tmp_path / "local-workspaces",
    )
    local_prepared = await local_manager.prepare(replace(run, id="run-local-cleanup"))
    local_candidate = await local_manager.create_candidate(
        local_prepared,
        run_branch=local_prepared.branch,
        candidate_ref=f"{local_prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("candidate",)),
        message=build_candidate_message(
            replace(run, id="run-local-cleanup"),
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await local_manager.push_candidate(local_candidate, expected_remote_commit=None)
    local_message = build_accepted_message(
        replace(run, id="run-local-cleanup"),
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        evaluated_commit=local_candidate.commit,
        current_score=80.0,
        candidate_score=85.0,
        reason="accepted",
        web_base_url=None,
    )
    original_checked = local_manager._git_checked

    async def fail_local_branch_delete(
        cwd: Path | None,
        argv: tuple[str, ...],
        **kwargs: object,
    ) -> GitProcessResult:
        if "branch" in argv and "-D" in argv and local_candidate.local_branch in argv:
            raise GitOperationError("local delete denied", stage="workspace")
        return await original_checked(cwd, argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(local_manager, "_git_checked", fail_local_branch_delete)
    local_cleanup = await local_manager.accept_candidate(
        local_candidate,
        run_branch=local_prepared.branch,
        message=local_message,
    )
    assert local_cleanup.cleanup_error == "local delete denied"
    assert _git(bare, "rev-parse", local_prepared.branch) == local_cleanup.commit


async def test_accept_does_not_read_commit_message_after_run_branch_publication(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-publication bookkeeping cannot turn a landed acceptance into a failure."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    trace = CandidateTrace("cluster-1", "检索索引读取失败", ("probe-1",), ("case-1",), "分析")
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("candidate",)),
        message=build_candidate_message(
            run,
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)
    accepted_message = build_accepted_message(
        run,
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        evaluated_commit=candidate.commit,
        current_score=80.0,
        candidate_score=85.0,
        reason="accepted",
        web_base_url=None,
    )
    original_push_run = manager._push_run_branch
    original_message_read = manager._commit_message
    run_branch_published = False

    async def mark_published(*args: object, **kwargs: object) -> None:
        nonlocal run_branch_published
        await original_push_run(*args, **kwargs)  # type: ignore[arg-type]
        run_branch_published = True

    async def fail_post_publication_read(cwd: Path, commit: str) -> str:
        if run_branch_published:
            raise GitOperationError("post-publication read failed", stage="workspace")
        return await original_message_read(cwd, commit)

    monkeypatch.setattr(manager, "_push_run_branch", mark_published)
    monkeypatch.setattr(manager, "_commit_message", fail_post_publication_read)

    accepted = await manager.accept_candidate(
        candidate,
        run_branch=prepared.branch,
        message=accepted_message,
    )

    assert accepted.commit == _git(bare, "rev-parse", prepared.branch)
    assert accepted.message.strip() == accepted_message.strip()


async def test_accept_replay_rejects_amended_tree_with_a_different_parent(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Tree and trailers alone cannot disguise an accepted commit from another history."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    trace = CandidateTrace("cluster-1", "检索索引读取失败", ("probe-1",), ("case-1",), "分析")
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("candidate",)),
        message=build_candidate_message(
            run,
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)
    accepted_message = build_accepted_message(
        run,
        round_seq=1,
        trace=trace,
        patch_sha256="a" * 64,
        created_at=NOW,
        evaluated_commit=candidate.commit,
        current_score=80.0,
        candidate_score=85.0,
        reason="accepted",
        web_base_url=None,
    )
    _git(
        prepared.path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "switch",
        prepared.branch,
    )
    _git(
        prepared.path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "rogue parent",
    )
    _git(prepared.path, "checkout", candidate.commit, "--", ".")
    _git(
        prepared.path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        accepted_message,
    )
    rogue = _git(prepared.path, "rev-parse", "HEAD")
    _git(prepared.path, "branch", "-f", candidate.local_branch, rogue)

    with pytest.raises(GitOperationError, match="evaluated parent"):
        await manager.accept_candidate(
            candidate,
            run_branch=prepared.branch,
            message=accepted_message,
        )

    assert _git(bare, "rev-parse", prepared.branch) == base_commit


async def test_reject_deletes_candidate_and_leaves_run_branch_unchanged(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Rejected work removes only the candidate ref and returns to the baseline."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    prepared = await manager.prepare(_run(str(bare), base_commit))
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("rejected",)),
        message=build_candidate_message(
            _run(str(bare), base_commit),
            round_seq=1,
            trace=CandidateTrace(
                cluster_id="cluster-1",
                cluster_title="检索索引读取失败",
                probes=("probe-1",),
                bad_cases=("case-1",),
                analysis="候选未通过评测。",
            ),
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)

    await manager.reject_candidate(candidate, run_branch=prepared.branch)

    assert _git(bare, "rev-parse", prepared.branch) == base_commit
    assert _git(candidate.workspace_path, "branch", "--show-current") == prepared.branch
    remote_candidate_after = subprocess.run(
        ("git", "ls-remote", "--tags", str(bare), f"refs/tags/{candidate.ref}"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert remote_candidate_after == ""


async def test_prepare_rebuilds_a_mirror_when_the_target_repo_url_changes(
    tmp_path: Path,
) -> None:
    """One target id must never prepare new content from a stale repository cache."""
    old_bare = tmp_path / "old.git"
    new_bare = tmp_path / "new.git"
    for bare, marker in ((old_bare, "OLD"), (new_bare, "NEW")):
        seed = tmp_path / f"seed-{marker.lower()}"
        _git(tmp_path, "init", "--bare", str(bare))
        _git(tmp_path, "clone", str(bare), str(seed))
        _git(seed, "config", "user.name", "Test User")
        _git(seed, "config", "user.email", "test@example.invalid")
        target = seed / "skills" / "deep-research"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(f"# {marker}\n", encoding="utf-8")
        _git(seed, "add", ".")
        _git(seed, "commit", "-m", f"{marker} target")
        _git(seed, "tag", "v2.0.0")
        _git(seed, "push", "origin", "HEAD:refs/heads/main", "--tags")

    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    old_commit = _git(old_bare, "rev-parse", "refs/heads/main")
    new_commit = _git(new_bare, "rev-parse", "refs/heads/main")
    await manager.prepare(replace(_run(str(old_bare), old_commit), id="run-old"))

    prepared = await manager.prepare(replace(_run(str(new_bare), new_commit), id="run-new"))

    assert prepared.base_commit == new_commit
    assert (prepared.path / "skills" / "deep-research" / "SKILL.md").read_text() == "# NEW\n"
    assert _git(prepared.repo_path, "remote", "get-url", "origin") == str(new_bare)


async def test_disabled_push_performs_no_remote_mutation_for_any_candidate_stage(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Local-only mode must suppress prepare, candidate, accept, reject, and deletion writes."""
    bare, _seed, base_commit = remote_repo
    run = _run(str(bare), base_commit)
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
        push_enabled=False,
    )
    baseline_refs = set(_git(bare, "for-each-ref", "--format=%(refname)").splitlines())
    prepared = await manager.prepare(run)
    assert set(_git(bare, "for-each-ref", "--format=%(refname)").splitlines()) == baseline_refs

    trace = CandidateTrace(
        cluster_id="cluster-1",
        cluster_title="检索索引读取失败",
        probes=("probe-1",),
        bad_cases=("case-1",),
        analysis="本地模式。",
    )
    first = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("local accepted",)),
        message=build_candidate_message(
            run,
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(first, expected_remote_commit=None)
    await manager.accept_candidate(
        first,
        run_branch=prepared.branch,
        message=build_accepted_message(
            run,
            round_seq=1,
            trace=trace,
            patch_sha256="a" * 64,
            created_at=NOW,
            evaluated_commit=first.commit,
            current_score=80.0,
            candidate_score=85.0,
            reason="local accepted",
            web_base_url=None,
        ),
    )
    assert set(_git(bare, "for-each-ref", "--format=%(refname)").splitlines()) == baseline_refs

    second = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r2",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("local rejected",)),
        message=build_candidate_message(
            run,
            round_seq=2,
            trace=trace,
            patch_sha256="b" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(second, expected_remote_commit=None)
    cleanup_warning = await manager.reject_candidate(second, run_branch=prepared.branch)

    assert cleanup_warning is None
    assert set(_git(bare, "for-each-ref", "--format=%(refname)").splitlines()) == baseline_refs


async def test_candidate_publication_rejects_dirty_missing_trailer_and_divergent_history(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Publication validates the actual commit rather than trusting caller fields."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    (prepared.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitOperationError, match="clean"):
        await manager.create_candidate(
            prepared,
            run_branch=prepared.branch,
            candidate_ref=f"{prepared.branch}/candidate/r1",
            patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("x",)),
            message="candidate",
        )
    (prepared.path / "dirty.txt").unlink()

    with pytest.raises(GitOperationError, match="Harness-Run-Id"):
        await manager.create_candidate(
            prepared,
            run_branch=prepared.branch,
            candidate_ref=f"{prepared.branch}/candidate/r1",
            patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("x",)),
            message="candidate",
        )

    _git(prepared.path, "switch", "--orphan", "rogue")
    (prepared.path / "rogue.txt").write_text("rogue\n", encoding="utf-8")
    _git(prepared.path, "add", "rogue.txt")
    _git(
        prepared.path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "rogue\n\nHarness-Run-Id: run-m5-09",
    )
    from app.third_party_api.git.workspace import CandidateWorkspace, candidate_local_branch

    candidate_ref = f"{prepared.branch}/candidate/r1"
    local_branch = candidate_local_branch(candidate_ref)
    _git(prepared.path, "branch", "-m", local_branch)

    rogue = CandidateWorkspace(
        workspace_path=prepared.path,
        run_branch=prepared.branch,
        local_branch=local_branch,
        ref=candidate_ref,
        commit=_git(prepared.path, "rev-parse", "HEAD"),
        message="rogue\n\nHarness-Run-Id: run-m5-09",
        run_id="run-m5-09",
        base_commit=base_commit,
    )
    with pytest.raises(GitOperationError, match=r"ancestry|descend|base"):
        await manager.push_candidate(rogue, expected_remote_commit=None)


async def test_publication_validator_checks_clean_ancestry_and_trailer(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """The named push boundary rejects every unsafe snapshot before publication."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)

    (prepared.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitOperationError, match="clean"):
        await manager.validate_for_push(prepared, run)
    (prepared.path / "dirty.txt").unlink()

    with pytest.raises(GitOperationError, match="Harness-Run-Id"):
        await manager.validate_for_push(
            prepared,
            run,
            expected_run_id=run.id,
            require_trailer=True,
        )

    _git(prepared.path, "switch", "--orphan", "divergent")
    (prepared.path / "divergent.txt").write_text("divergent\n", encoding="utf-8")
    _git(prepared.path, "add", "divergent.txt")
    _git(
        prepared.path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "divergent\n\nHarness-Run-Id: run-m5-09",
    )
    _git(prepared.path, "branch", "-f", prepared.branch, "divergent")
    _git(prepared.path, "switch", prepared.branch)
    with pytest.raises(GitOperationError, match=r"ancestry|descend|base"):
        await manager.validate_for_push(prepared, run)


async def test_reject_returns_cleanup_warning_only_for_candidate_deletion_failure(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch restoration is required while remote candidate deletion is best effort."""
    bare, _seed, base_commit = remote_repo
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    run = _run(str(bare), base_commit)
    prepared = await manager.prepare(run)
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=f"{prepared.branch}/candidate/r1",
        patch=Patch(file="skills/deep-research/EVOLUTION.md", lines=("reject",)),
        message=build_candidate_message(
            run,
            round_seq=1,
            trace=CandidateTrace("c", "title", ("p",), ("b",), "analysis"),
            patch_sha256="a" * 64,
            created_at=NOW,
            web_base_url=None,
        ),
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)

    async def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise GitOperationError("delete denied", stage="push")

    monkeypatch.setattr(manager, "_delete_remote_tag", fail_delete)
    warning = await manager.reject_candidate(candidate, run_branch=prepared.branch)

    assert warning == "delete denied"
    assert _git(prepared.path, "branch", "--show-current") == prepared.branch


def test_commit_messages_are_traceable_and_probe_analysis_is_truncated(
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Messages retain IDs and trailers while bounded probe analysis stays readable."""
    bare, _seed, base_commit = remote_repo
    run = _run(str(bare), base_commit)
    trace = CandidateTrace(
        cluster_id="cluster-1",
        cluster_title="检索索引读取失败",
        probes=("probe-1",),
        bad_cases=("case-1",),
        analysis="\n".join(f"line {index}" for index in range(40)),
    )

    candidate = build_candidate_message(
        run,
        round_seq=2,
        trace=trace,
        patch_sha256="b" * 64,
        created_at=NOW,
        web_base_url=None,
    )
    accepted = build_accepted_message(
        run,
        round_seq=2,
        trace=trace,
        patch_sha256="b" * 64,
        created_at=NOW,
        evaluated_commit="c" * 40,
        current_score=70.0,
        candidate_score=79.0,
        reason="候选分 79.0 高于基线 70.0（+9.0），按 external-verdict 规则接纳",
        web_base_url=None,
    )

    assert candidate.splitlines()[0].endswith("候选变更")
    assert "Harness-Run-Id: run-m5-09" in candidate
    assert "Harness-Cluster-Id: cluster-1" in candidate
    assert "完整推导见归因链接" in candidate
    assert "line 30" not in candidate
    assert "https://" not in candidate
    assert "Harness-Evaluated-Commit: " + ("c" * 40) in accepted
    assert "为什么接纳" in accepted


def test_documented_git_push_environment_variable_disables_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented HE_GIT_PUSH switch must fail closed for all remote writes."""
    monkeypatch.setenv("HE_GIT_PUSH", "false")
    monkeypatch.setenv("HE_JWT_SECRET", "test-signing-secret")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    app = create_app(settings)

    assert settings.git_push is False
    assert app.state.git_workspaces._push_enabled is False
    assert app.state.evolution_candidates._workspaces._push_enabled is False


async def test_git_timeout_terminates_the_process_group_before_returning(
    tmp_path: Path,
) -> None:
    """A timed-out command cannot keep producing side effects during a later retry."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    marker = tmp_path / "late-side-effect"
    script = tmp_path / "slow-git"
    script.write_text(
        (
            "#!/bin/sh\n"
            'touch "$(dirname "$0")/process-started"\n'
            "trap '' HUP PIPE TERM\n"
            '(sleep 4; touch "$(dirname "$0")/late-side-effect") &\n'
            "wait\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    _git(repo, "config", "alias.delayed", f"!{script}")
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
        timeout_seconds=2.0,
    )

    with pytest.raises(GitOperationError, match="workspace operation failed"):
        await manager._git(repo, ("git", "delayed"), stage="workspace")
    await asyncio.sleep(4.5)

    assert (tmp_path / "process-started").exists()
    assert not marker.exists()


async def test_candidate_accept_returns_cleanup_result_type() -> None:
    """The acceptance boundary exposes cleanup warnings without another exception channel."""
    from app.third_party_api.git.workspace import CandidateAcceptance

    assert CandidateAcceptance.__name__ == "CandidateAcceptance"


async def test_immediate_push_retry_stops_on_success_and_never_retries_permanent_errors() -> None:
    """Only transient failures retry, with two five-second waits before success."""
    attempts = 0
    sleeps: list[float] = []

    async def flaky_push() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise GitOperationError("temporary", stage="push", retryable=True)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    completed = await retry_push(flaky_push, sleep=sleep)
    assert completed == 3
    assert sleeps == [5.0, 5.0]

    permanent_attempts = 0

    async def permanent_push() -> None:
        nonlocal permanent_attempts
        permanent_attempts += 1
        raise GitOperationError("denied", stage="push", retryable=False)

    with pytest.raises(GitOperationError, match="denied"):
        await retry_push(permanent_push, sleep=sleep)
    assert permanent_attempts == 1


def test_workspace_cleanup_keeps_active_and_newest_inactive_runs(tmp_path: Path) -> None:
    """Retention removes old inactive workspaces but never an active run."""
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    manager.workspaces_root.mkdir(parents=True)
    created: list[Path] = []
    for index in range(23):
        path = manager.workspaces_root / f"run-{index:02d}"
        path.mkdir()
        (path / "marker").touch(exist_ok=False)
        created.append(path)
    active = created[0]

    removed = manager.cleanup_workspaces(active_run_ids={active.name}, keep=20)

    assert active.exists()
    assert len(removed) == 2
    assert created[1] in removed
    assert created[2] in removed


async def test_skill_multifile_candidate_uses_branch_not_tag(
    tmp_path: Path,
    remote_repo: tuple[Path, Path, str],
) -> None:
    """Skill replacement/deletion publication does not create a candidate tag."""
    bare, _, base = remote_repo
    run = _run(str(bare), base)
    manager = GitWorkspaceManager(repos_root=tmp_path / "repos", workspaces_root=tmp_path / "work")
    prepared = await manager.prepare(run)
    ref = f"evo-candidate/{prepared.branch.removeprefix('evo/')}/r1"
    patch = Patch(
        file="skills/deep-research/SKILL.md",
        changes={
            "skills/deep-research/SKILL.md": "# Revised\n",
            "skills/deep-research/reference.md": "Evidence\n",
        },
    )
    candidate = await manager.create_candidate(
        prepared,
        run_branch=prepared.branch,
        candidate_ref=ref,
        patch=patch,
        message=f"candidate\n\nHarness-Run-Id: {run.id}",
        expected_run_id=run.id,
    )
    await manager.push_candidate(candidate, expected_remote_commit=None)
    assert _git(bare, "rev-parse", f"refs/heads/{ref}") == candidate.commit
    assert _git(bare, "tag", "--list", ref) == ""
    assert (prepared.path / patch.file).read_text() == "# Revised\n"
    snapshot = f"evo-snapshot/{run.id}/baseline"
    await manager.publish_skill_snapshot(prepared.path, branch=snapshot, commit=base)
    await manager.publish_skill_snapshot(prepared.path, branch=snapshot, commit=base)
    with pytest.raises(GitOperationError, match="immutable"):
        await manager.publish_skill_snapshot(
            prepared.path, branch=snapshot, commit=candidate.commit
        )
    await manager.reject_candidate(candidate, run_branch=prepared.branch)
    assert _git(bare, "branch", "--list", ref) == ""
    assert _git(bare, "rev-parse", f"refs/heads/{snapshot}") == base


def test_skill_multifile_patch_rejects_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "SKILL.md").symlink_to(outside)
    with pytest.raises(GitOperationError, match="symlink"):
        GitWorkspaceManager._write_patch(
            workspace, Patch(file="SKILL.md", changes={"SKILL.md": "bad"})
        )
    assert outside.read_text() == "unchanged"
