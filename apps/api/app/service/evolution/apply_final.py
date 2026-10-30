"""Apply strict evaluation improvements and independently finalize Skill runs."""

# ruff: noqa: RUF001

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeHitRow
from app.db.crud.skill_evolution import SkillCheckpointConflict, SkillEvolutionRepository
from app.db.models.evolution import EvolutionRoundRecord, EvolutionRunRecord
from app.db.models.jobs import JobRunRecord
from app.service.evolution.advance import build_candidate_trace, build_frozen_candidate_trace
from app.service.evolution.backoff import next_retry_at
from app.service.evolution.engine.contracts import EvalRequest, EvalResult, RunBlocked
from app.service.evolution.eval_poller import GatewayResolver
from app.service.evolution.events import EvolutionRunEventService
from app.service.evolution.git_candidate import (
    CandidateTrace,
    EvolutionCandidateService,
)
from app.service.evolution.git_failures import classify_git_failure
from app.third_party_api.evaluation.models import (
    AmbiguousEvaluationSubmission,
    InvalidEvaluationEvidence,
)
from app.third_party_api.git.workspace import (
    GitOperationError,
    GitWorkspaceManager,
    PreparedWorkspace,
)
from app.utils.redaction import sanitize_message
from app.utils.time import now_shanghai

logger = logging.getLogger(__name__)


class FinalEvaluationTerminalError(ValueError):
    """A terminal external attempt may be replaced by an explicitly retried attempt."""


class ProbeSourceReader(Protocol):
    """Load complete source rows for an evaluated round trace."""

    async def list_rows_by_ids(self, probe_ids: Sequence[str]) -> tuple[ProbeHitRow, ...]:
        """Return source evidence for selected probes."""


class EvolutionApplyService:
    """Apply one completed evaluation through the candidate publication boundary."""

    def __init__(
        self,
        runs: EvolutionRepository,
        candidates: EvolutionCandidateService,
        probes: ProbeSourceReader,
        *,
        events: EvolutionRunEventService | None = None,
        skill_states: SkillEvolutionRepository | None = None,
        now: Any = now_shanghai,
    ) -> None:
        """Bind durable evaluation, candidate publication, and trace evidence."""
        self._runs = runs
        self._candidates = candidates
        self._probes = probes
        self._events = events
        self._skill_states = skill_states
        self._now = now

    async def handle(self, job: JobRunRecord) -> Mapping[str, Any]:
        """Accept or reject one evaluated candidate exactly once."""
        round_id = str(
            job.payload.get("evolutionRoundId") or job.payload.get("evolution_round_id") or "",
        )
        if not round_id:
            raise ValueError("apply job requires an evolution round id")
        round_ = await self._runs.get_round(round_id)
        if round_ is None:
            raise LookupError("evolution round not found")
        run = await self._runs.get_run(round_.run_id)
        if run is None:
            raise LookupError("evolution run not found")
        if round_.status in {"accepted", "rejected"}:
            return {
                "runId": run.id,
                "roundId": round_.id,
                "status": round_.status,
                "applied": False,
                "reason": "round already decided",
            }
        if run.mode != "full" or run.status != "running":
            return {
                "runId": run.id,
                "roundId": round_.id,
                "status": run.status,
                "applied": False,
                "reason": "run is not ready to apply an evaluation",
            }
        if round_.eval_status != "completed" or round_.eval_result is None:
            raise ValueError("apply job requires a completed durable evaluation result")
        result = EvalResult.from_payload(round_.eval_result)
        trace = await self._trace(run, round_)
        accepted = result.candidate_score > result.current_score
        reason = decision_sentence(result, accepted=accepted)
        if accepted:
            decided = await self._candidates.accept(
                run,
                round_,
                trace,
                current_score=result.current_score,
                candidate_score=result.candidate_score,
                reason=reason,
                job_id=job.id,
                retry_attempts=_job_retry_attempts(job),
            )
        else:
            decided = await self._candidates.reject(
                run,
                round_,
                reason=reason,
                job_id=job.id,
            )
        return {
            "runId": run.id,
            "roundId": decided.id,
            "status": decided.status,
            "accepted": accepted,
            "reason": reason,
            "applied": True,
        }

    async def _trace(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
    ) -> CandidateTrace:
        """Reconstruct the complete immutable trace from persisted evidence."""
        cluster = next(
            (
                item
                for item in await self._runs.list_clusters(run.id)
                if item.probe_cluster_id == round_.probe_cluster_id
            ),
            None,
        )
        if cluster is None:
            raise LookupError("round cluster not found")
        links = tuple(
            link.probe_id
            for link in await self._runs.list_cluster_probes(run.id)
            if link.probe_cluster_id == cluster.probe_cluster_id
        )
        state = await self._skill_states.get(run.id) if self._skill_states is not None else None
        if state is not None:
            return build_frozen_candidate_trace(
                state,
                cluster.probe_cluster_id,
                cluster.probe_cluster_description,
                links,
            )
        rows = await self._probes.list_rows_by_ids(links)
        return build_candidate_trace(
            cluster.probe_cluster_id,
            cluster.probe_cluster_description,
            links,
            rows,
        )


