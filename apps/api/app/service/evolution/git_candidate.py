"""Traceable candidate messages and git-backed round publication."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import urlsplit

from app.db.crud.evolution import EvolutionRepository
from app.db.models.evolution import EvolutionRoundRecord, EvolutionRunRecord
from app.service.evolution.backoff import next_retry_at
from app.service.evolution.engine.contracts import RunBlocked
from app.service.evolution.events import (
    EvolutionRunEventLevel,
    EvolutionRunEventService,
    EvolutionRunEventStage,
    EvolutionRunEventType,
)
from app.service.evolution.git_failures import classify_git_failure
from app.service.evolution.skill_core import SkillPatchRejected
from app.service.evolution.state import StageConflict
from app.third_party_api.git.workspace import (
    CandidateWorkspace,
    GitOperationError,
    GitWorkspaceManager,
    Patch,
    PreparedWorkspace,
    candidate_local_branch,
    retry_push,
)
from app.utils.redaction import redact_text, sanitize_message

_HTML_TAG = re.compile(r"<[^>]+>")
_MAX_SUBJECT = 72
_MAX_ANALYSIS_LINES = 30
_MAX_ANALYSIS_CHARS = 3_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CandidateSource:
    """One probe and its related bad cases and trace destination."""

    probe_id: str
    bad_case_ids: tuple[str, ...]
    session_id: str | None = None
    description: str = ""
    analysis: str = ""


@dataclass(frozen=True, slots=True)
class CandidateTrace:
    """Human-readable facts shared by candidate and accepted messages."""

    cluster_id: str
    cluster_title: str
    probes: tuple[str, ...]
    bad_cases: tuple[str, ...]
    analysis: str
    sources: tuple[CandidateSource, ...] = ()
    patch_file: str = ".harness-evolution.md"


def build_candidate_message(
    run: EvolutionRunRecord,
    *,
    round_seq: int,
    trace: CandidateTrace,
    patch_sha256: str,
    created_at: datetime,
    web_base_url: str | None,
) -> str:
    """Build a hypothesis-shaped candidate commit with stable machine trailers."""
    title = _safe_single_line(trace.cluster_title)
    subject = _bounded_subject(
        f"evolve({run.target_name}@{run.target_version}): 针对「{title}」的候选变更",
    )
    sections = _message_sections(run, trace, web_base_url=web_base_url)
    sections.insert(
        4,
        "## 状态\n\n候选变更，尚未评测；评测完成后本消息会被 amend 改写为终态结论。",
    )
    trailers = _trailers(
        run,
        round_seq=round_seq,
        trace=trace,
        patch_sha256=patch_sha256,
        created_at=created_at,
    )
    return "\n\n".join((subject, *sections, "---\n" + trailers))


def build_accepted_message(
    run: EvolutionRunRecord,
    *,
    round_seq: int,
    trace: CandidateTrace,
    patch_sha256: str,
    created_at: datetime,
    evaluated_commit: str,
    current_score: float,
    candidate_score: float,
    reason: str,
    web_base_url: str | None,
) -> str:
    """Build the assertion-shaped accepted message while preserving shared facts."""
    title = _safe_single_line(trace.cluster_title)
    subject = _bounded_subject(
        f"evolve({run.target_name}@{run.target_version}): 修复{title}",
    )
    sections = _message_sections(run, trace, web_base_url=web_base_url)
    sections.insert(4, f"## 为什么接纳\n\n{_safe_single_line(reason)}")
    delta = candidate_score - current_score
    trailers = _trailers(
        run,
        round_seq=round_seq,
        trace=trace,
        patch_sha256=patch_sha256,
        created_at=created_at,
    )
    trailers = "\n".join(
        (
            trailers,
            f"Harness-Evaluated-Commit: {evaluated_commit}",
            (
                f"Harness-Verdict: accepted "
                f"({candidate_score:.1f} vs {current_score:.1f}, {delta:+.1f})"
            ),
            (
                "Harness-Decision-Rule: strict-case-total-score-mean"
                if run.eval_config.get("skillEvolution") == "real"
                else "Harness-Decision-Rule: external-verdict"
            ),
        ),
    )
    return "\n\n".join((subject, *sections, "---\n" + trailers))


def extract_trailer(message: str, name: str) -> str | None:
    """Read one exact machine trailer from a commit message."""
    prefix = f"{name}: "
    for line in reversed(message.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _candidate_created_at(
    round_: EvolutionRoundRecord,
    fallback: datetime,
) -> datetime:
    """Reuse the evaluated candidate timestamp so accepted retries rebuild identically."""
    raw = extract_trailer(round_.candidate_message or "", "Harness-Created-At")
    if raw is None:
        return fallback
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return fallback


class EvolutionCandidateService:
    """Publish, accept, and reject one durable candidate round."""

    def __init__(
        self,
        runs: EvolutionRepository,
        workspaces: GitWorkspaceManager,
        *,
        events: EvolutionRunEventService | None = None,
        skill_core: Any = None,
        skill_states: Any = None,
        web_base_url: str | None = None,
        push_interval_seconds: float = 5.0,
        push_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind durable state, git operations, timing, and trace links."""
        self._runs = runs
        self._skill_core = skill_core
        self._skill_states = skill_states
        self._workspaces = workspaces
        self._events = events
        self._web_base_url = web_base_url
        self._push_interval_seconds = push_interval_seconds
        self._push_attempts = push_attempts
        self._sleep = sleep
        self._now = now or (lambda: datetime.now().astimezone())

    async def publish(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        trace: CandidateTrace,
        *,
        job_id: str | None = None,
    ) -> EvolutionRoundRecord:
        """Create, commit, and push one candidate with bounded immediate retries."""
        if round_.status in {"evaluating", "deciding", "accepted", "rejected", "skipped"}:
            return round_
        if run.target_type == "skill" and self._skill_core is None:
            existing_state = (
                await self._skill_states.get(run.id) if self._skill_states is not None else None
            )
            if run.eval_config.get("skillEvolution") == "real" or existing_state is not None:
                raise ValueError("Real Skill model is unavailable; refusing placeholder fallback")
        prepared = _prepared_workspace(run)
        current = await self._runs.mark_round_patching(
            round_.id,
            started_at=self._now(),
        )
        patch_payload = None
        if (
            run.target_type == "skill"
            and self._skill_core is not None
            and (
                run.eval_config.get("skillEvolution") == "real"
                or (
                    self._skill_states is not None
                    and await self._skill_states.get(run.id) is not None
                )
            )
        ):
            if self._skill_states is None:
                raise ValueError("Skill state repository is missing")
            state = await self._skill_states.get(run.id)
            if state is None:
                raise ValueError("Skill input snapshot is missing")
            from app.service.evolution.skill_identity import skill_identity

            checkpoint = state.checkpoint
            if checkpoint.get("identity") != skill_identity(self._skill_core):
                raise ValueError("Skill model or strategy changed since preparation")
            cluster = checkpoint["cluster_map"][current.probe_cluster_id]
            reports = [
                item
                for item in checkpoint["plan"]["canonical_reports"]
                if item["probe_id"] in cluster["probe_ids"]
            ]
            try:
                skill_patch = await self._skill_core.patch(
                    cluster, reports, dict(checkpoint["best_files"])
                )
            except SkillPatchRejected as exc:
                # Provider transport failures are not caught: only invalid proposals reject.
                return await self._runs.reject_invalid_patch(
                    current.id,
                    reason=exc.reason,
                    finished_at=self._now(),
                )
            prefix = "" if run.sub_dir in ("", ".") else run.sub_dir.rstrip("/") + "/"
            changes = {prefix + name: content for name, content in skill_patch.changes.items()}
            patch = Patch(file=next(iter(changes)), changes=changes)
            accepted = [
                item
                for item in await self._runs.list_rounds(run.id)
                if item.status == "accepted" and item.commit_hash
            ]
            baseline_commit = (
                accepted[-1].commit_hash if accepted else None
            ) or prepared.base_commit
            baseline_ref = f"evo-snapshot/{run.id}/r{current.seq}-baseline"
            await self._workspaces.publish_skill_snapshot(
                prepared.path,
                branch=baseline_ref,
                commit=baseline_commit,
            )
            patch_payload = {
                "file": patch.file,
                "changes": changes,
                "files": skill_patch.files,
                "edit_patch": skill_patch.edit_patch,
                "sha256": patch.sha256,
                "baseline_ref": baseline_ref,
                "baseline_commit": baseline_commit,
            }
        else:
            patch = placeholder_patch(run, current.seq)
        candidate_ref = (
            f"evo-candidate/{prepared.branch.removeprefix('evo/')}/r{current.seq}"
            if patch.changes is not None
            else f"{prepared.branch}/candidate/r{current.seq}"
        )
        message = build_candidate_message(
            run,
            round_seq=current.seq,
            trace=_with_patch_file(trace, patch.file),
            patch_sha256=patch.sha256,
            created_at=self._now(),
            web_base_url=self._web_base_url,
        )
        try:
            candidate = await self._workspaces.create_candidate(
                prepared,
                run_branch=prepared.branch,
                candidate_ref=candidate_ref,
                patch=patch,
                message=message,
                expected_run_id=run.id,
            )
        except GitOperationError as exc:
            await self._handle_git_failure(
                run.id,
                exc,
                job_id=job_id,
            )
        current = await self._runs.mark_round_candidate(
            current.id,
            candidate_ref=candidate.ref,
            candidate_commit=candidate.commit,
            candidate_message=candidate.message,
            patch=patch_payload or {"file": patch.file, "lines": list(patch.lines)},
        )
        await self._record(
            run.id,
            "candidate_committed",
            stage="candidate",
            message=f"候选提交已创建：第 {current.seq} 轮",
            round_id=current.id,
            job_id=job_id,
            payload={
                "files": list(patch.changes or {patch.file: None}),
                "patchHash": f"sha256:{patch.sha256}",
            },
        )

        if not self._workspaces.push_enabled:
            return await self._runs.mark_round_and_run_push_skipped(
                run.id,
                current.id,
            )

        attempt_counter = 0

        async def attempt_push() -> None:
            nonlocal attempt_counter
            attempt_counter += 1
            await self._runs.mark_round_pushing(
                current.id,
                attempt=attempt_counter,
            )
            await self._record(
                run.id,
                "push_started",
                stage="push",
                message=f"候选推送第 {attempt_counter} 次",
                round_id=current.id,
                job_id=job_id,
                payload={"attempt": attempt_counter},
            )
            await self._workspaces.push_candidate(
                candidate,
                expected_remote_commit=None,
            )

        try:
            attempts = await retry_push(
                attempt_push,
                sleep=self._sleep or _default_sleep,
                on_retry=lambda attempt, _error: self._record(
                    run.id,
                    "push_retry_scheduled",
                    stage="push",
                    message=f"候选推送将在 {self._push_interval_seconds:g} 秒后重试",
                    round_id=current.id,
                    job_id=job_id,
                    payload={"attempt": attempt},
                ),
                interval_seconds=self._push_interval_seconds,
                attempts=self._push_attempts,
            )
        except GitOperationError as exc:
            await self._runs.mark_round_push_failed(
                run.id,
                current.id,
                attempts=attempt_counter,
                error=sanitize_message(str(exc), limit=2_000),
            )
            await self._record(
                run.id,
                "push_failed",
                stage="push",
                message="候选推送失败",
                round_id=current.id,
                job_id=job_id,
                level="error",
                payload={
                    "attempts": attempt_counter,
                    "ref": current.candidate_ref,
                },
                error=exc,
            )
            retry_at = next_retry_at(self._now(), 0) if exc.retryable else None
            await self._runs.mark_blocked(
                run.id,
                stage="push",
                message="candidate push failed",
                next_retry_at=retry_at,
                retry_attempts=0,
            )
            raise RunBlocked(
                "candidate push failed",
                stage="push",
                next_retry_at=retry_at,
                retry_attempts=0,
                retryable=exc.retryable,
            ) from None

        pushed = await self._runs.mark_round_push_succeeded(
            run.id,
            current.id,
            pushed_at=self._now(),
            attempts=attempts,
        )
        await self._record(
            run.id,
            "candidate_pushed",
            stage="push",
            message="候选已推送，等待评测",
            round_id=pushed.id,
            job_id=job_id,
            payload={"ref": pushed.candidate_ref, "attempts": attempts},
        )
        await self._record(
            run.id,
            "push_succeeded",
            stage="push",
            message="候选推送成功",
            round_id=pushed.id,
            job_id=job_id,
        )
        return await self._runs.mark_round_evaluating(pushed.id)

    async def repush_existing(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        *,
        job_id: str | None = None,
        retry_attempts: int = 0,
    ) -> EvolutionRoundRecord:
        """Push one already-committed candidate exactly once and re-enter evaluation."""
        if (
            round_.candidate_ref is None
            or round_.candidate_commit is None
            or not round_.candidate_message
        ):
            raise ValueError("candidate push retry requires a persisted candidate")
        if not self._workspaces.push_enabled:
            raise ValueError("push-disabled runs cannot resume a candidate push")
        prepared = _prepared_workspace(run)
        candidate = CandidateWorkspace(
            workspace_path=prepared.path,
            run_branch=prepared.branch,
            local_branch=candidate_local_branch(round_.candidate_ref),
            ref=round_.candidate_ref,
            commit=round_.candidate_commit,
            message=round_.candidate_message,
            run_id=run.id,
            base_commit=prepared.base_commit,
        )
        attempt = round_.push_attempts + 1
        pushing = await self._runs.mark_round_pushing(round_.id, attempt=attempt)
        await self._record(
            run.id,
            "push_started",
            stage="push",
            message=f"候选推送第 {attempt} 次",
            round_id=pushing.id,
            job_id=job_id,
            payload={"attempt": attempt},
        )
        try:
            await self._workspaces.push_candidate(
                candidate,
                expected_remote_commit=None,
            )
        except GitOperationError as exc:
            await self._runs.mark_round_push_failed(
                run.id,
                pushing.id,
                attempts=attempt,
                error=sanitize_message(str(exc), limit=2_000),
            )
            await self._record(
                run.id,
                "push_failed",
                stage="push",
                message="候选推送失败",
                round_id=pushing.id,
                job_id=job_id,
                level="error",
                payload={"attempts": attempt, "ref": pushing.candidate_ref},
                error=exc,
            )
            await self._handle_git_failure(
                run.id,
                exc,
                job_id=job_id,
                retry_attempts=retry_attempts,
            )
        pushed = await self._runs.mark_round_push_succeeded(
            run.id,
            pushing.id,
            pushed_at=self._now(),
            attempts=attempt,
        )
        await self._record(
            run.id,
            "candidate_pushed",
            stage="push",
            message="候选已推送，等待评测",
            round_id=pushed.id,
            job_id=job_id,
            payload={"ref": pushed.candidate_ref, "attempts": attempt},
        )
        await self._record(
            run.id,
            "push_succeeded",
            stage="push",
            message="候选推送成功",
            round_id=pushed.id,
            job_id=job_id,
        )
        return await self._runs.mark_round_evaluating(pushed.id)

    async def accept(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        trace: CandidateTrace,
        *,
        current_score: float,
        candidate_score: float,
        reason: str,
        job_id: str | None = None,
        retry_attempts: int = 0,
    ) -> EvolutionRoundRecord:
        """Amend accepted metadata, fast-forward the run branch, and persist it."""
        if round_.status == "accepted":
            return round_
        if round_.status not in {"evaluating", "deciding"}:
            raise StageConflict(
                status_code=409,
                current=0,
                target=1,
                reason="accepted rounds must be decided from the deciding state",
            )
        if round_.candidate_ref is None or round_.candidate_commit is None:
            raise ValueError("accepted rounds require a pushed candidate")
        evaluated_commit = round_.candidate_commit
        deciding = await self._runs.mark_round_deciding(round_.id)
        prepared = _prepared_workspace(run)
        candidate = CandidateWorkspace(
            workspace_path=prepared.path,
            run_branch=prepared.branch,
            local_branch=candidate_local_branch(deciding.candidate_ref or ""),
            ref=deciding.candidate_ref or "",
            commit=evaluated_commit,
            message=deciding.candidate_message or "",
            run_id=run.id,
            base_commit=prepared.base_commit,
        )
        patch_hash = _patch_hash(deciding)
        message = build_accepted_message(
            run,
            round_seq=deciding.seq,
            trace=_with_patch_file(trace, _patch_file(deciding)),
            patch_sha256=patch_hash,
            created_at=_candidate_created_at(deciding, self._now()),
            evaluated_commit=evaluated_commit,
            current_score=current_score,
            candidate_score=candidate_score,
            reason=reason,
            web_base_url=self._web_base_url,
        )
        try:
            final = await self._workspaces.accept_candidate(
                candidate,
                run_branch=prepared.branch,
                message=message,
            )
        except GitOperationError as exc:
            await self._handle_git_failure(
                run.id,
                exc,
                job_id=job_id,
                retry_attempts=retry_attempts,
            )
        accepted = await self._runs.accept_round(
            deciding.id,
            final_commit=final.commit,
            final_message=final.message,
            finished_at=self._now(),
        )
        await self._record(
            run.id,
            "cluster_accepted",
            stage="apply",
            message=reason,
            cluster_id=trace.cluster_id,
            round_id=accepted.id,
            job_id=job_id,
            payload={
                "reason": reason,
                "finalCommit": final.commit,
                "cleanupError": final.cleanup_error,
            },
        )
        return accepted

    async def reject(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        *,
        reason: str,
        job_id: str | None = None,
    ) -> EvolutionRoundRecord:
        """Delete rejected candidate refs and retain the run branch unchanged."""
        if round_.status == "rejected":
            return round_
        if round_.status not in {"evaluating", "deciding"}:
            raise StageConflict(
                status_code=409,
                current=0,
                target=1,
                reason="rejected rounds must be decided from the deciding state",
            )
        if round_.candidate_ref is None or round_.candidate_commit is None:
            raise ValueError("rejected rounds require a pushed candidate")
        deciding = await self._runs.mark_round_deciding(round_.id)
        prepared = _prepared_workspace(run)
        candidate = CandidateWorkspace(
            workspace_path=prepared.path,
            run_branch=prepared.branch,
            local_branch=candidate_local_branch(deciding.candidate_ref or ""),
            ref=deciding.candidate_ref or "",
            commit=deciding.candidate_commit or "",
            message=deciding.candidate_message or "",
            run_id=run.id,
            base_commit=prepared.base_commit,
        )
        cleanup_error = await self._workspaces.reject_candidate(
            candidate,
            run_branch=prepared.branch,
        )
        rejected = await self._runs.reject_round(
            deciding.id,
            finished_at=self._now(),
        )
        await self._record(
            run.id,
            "cluster_rejected",
            stage="apply",
            message=reason,
            round_id=rejected.id,
            job_id=job_id,
            payload={"reason": reason, "cleanupError": cleanup_error},
        )
        return rejected

    async def _record(
        self,
        run_id: str,
        event_type: EvolutionRunEventType,
        *,
        stage: EvolutionRunEventStage,
        message: str,
        round_id: str,
        job_id: str | None,
        cluster_id: str | None = None,
        level: EvolutionRunEventLevel = "info",
        payload: Mapping[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Record a best-effort event without masking the git transaction."""
        if self._events is None:
            return
        try:
            await self._events.record(
                run_id,
                event_type,
                level=level,
                stage=stage,
                cluster_id=cluster_id,
                round_id=round_id,
                job_id=job_id,
                message=message,
                payload=payload,
                error=error,
            )
        except Exception:
            logger.exception(
                "failed to record candidate event",
                extra={"run_id": run_id, "event_type": event_type},
            )

    async def _handle_git_failure(
        self,
        run_id: str,
        error: GitOperationError,
        *,
        job_id: str | None,
        retry_attempts: int = 0,
    ) -> NoReturn:
        """Apply the shared Git disposition and raise only when it is retryable."""
        disposition = classify_git_failure(error)
        if disposition.action != "blocked" or disposition.blocked_stage is None:
            raise error from None
        message = sanitize_message(str(error), limit=2_000)
        retry_at = (
            next_retry_at(self._now(), retry_attempts)
            if disposition.blocked_stage == "push" and error.retryable
            else None
        )
        await self._runs.mark_blocked(
            run_id,
            stage=disposition.blocked_stage,
            message=message,
            error_stage=disposition.error_stage,
            next_retry_at=retry_at,
            retry_attempts=retry_attempts,
        )
        del job_id
        raise RunBlocked(
            message,
            stage=disposition.blocked_stage,
            error_stage=disposition.error_stage,
            next_retry_at=retry_at,
            retry_attempts=retry_attempts,
            retryable=error.retryable,
        ) from None


def _message_sections(
    run: EvolutionRunRecord,
    trace: CandidateTrace,
    *,
    web_base_url: str | None,
) -> list[str]:
    """Build the common issue, analysis, change, and trace sections."""
    title = _safe_single_line(trace.cluster_title)
    case_list = ", ".join(_unique(trace.bad_cases)) or "未关联"
    analysis = _bounded_analysis(trace)
    modified = PurePosixPath(trace.patch_file).as_posix()
    return [
        (
            "## 问题\n\n"
            f"{run.target_name}@{run.target_version} 的聚类「{title}」包含需要处理的来源问题："
            f"{case_list}。"
        ),
        f"## 分析\n\n{analysis}",
        (f"## 改动\n\n- {modified}（V1 占位 patch；真实文件已写入 isolated workspace）"),
        "## 追溯\n\n" + _trace_lines(run, trace, web_base_url=web_base_url),
    ]


def _trace_lines(
    run: EvolutionRunRecord,
    trace: CandidateTrace,
    *,
    web_base_url: str | None,
) -> str:
    """Render run, cluster, probe, and bad-case identities without secret material."""
    base = _safe_base_url(web_base_url)
    run_link = f"{base}/evolution/runs/{run.id}?step=iteration" if base else run.id
    cluster_link = (
        f"{base}/evolution/runs/{run.id}?step=clusters&cluster={trace.cluster_id}"
        if base
        else trace.cluster_id
    )
    lines = [f"- 自进化运行：{run_link}", f"- 问题簇：{cluster_link}"]
    source_by_probe = {source.probe_id: source for source in trace.sources}
    for probe_id in _unique(trace.probes):
        source = source_by_probe.get(probe_id)
        if source is None:
            lines.append(f"    - 根因归因：{probe_id}")
            continue
        if base and source.session_id and source.bad_case_ids:
            probe_link = (
                f"{base}/sessions/{source.session_id}/errors?case={source.bad_case_ids[0]}&tab=attr"
            )
        else:
            probe_link = probe_id
        lines.append(
            f"    - 根因归因：{probe_link}（{_safe_single_line(source.description)}）",
        )
    for bad_case_id in _unique(trace.bad_cases):
        session_id = next(
            (
                source.session_id
                for source in trace.sources
                if bad_case_id in source.bad_case_ids and source.session_id
            ),
            None,
        )
        case_link = (
            f"{base}/sessions/{session_id}/errors?case={bad_case_id}&tab=desc"
            if base and session_id
            else bad_case_id
        )
        lines.append(f"    - 来源问题：{case_link}")
    if run.base_commit:
        lines.append(f"- 基线：`{run.base_commit}`（{run.target_version}）")
    return "\n".join(lines)


def _trailers(
    run: EvolutionRunRecord,
    *,
    round_seq: int,
    trace: CandidateTrace,
    patch_sha256: str,
    created_at: datetime,
) -> str:
    """Render the fixed final trailer block used by push validation and traceability."""
    probe_ids = ", ".join(_unique(trace.probes)) or "none"
    bad_case_ids = ", ".join(_unique(trace.bad_cases)) or "none"
    return "\n".join(
        (
            f"Harness-Run-Id: {run.id}",
            f"Harness-Cluster-Id: {trace.cluster_id}",
            f"Harness-Round: {round_seq}",
            f"Harness-Probes: {probe_ids}",
            f"Harness-Bad-Cases: {bad_case_ids}",
            f"Harness-Base-Commit: {run.base_commit or ''}",
            f"Harness-Patch-Hash: sha256:{patch_sha256}",
            f"Harness-Created-At: {created_at.isoformat()}",
        ),
    )


def _bounded_analysis(trace: CandidateTrace) -> str:
    """Strip active HTML and cap analysis by both line and character budgets."""
    raw = trace.analysis
    if not raw and trace.sources:
        raw = "\n\n".join(source.analysis for source in trace.sources if source.analysis.strip())
    normalized = _HTML_TAG.sub("", html.unescape(raw)).replace("\r\n", "\n").replace("\r", "\n")
    normalized = redact_text(normalized).strip()
    lines = normalized.splitlines()
    truncated = len(lines) > _MAX_ANALYSIS_LINES or len(normalized) > _MAX_ANALYSIS_CHARS
    if len(lines) > _MAX_ANALYSIS_LINES:
        normalized = "\n".join(lines[:_MAX_ANALYSIS_LINES])
    if len(normalized) > _MAX_ANALYSIS_CHARS:
        normalized = normalized[:_MAX_ANALYSIS_CHARS]
    if not normalized:
        normalized = "V1 使用占位分析，详细推导见来源问题与运行详情。"
    if truncated:
        normalized += "\n\n…完整推导见归因链接。"
    return normalized


def _bounded_subject(subject: str) -> str:
    """Bound one subject to the commit convention's 72-character budget."""
    normalized = _safe_single_line(subject)
    if len(normalized) <= _MAX_SUBJECT:
        return normalized
    return normalized[: _MAX_SUBJECT - 1].rstrip() + "…"


def _safe_single_line(value: str) -> str:
    """Redact controls from externally sourced commit-message fragments."""
    return redact_text(" ".join(value.split()))


def _safe_base_url(value: str | None) -> str | None:
    """Accept only credential-free HTTP(S) base URLs for trace markdown."""
    if not value:
        return None
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return normalized


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    """Preserve first occurrence while removing duplicate trace identifiers."""
    return tuple(dict.fromkeys(value for value in values if value))


def placeholder_patch(run: EvolutionRunRecord, round_seq: int) -> Patch:
    """Return a deterministic patch that is still written and committed for real."""
    sub_dir = PurePosixPath(run.sub_dir or ".")
    relative = (
        PurePosixPath(".harness-evolution") / f"candidate-r{round_seq}.md"
        if sub_dir == PurePosixPath(".")
        else sub_dir / ".harness-evolution" / f"candidate-r{round_seq}.md"
    )
    return Patch(
        file=relative.as_posix(),
        lines=(
            f"# Harness Evolution candidate r{round_seq}",
            "",
            f"- Run: {run.id}",
            f"- Target: {run.target_name}@{run.target_version}",
            "- This V1 patch is a deterministic placeholder applied to an isolated workspace.",
        ),
    )


def _prepared_workspace(run: EvolutionRunRecord) -> PreparedWorkspace:
    """Reconstruct immutable workspace facts persisted during prepare."""
    if (
        run.workspace_path is None
        or run.repo_path is None
        or run.branch_name is None
        or run.base_commit is None
    ):
        raise ValueError("run has no prepared workspace")
    return PreparedWorkspace(
        repo_path=Path(run.repo_path),
        path=Path(run.workspace_path),
        ref=run.base_ref or run.target_version,
        base_commit=run.base_commit,
        branch=run.branch_name,
    )


def _with_patch_file(trace: CandidateTrace, file: str) -> CandidateTrace:
    """Return the trace with its actual applied patch destination."""
    return CandidateTrace(
        cluster_id=trace.cluster_id,
        cluster_title=trace.cluster_title,
        probes=trace.probes,
        bad_cases=trace.bad_cases,
        analysis=trace.analysis,
        sources=trace.sources,
        patch_file=file,
    )


def _patch_file(round_: EvolutionRoundRecord) -> str:
    """Read the applied file from one stored patch."""
    if not isinstance(round_.patch, Mapping):
        return ".harness-evolution.md"
    value = round_.patch.get("file")
    return value if isinstance(value, str) and value else ".harness-evolution.md"


def _patch_hash(round_: EvolutionRoundRecord) -> str:
    """Recompute the stored patch digest for accepted-message reproduction."""
    file = _patch_file(round_)
    if isinstance(round_.patch, Mapping) and isinstance(round_.patch.get("changes"), dict):
        return Patch(file="SKILL.md", changes=dict(round_.patch["changes"])).sha256
    raw_lines = round_.patch.get("lines") if isinstance(round_.patch, Mapping) else None
    lines = (
        tuple(line for line in raw_lines if isinstance(line, str))
        if isinstance(raw_lines, (list, tuple))
        else ()
    )
    return Patch(file=file, lines=lines).sha256


async def _default_sleep(seconds: float) -> None:
    """Wait between immediate push attempts."""
    await asyncio.sleep(seconds)
