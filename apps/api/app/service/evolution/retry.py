"""Recovery dispatch for blocked evolution runs."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol, TypeVar, cast

from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeHitRow
from app.db.crud.skill_evolution import SkillEvolutionRepository
from app.db.models.evolution import EvolutionRoundRecord, EvolutionRunRecord
from app.db.models.jobs import JobRunRecord
from app.errors import AppError, ErrorCode
from app.service.evolution.advance import build_candidate_trace, build_frozen_candidate_trace
from app.service.evolution.backoff import next_retry_delay
from app.service.evolution.engine.contracts import EvolutionBlockedStage, RunBlocked
from app.service.evolution.events import (
    EvolutionRunEventService,
    EvolutionRunEventStage,
)
from app.service.evolution.git_candidate import (
    CandidateTrace,
    EvolutionCandidateService,
)
from app.service.identity.schemas import IdentityModel
from app.utils.redaction import sanitize_message
from app.utils.time import now_shanghai

logger = logging.getLogger(__name__)

_TERMINAL_ROUND_STATUSES = frozenset({"accepted", "rejected", "skipped", "failed"})
_BLOCKED_STAGE_TO_EVENT_STAGE: dict[str, EvolutionRunEventStage] = {
    "patch": "candidate",
    "push": "push",
    "eval": "eval",
}
_HandlerT = TypeVar("_HandlerT")


class RetryJobSubmitter(Protocol):
    """Queue seam used to admit one manual or follow-up recovery job."""

    async def submit(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        ref_id: str | None,
        created_by: str | None,
    ) -> JobSubmissionView:
        """Persist and schedule one durable job."""


class JobSubmissionView(Protocol):
    """The stable subset of one queued-or-reused job submission."""

    @property
    def id(self) -> str:
        """Return the durable job identifier."""

    @property
    def reused(self) -> bool:
        """Return whether an already-active job was reused."""


class StageHandler(Protocol):
    """One existing stage service callable from retry dispatch."""

    async def handle(self, job: JobRunRecord) -> Mapping[str, Any]:
        """Execute one durable stage job."""


class EvalResubmitter(Protocol):
    """Submit a cleared evaluation round and return its new external identity."""

    async def submit(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        *,
        previous_external_id: str | None = None,
    ) -> EvolutionRoundRecord:
        """Persist the new external evaluation id and poll deadline."""


class ProbeSourceReader(Protocol):
    """Load complete probe rows for a regenerated candidate trace."""

    async def list_rows_by_ids(self, probe_ids: Sequence[str]) -> tuple[ProbeHitRow, ...]:
        """Return complete source rows for selected probe identifiers."""


class EvolutionRetryAccepted(IdentityModel):
    """Asynchronous acceptance of one manual blocked-run retry."""

    job_id: str
    reused: bool


class EvolutionRetryService:
    """Resume one blocked run exactly once, then hand off the next real stage."""

    def __init__(
        self,
        runs: EvolutionRepository,
        candidates: EvolutionCandidateService,
        jobs: RetryJobSubmitter,
        *,
        apply: StageHandler | None = None,
        finalize: StageHandler | None = None,
        advance: StageHandler | None = None,
        eval_submitter: EvalResubmitter | None = None,
        probes: ProbeSourceReader | None = None,
        events: EvolutionRunEventService | None = None,
        skill_states: SkillEvolutionRepository | None = None,
        now: Any = now_shanghai,
    ) -> None:
        """Bind durable state, stage services, queueing, and trace evidence."""
        self._runs = runs
        self._candidates = candidates
        self._jobs = jobs
        self._apply_service = apply
        self._finalize_service = finalize
        self._advance_service = advance
        self._eval_submitter = eval_submitter
        self._probes = probes
        self._events = events
        self._skill_states = skill_states
        self._now = now

    async def request_retry(
        self,
        run_id: str,
        created_by: str | None,
    ) -> EvolutionRetryAccepted:
        """Admit one manual retry after restarting the run's backoff ladder."""
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AppError(
                status_code=404,
                code=ErrorCode.RUN_NOT_FOUND,
                message="evolution run not found",
            )
        if run.status != "blocked":
            raise AppError(
                status_code=409,
                code=ErrorCode.RUN_NOT_RETRYABLE,
                message="run is not blocked, so it cannot be retried",
                details={"status": run.status},
            )
        if (run.error_stage or "").endswith("_submission_ambiguous"):
            raise AppError(
                status_code=409,
                code=ErrorCode.RUN_NOT_RETRYABLE,
                message="evaluation submission requires provider reconciliation before retry",
            )
        await self._runs.reset_manual_retry(run.id)
        submission = await self._jobs.submit(
            kind="evolution_retry",
            payload={"runId": run.id, "automatic": False},
            ref_id=run.id,
            created_by=created_by,
        )
        return EvolutionRetryAccepted(
            job_id=str(submission.id),
            reused=bool(submission.reused),
        )

    async def handle(self, job: JobRunRecord) -> Mapping[str, Any]:
        """Resume the blocked stage and raise ``RunBlocked`` with retry facts."""
        run_id = job.ref_id or str(job.payload.get("runId") or "")
        run = await self._runs.get_run(run_id)
        if run is None:
            raise LookupError("evolution run not found")
        automatic = job.payload.get("automatic") is True
        if run.status != "blocked" or run.blocked_stage is None:
            return {
                "runId": run.id,
                "resumed": False,
                "status": run.status,
                "reason": "run is not blocked",
            }
        if (run.error_stage or "").endswith("_submission_ambiguous"):
            raise RunBlocked(
                "evaluation submission requires provider reconciliation before retry",
                stage="eval",
                error_stage=run.error_stage,
                retryable=False,
            )
        blocked_stage = cast(EvolutionBlockedStage, run.blocked_stage)
        if automatic and blocked_stage != "push":
            return {
                "runId": run.id,
                "resumed": False,
                "status": run.status,
                "reason": "only push-stage backoff is automatic",
            }
        attempts = run.retry_attempts
        if automatic:
            attempts += 1
            run = await self._runs.increment_retry_attempts(run.id)

        event_stage = _BLOCKED_STAGE_TO_EVENT_STAGE[blocked_stage]
        cleared_round: EvolutionRoundRecord | None = None
        previous_eval_id: str | None = None
        final_eval = blocked_stage == "eval" and run.error_stage == "final_eval"
        if blocked_stage == "eval" and not final_eval:
            failed = await self._failed_eval_round(run.id)
            if failed is None:
                raise RuntimeError("blocked evaluation round not found")
            previous_eval_id = failed.eval_external_id
            cleared_round = await self._runs.reset_round_for_eval_retry(
                run.id,
                failed.id,
            )
        await self._resume(run.id, stage=event_stage, job_id=job.id)
        refreshed = await self._runs.get_run(run.id)
        if refreshed is not None:
            run = refreshed
        try:
            if final_eval:
                dispatched = await self._finalize(run, job)
            elif blocked_stage == "eval":
                if cleared_round is None:
                    raise RuntimeError("evaluation retry round was not prepared")
                dispatched = await self._resubmit_eval(
                    run,
                    cleared_round,
                    previous_eval_id,
                    job,
                )
            else:
                dispatched = await self._dispatch(run, blocked_stage, job)
        except RunBlocked as exc:
            raise self._with_backoff(attempts, exc) from None
        except Exception as exc:
            message = sanitize_message(f"retry failed: {exc}", limit=2_000)
            raise self._with_backoff(
                attempts,
                RunBlocked(message, stage=blocked_stage, retryable=False),
            ) from None
        result: dict[str, Any] = {
            "runId": run.id,
            "resumed": True,
            "stage": blocked_stage,
        }
        result.update(dispatched)
        return result

    async def _dispatch(
        self,
        run: EvolutionRunRecord,
        blocked_stage: EvolutionBlockedStage,
        job: JobRunRecord,
    ) -> Mapping[str, Any]:
        """Choose the single recovery action that matches durable on-disk state."""
        if blocked_stage == "patch":
            active = await self._active_round(run.id)
            if active is None:
                return await self._schedule_next(run, job)
            return await self._regenerate_round(run, active, job)
        final = await self._runs.get_final(run.id)
        if final is not None:
            return await self._finalize(run, job)
        evaluated = await self._undecided_evaluated_round(run.id)
        if evaluated is not None:
            return await self._apply(run, evaluated, job)
        active = await self._active_round(run.id)
        if active is not None:
            if active.status in {"pushing", "evaluating"}:
                return await self._republish_push(run, active, job)
            return await self._regenerate_round(run, active, job)
        rounds = await self._runs.list_rounds(run.id)
        if rounds and all(round_.status in _TERMINAL_ROUND_STATUSES for round_ in rounds):
            return await self._finalize(run, job)
        return await self._schedule_next(run, job)

    async def _resubmit_eval(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        previous_eval_id: str | None,
        job: JobRunRecord,
    ) -> Mapping[str, Any]:
        """Submit a cleared round under a fresh external id."""
        submitter = self._require_stage(self._eval_submitter, "eval submitter")
        submitted = await submitter.submit(
            run,
            round_,
            previous_external_id=previous_eval_id,
        )
        return {
            "roundId": submitted.id,
            "status": submitted.status,
            "evalExternalId": submitted.eval_external_id,
            "previousEvalExternalId": previous_eval_id,
            "jobId": job.id,
        }

    async def _republish_push(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        job: JobRunRecord,
    ) -> Mapping[str, Any]:
        """Push the persisted candidate once, then submit its evaluation."""
        pushed = await self._candidates.repush_existing(
            run,
            round_,
            job_id=job.id,
            retry_attempts=run.retry_attempts,
        )
        if pushed.status == "evaluating" and pushed.eval_external_id is None:
            submitter = self._require_stage(self._eval_submitter, "eval submitter")
            pushed = await submitter.submit(run, pushed)
        return {"roundId": pushed.id, "status": pushed.status}

    async def _regenerate_round(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        job: JobRunRecord,
    ) -> Mapping[str, Any]:
        """Regenerate one patching round through the standard publication seam."""
        if round_.status == "pending" and self._advance_service is not None:
            return await self._advance_service.handle(job)
        trace = await self._trace(run, round_)
        current = await self._candidates.publish(
            run,
            round_,
            trace,
            job_id=job.id,
        )
        if current.status == "evaluating" and current.eval_external_id is None:
            submitter = self._require_stage(self._eval_submitter, "eval submitter")
            current = await submitter.submit(run, current)
        return {"roundId": current.id, "status": current.status}

    async def _apply(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        job: JobRunRecord,
    ) -> Mapping[str, Any]:
        """Continue a completed evaluation through apply, never re-evaluating."""
        handler = self._require_stage(self._apply_service, "apply")
        applied = await handler.handle(
            replace(
                job,
                payload={
                    **job.payload,
                    "runId": run.id,
                    "evolutionRoundId": round_.id,
                    "retryAttempts": run.retry_attempts,
                },
            ),
        )
        scheduled = await self._schedule_next(run, job)
        result = dict(applied)
        result.update(scheduled)
        return result

    async def _finalize(
        self,
        run: EvolutionRunRecord,
        job: JobRunRecord,
    ) -> Mapping[str, Any]:
        """Continue a final-stage push failure without re-running iterations."""
        handler = self._require_stage(self._finalize_service, "finalize")
        return await handler.handle(
            replace(
                job,
                payload={
                    **job.payload,
                    "runId": run.id,
                    "retryAttempts": run.retry_attempts,
                },
            ),
        )

    async def _schedule_next(
        self,
        run: EvolutionRunRecord,
        job: JobRunRecord,
    ) -> Mapping[str, Any]:
        """Queue the stage that follows a successful non-terminal recovery."""
        rounds = await self._runs.list_rounds(run.id)
        if not rounds:
            kind = "evolution_prepare"
        elif any(round_.status == "pending" for round_ in rounds):
            kind = "evolution_advance"
        elif all(round_.status in _TERMINAL_ROUND_STATUSES for round_ in rounds):
            kind = "evolution_finalize"
        else:
            return {}
        try:
            await self._jobs.submit(
                kind=kind,
                payload={"runId": run.id},
                ref_id=run.id,
                created_by=job.created_by,
            )
        except AppError as exc:
            if exc.code != ErrorCode.RUN_BUSY:
                logger.exception(
                    "failed to schedule the next stage after retry",
                    extra={"run_id": run.id, "kind": kind},
                )
            return {}
        except Exception:
            logger.exception(
                "failed to schedule the next stage after retry",
                extra={"run_id": run.id, "kind": kind},
            )
            return {}
        return {"scheduled": kind}

    async def _undecided_evaluated_round(
        self,
        run_id: str,
    ) -> EvolutionRoundRecord | None:
        """Find one evaluated round whose accept/reject decision is still missing."""
        for round_ in await self._runs.list_rounds(run_id):
            if round_.status in {"evaluating", "deciding"} and round_.eval_result is not None:
                return round_
        return None

    async def _failed_eval_round(
        self,
        run_id: str,
    ) -> EvolutionRoundRecord | None:
        """Find the evaluation round that caused blocked(eval)."""
        for round_ in await self._runs.list_rounds(run_id):
            if round_.status == "evaluating" and round_.eval_status in {"failed", "timeout"}:
                return round_
        return None

    async def _active_round(self, run_id: str) -> EvolutionRoundRecord | None:
        """Return the first round that has not reached a terminal decision."""
        for round_ in await self._runs.list_rounds(run_id):
            if round_.status not in _TERMINAL_ROUND_STATUSES:
                return round_
        return None

    async def _trace(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
    ) -> CandidateTrace:
        """Rebuild immutable candidate evidence for a regenerated patch."""
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
        if self._probes is None:
            raise RuntimeError("retry has no probe evidence reader")
        rows = await self._probes.list_rows_by_ids(links)
        return build_candidate_trace(
            cluster.probe_cluster_id,
            cluster.probe_cluster_description,
            links,
            rows,
        )

    def _require_stage(self, handler: _HandlerT | None, name: str) -> _HandlerT:
        """Reject a recovery path whose stage service was not composed."""
        if handler is None:
            raise RuntimeError(f"retry has no {name} stage handler")
        return handler

    async def _resume(
        self,
        run_id: str,
        *,
        stage: EvolutionRunEventStage,
        job_id: str,
    ) -> None:
        """Clear blocked state and publish ``run.resumed`` before continuing."""
        if self._events is not None:
            await self._events.record_resumed(run_id, stage=stage, job_id=job_id)
        else:
            await self._runs.mark_resumed(run_id)

    def _with_backoff(self, attempts: int, exc: RunBlocked) -> RunBlocked:
        """Attach the automatic backoff deadline, only for transient push failures."""
        delay = next_retry_delay(attempts) if exc.retryable and exc.stage == "push" else None
        exc.next_retry_at = None if delay is None else self._now() + delay
        exc.retry_attempts = max(attempts, exc.retry_attempts)
        return exc


__all__ = [
    "EvolutionRetryAccepted",
    "EvolutionRetryService",
]