class FeedbackAdvancer(Protocol):
    """Persist another validation iteration before the independent final test."""

    async def advance_if_needed(
        self,
        run: EvolutionRunRecord,
        rounds: Sequence[EvolutionRoundRecord],
    ) -> bool:
        """Return whether another durable iteration is now pending."""


class EvolutionFinalizeService:
    """Reevaluate baseline and accepted best, publish the branch, and finish the run."""

    def __init__(
        self,
        runs: EvolutionRepository,
        workspaces: GitWorkspaceManager,
        *,
        events: EvolutionRunEventService | None = None,
        gateway_for_run: GatewayResolver | None = None,
        feedback: FeedbackAdvancer | None = None,
        skill_states: SkillEvolutionRepository | None = None,
        poll_interval: timedelta = timedelta(minutes=5),
        timeout: timedelta = timedelta(hours=2),
        now: Any = now_shanghai,
    ) -> None:
        """Bind durable finals, git publication, and event recording."""
        self._runs = runs
        self._workspaces = workspaces
        self._events = events
        self._now = now
        self._gateway_for_run = gateway_for_run
        self._feedback = feedback
        self._skill_states = skill_states
        self._poll_interval = poll_interval
        self._timeout = timeout

    async def handle(self, job: JobRunRecord) -> Mapping[str, Any]:
        """Finalize only after every round reached a terminal decision."""
        run_id = job.ref_id or str(job.payload.get("runId") or "")
        run = await self._runs.get_run(run_id)
        if run is None:
            raise LookupError("evolution run not found")
        if run.mode != "full":
            return {
                "runId": run.id,
                "status": run.status,
                "finalized": False,
                "reason": "cluster-only runs do not have a final evaluation",
            }
        if run.status == "completed":
            return {
                "runId": run.id,
                "status": run.status,
                "finalized": False,
                "reason": "run is already complete",
            }
        if run.status != "running":
            return {
                "runId": run.id,
                "status": run.status,
                "finalized": False,
                "reason": "run is not ready to finalize",
            }
        rounds = await self._runs.list_rounds(run.id)
        if any(round_.status not in {"accepted", "rejected", "skipped"} for round_ in rounds):
            return {
                "runId": run.id,
                "status": run.status,
                "finalized": False,
                "reason": "rounds are still active",
            }
        retry_attempts = _job_retry_attempts(job)
        result = None
        real_skill = run.eval_config.get("skillEvolution") == "real"
        if real_skill:
            try:
                if self._gateway_for_run is None or self._skill_states is None:
                    raise ValueError("real Skill final evaluation dependencies are unavailable")
                if self._feedback is not None and await self._feedback.advance_if_needed(
                    run, rounds
                ):
                    return {
                        "runId": run.id,
                        "finalized": False,
                        "reason": "validation feedback scheduled another iteration",
                    }
                result = await self._evaluate_final(run, rounds)
            except Exception as exc:
                if isinstance(exc, FinalEvaluationTerminalError) and self._skill_states is not None:
                    state = await self._skill_states.get(run.id)
                    if state is not None:
                        previous = dict(state.checkpoint.get("final_evaluation", {}))
                        await self._save_final_state(
                            run.id,
                            {
                                "attempt": int(previous.get("attempt", 0)) + 1,
                                "previous_external_id": previous.get("external_id"),
                            },
                        )
                error_stage = (
                    "final_eval_submission_ambiguous"
                    if isinstance(exc, AmbiguousEvaluationSubmission)
                    else "final_eval"
                )
                message = sanitize_message(f"final evaluation failed: {exc}", limit=2_000)
                await self._runs.mark_blocked(
                    run.id,
                    stage="eval",
                    message=message,
                    error_stage=error_stage,
                    next_retry_at=None,
                    retry_attempts=retry_attempts,
                )
                raise RunBlocked(
                    message,
                    stage="eval",
                    error_stage=error_stage,
                    retryable=False,
                ) from None
            if result is None:
                return {"runId": run.id, "finalized": False, "reason": "final evaluation pending"}
        try:
            final = await self._runs.write_final(_final_values(run, rounds, result=result))
            prepared = _prepared_workspace(run)
        except Exception as exc:
            message = sanitize_message(str(exc), limit=2_000)
            await self._runs.mark_blocked(
                run.id,
                stage="push",
                message=message,
                error_stage="final",
                next_retry_at=None,
                retry_attempts=retry_attempts,
            )
            raise RunBlocked(
                message,
                stage="push",
                error_stage="final",
                next_retry_at=None,
                retry_attempts=retry_attempts,
                retryable=False,
            ) from None
        try:
            await self._workspaces.publish_run_branch(
                prepared,
                run,
                expected_run_id=run.id,
                require_trailer=any(round_.accepted for round_ in rounds),
            )
        except GitOperationError as exc:
            disposition = classify_git_failure(exc)
            message = sanitize_message(str(exc), limit=2_000)
            retry_at = next_retry_at(self._now(), retry_attempts) if exc.retryable else None
            await self._runs.mark_blocked(
                run.id,
                stage="push",
                message=message,
                error_stage=disposition.error_stage,
                next_retry_at=retry_at,
                retry_attempts=retry_attempts,
            )
            raise RunBlocked(
                message,
                stage="push",
                error_stage=disposition.error_stage,
                next_retry_at=retry_at,
                retry_attempts=retry_attempts,
                retryable=exc.retryable,
            ) from None
        push_status = "succeeded" if self._workspaces.push_enabled else "skipped"
        completed = await self._runs.complete_run(
            run.id,
            finished_at=self._now(),
            push_status=push_status,
        )
        await self._record(
            run.id,
            message="运行终评已完成",
            job_id=job.id,
            payload={
                "verdict": final.verdict,
                "bestVersion": final.best_version,
                "bestScore": final.best_score,
                "initScore": final.init_score,
                "pushStatus": push_status,
            },
        )
        return {
            "runId": completed.id,
            "status": completed.status,
            "finalized": True,
            "pushStatus": push_status,
        }

    async def _save_final_state(self, run_id: str, value: Mapping[str, Any]) -> None:
        """Merge final polling facts without overwriting other checkpoint owners."""
        if self._skill_states is None:
            raise ValueError("final evaluation requires durable Skill state")
        while True:
            state = await self._skill_states.get(run_id)
            if state is None:
                raise ValueError("Skill input snapshot not found")
            checkpoint = dict(state.checkpoint)
            checkpoint["final_evaluation"] = dict(value)
            try:
                await self._skill_states.save_checkpoint(
                    run_id,
                    checkpoint,
                    expected_revision=state.revision,
                )
                return
            except SkillCheckpointConflict:
                continue

    async def _evaluate_final(
        self,
        run: EvolutionRunRecord,
        rounds: Sequence[EvolutionRoundRecord],
    ) -> EvalResult | None:
        """Submit or poll independent baseline/best evaluation without waiting in a worker."""
        if self._skill_states is None or self._gateway_for_run is None:
            raise ValueError("final evaluation dependencies are missing")
        state = await self._skill_states.get(run.id)
        if state is None:
            raise ValueError("Skill input snapshot not found")
        pending = dict(state.checkpoint.get("final_evaluation", {}))
        if pending.get("result") is not None:
            return EvalResult.from_payload(pending["result"])
        if not self._workspaces.push_enabled:
            raise ValueError("real final evaluation requires published snapshot branches")
        current = self._now()
        gateway = self._gateway_for_run(run)
        if not pending.get("external_id"):
            accepted = [round_ for round_ in rounds if round_.accepted]
            best_commit = accepted[-1].commit_hash if accepted else run.base_commit
            if not best_commit or not run.base_commit or not run.workspace_path:
                raise ValueError("final evaluation requires immutable commits and workspace")
            best_ref = f"evo-snapshot/{run.id}/final-best"
            await self._workspaces.publish_skill_snapshot(
                Path(run.workspace_path),
                branch=best_ref,
                commit=best_commit,
            )
            submission = await gateway.submit(
                EvalRequest(
                    run_id=run.id,
                    evolution_round_id="final",
                    repo_url=run.repo_url,
                    ref=best_ref,
                    commit=best_commit,
                    target_type=run.target_type,
                    target_name=run.target_name,
                    target_version=run.target_version,
                    cluster_id="final",
                    idempotency_key=f"{run.id}:final:{best_commit}:{pending.get('attempt', 0)}",
                    baseline_ref=f"evo-snapshot/{run.id}/baseline",
                    baseline_commit=run.base_commit,
                )
            )
            if not submission.eval_id.strip():
                raise ValueError("final evaluation returned blank id")
            pending = {
                **pending,
                "external_id": submission.eval_id,
                "started_at": current.isoformat(),
                "next_poll_at": (current + self._poll_interval).isoformat(),
            }
            await self._save_final_state(run.id, pending)
            return None
        if current - datetime.fromisoformat(pending["started_at"]) >= self._timeout:
            raise FinalEvaluationTerminalError("final evaluation timed out")
        if current < datetime.fromisoformat(pending["next_poll_at"]):
            return None
        status = await gateway.get_status(pending["external_id"])
        if status.status in {"queued", "running"}:
            pending["next_poll_at"] = (current + self._poll_interval).isoformat()
            await self._save_final_state(run.id, pending)
            return None
        if status.status != "completed":
            raise FinalEvaluationTerminalError(
                status.message or f"final evaluation {status.status}"
            )
        try:
            result = await gateway.get_result(pending["external_id"])
        except (ValueError, InvalidEvaluationEvidence) as exc:
            raise FinalEvaluationTerminalError(str(exc)) from exc
        pending["result"] = result.to_payload()
        await self._save_final_state(run.id, pending)
        return result

    async def _record(
        self,
        run_id: str,
        *,
        message: str,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Record best-effort final evidence after the run is durably complete."""
        if self._events is None:
            return
        try:
            await self._events.record(
                run_id,
                "final_completed",
                level="info",
                stage="final",
                message=message,
                job_id=job_id,
                payload=payload,
            )
        except Exception:
            logger.exception(
                "failed to record final completion event",
                extra={"run_id": run_id},
            )


def _job_retry_attempts(job: JobRunRecord) -> int:
    """Read the retry counter a recovery job carries into finalization."""
    raw = job.payload.get("retryAttempts")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def decision_sentence(result: EvalResult, *, accepted: bool) -> str:
    """Translate the strict score comparison into one human-readable rule."""
    delta = result.candidate_score - result.current_score
    if delta == 0.0:
        relation = "等于"
    elif delta > 0:
        relation = "高于"
    else:
        relation = "低于"
    verdict = "接纳" if accepted else "拒绝"
    return (
        f"候选分 {result.candidate_score:.1f} {relation}基线 "
        f"{result.current_score:.1f}（{delta:+.1f}），"
        f"按严格增分规则{verdict}"
    )


def _final_values(
    run: EvolutionRunRecord,
    rounds: Sequence[EvolutionRoundRecord],
    *,
    result: EvalResult | None = None,
) -> Mapping[str, Any]:
    """Build deterministic final metrics from completed round evaluations."""
    final_result = result
    evaluated = [
        (round_, EvalResult.from_payload(round_.eval_result))
        for round_ in rounds
        if round_.eval_result is not None
    ]
    initial = evaluated[0][1].current_score if evaluated else 0.0
    accepted_scores = [result.candidate_score for round_, result in evaluated if round_.accepted]
    best = max((initial, *accepted_scores))
    metrics = []
    for round_, result in evaluated:
        metrics.append(
            {
                "round": round_.seq,
                "currentScore": result.current_score,
                "candidateScore": result.candidate_score,
                "delta": result.candidate_score - result.current_score,
                "accepted": round_.accepted,
                "candidateCommit": round_.candidate_commit,
                "finalCommit": round_.commit_hash,
            },
        )
    if final_result is not None:
        initial = final_result.current_score
        best = final_result.candidate_score
        metrics.append({"phase": "independent_final", **final_result.to_payload()})
    return {
        "run_id": run.id,
        "best_version": run.target_version,
        "verdict": bool(accepted_scores) and best > initial,
        "best_score": best,
        "init_score": initial,
        "metrics": metrics,
    }


def _prepared_workspace(run: EvolutionRunRecord) -> PreparedWorkspace:
    """Reconstruct the prepared workspace snapshot persisted by m5-09."""
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


__all__ = [
    "EvolutionApplyService",
    "EvolutionFinalizeService",
    "decision_sentence",
]
