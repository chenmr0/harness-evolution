"""Durable external-evaluation polling for the placeholder evolution loop."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.db.crud.evolution import EvolutionRepository
from app.db.models.evolution import EvolutionRoundRecord, EvolutionRunRecord
from app.errors import AppError, ErrorCode
from app.service.evolution.engine.contracts import EvalGateway, EvalRequest, RunBlocked
from app.service.evolution.events import EvolutionRunEventService
from app.third_party_api.evaluation.models import AmbiguousEvaluationSubmission
from app.utils.redaction import sanitize_message, sanitize_value
from app.utils.time import now_shanghai

logger = logging.getLogger(__name__)


class EvalJobSubmitter(Protocol):
    """Follow-up job queue used when an evaluation reaches a terminal state."""

    async def submit(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        ref_id: str | None,
        created_by: str | None,
    ) -> object:
        """Persist and schedule one durable job."""


type GatewayResolver = Callable[[EvolutionRunRecord], EvalGateway]


class EvolutionEvalSubmitter:
    """Submit one published candidate using the run's immutable gateway snapshot."""

    def __init__(
        self,
        runs: EvolutionRepository,
        gateway_for_run: GatewayResolver,
        *,
        events: EvolutionRunEventService | None,
        now: Callable[[], datetime] = now_shanghai,
        poll_interval: timedelta = timedelta(minutes=5),
    ) -> None:
        """Bind persistence, gateway resolution, and the durable poll cadence."""
        if poll_interval.total_seconds() <= 0:
            raise ValueError("evaluation poll interval must be positive")
        self._runs = runs
        self._gateway_for_run = gateway_for_run
        self._events = events
        self._now = now
        self._poll_interval = poll_interval

    async def submit(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        *,
        previous_external_id: str | None = None,
    ) -> EvolutionRoundRecord:
        """Submit once, preserving an existing external id across restart."""
        if round_.eval_external_id is not None:
            return round_
        if (
            run.mode != "full"
            or run.status != "running"
            or round_.status != "evaluating"
            or round_.push_status != "succeeded"
            or round_.candidate_ref is None
            or round_.candidate_commit is None
        ):
            return round_
        gateway = self._gateway_for_run(run)
        try:
            submission = await gateway.submit(
                EvalRequest(
                    run_id=run.id,
                    evolution_round_id=round_.id,
                    repo_url=run.repo_url,
                    ref=round_.candidate_ref,
                    commit=round_.candidate_commit,
                    target_type=run.target_type,
                    target_name=run.target_name,
                    target_version=run.target_version,
                    cluster_id=round_.probe_cluster_id,
                    idempotency_key=(
                        f"{run.id}:{round_.seq}:{round_.candidate_commit}:{round_.eval_attempts}"
                    ),
                    baseline_ref=(round_.patch or {}).get("baseline_ref"),
                    baseline_commit=(round_.patch or {}).get("baseline_commit"),
                ),
            )
            if not submission.eval_id.strip():
                raise ValueError("evaluation submission returned a blank id")
        except Exception as exc:
            safe = sanitize_message(
                f"evaluation submission failed: {exc}",
                limit=2_000,
            )
            await self._runs.mark_eval_failed(
                run.id,
                round_.id,
                status="failed",
                error=safe,
                finished_at=self._now(),
            )
            await self._record(
                run.id,
                "eval_failed",
                level="error",
                message=safe,
                round_id=round_.id,
                payload={"ref": round_.candidate_ref, "commit": round_.candidate_commit},
                error=exc,
            )
            ambiguous = isinstance(exc, AmbiguousEvaluationSubmission)
            if ambiguous:
                await self._runs.mark_blocked(
                    run.id,
                    stage="eval",
                    message=safe,
                    error_stage="eval_submission_ambiguous",
                    next_retry_at=None,
                    retry_attempts=0,
                )
            raise RunBlocked(
                safe,
                stage="eval",
                error_stage="eval_submission_ambiguous" if ambiguous else "eval",
                retryable=not ambiguous,
            ) from None
        current = self._now()
        submitted = await self._runs.mark_eval_submitted(
            run.id,
            round_.id,
            external_id=submission.eval_id,
            started_at=current,
            next_poll_at=current + self._poll_interval,
        )
        payload: dict[str, Any] = {
            "externalId": submission.eval_id,
            "ref": round_.candidate_ref,
            "commit": round_.candidate_commit,
        }
        if previous_external_id is not None:
            payload["previousExternalId"] = previous_external_id
        await self._record(
            run.id,
            "eval_submitted",
            level="info",
            message=f"已提交评测 {submission.eval_id}",
            round_id=round_.id,
            payload=payload,
        )
        return submitted

    async def _record(
        self,
        run_id: str,
        event_type: str,
        *,
        level: str,
        message: str,
        round_id: str,
        payload: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Persist a best-effort submission event without losing the eval row."""
        if self._events is None:
            return
        try:
            await self._events.record(
                run_id,
                event_type,  # type: ignore[arg-type]
                level=level,  # type: ignore[arg-type]
                stage="eval",
                message=message,
                round_id=round_id,
                payload=payload,
                error=error,
            )
        except Exception:
            logger.exception(
                "failed to record evaluation submission event",
                extra={"run_id": run_id},
            )


class EvolutionEvalPoller:
    """Poll due evaluations without occupying the global JobRunner worker."""

    def __init__(
        self,
        runs: EvolutionRepository,
        gateway_for_run: GatewayResolver,
        jobs: EvalJobSubmitter,
        *,
        events: EvolutionRunEventService | None,
        now: Callable[[], datetime] = now_shanghai,
        scan_interval: timedelta = timedelta(seconds=30),
        poll_interval: timedelta = timedelta(minutes=5),
        timeout: timedelta = timedelta(hours=2),
        batch_size: int = 100,
    ) -> None:
        """Bind persistence, gateway resolution, scheduling, and timing."""
        if scan_interval.total_seconds() <= 0:
            raise ValueError("evaluation scan interval must be positive")
        if poll_interval.total_seconds() <= 0:
            raise ValueError("evaluation poll interval must be positive")
        if timeout.total_seconds() <= 0:
            raise ValueError("evaluation timeout must be positive")
        if batch_size < 1:
            raise ValueError("evaluation batch size must be positive")
        self._runs = runs
        self._gateway_for_run = gateway_for_run
        self._jobs = jobs
        self._events = events
        self._now = now
        self._scan_interval = scan_interval
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._batch_size = batch_size
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the process-local lightweight loop once."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run_forever(), name="evolution-eval-poller")

    async def stop(self) -> None:
        """Stop the loop and await task cleanup."""
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_forever(self) -> None:
        """Scan timers forever until graceful shutdown."""
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("evaluation poller scan failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._scan_interval.total_seconds(),
                )
            except TimeoutError:
                continue

    async def run_once(self) -> int:
        """Process one durable due batch and return the number of acted-on rounds."""
        current = self._now()
        due = await self._runs.list_eval_due(current, limit=self._batch_size)
        acted_on: set[tuple[str, str, str | None]] = set()
        for round_ in due:
            try:
                if await self._poll_round(round_, current):
                    acted_on.add(("round", round_.id, None))
            except Exception as exc:
                logger.exception(
                    "evaluation round poll failed",
                    extra={"round_id": round_.id, "run_id": round_.run_id},
                )
                run = await self._runs.get_run(round_.run_id)
                if run is not None:
                    try:
                        await self._fail(
                            run,
                            round_,
                            status="failed",
                            message=f"evaluation poll failed: {exc}",
                            current=current,
                            error=exc,
                        )
                    except Exception:
                        logger.exception(
                            "failed to isolate malformed evaluation round",
                            extra={"round_id": round_.id, "run_id": round_.run_id},
                        )

        for round_ in await self._runs.list_eval_followups(limit=self._batch_size):
            run = await self._runs.get_run(round_.run_id)
            if run is None:
                continue
            if await self._ensure_followup(
                run_id=run.id,
                kind="evolution_apply",
                payload={"evolutionRoundId": round_.id},
                created_by=None,
            ):
                acted_on.add(("round", round_.id, None))

        for run in await self._runs.list_running_full_runs(limit=self._batch_size):
            rounds = await self._runs.list_rounds(run.id)
            active = {
                "patching",
                "pushing",
                "evaluating",
                "deciding",
                "failed",
            }
            if any(round_.status in active for round_ in rounds):
                continue
            pending = any(round_.status == "pending" for round_ in rounds)
            terminal = all(
                round_.status in {"accepted", "rejected", "skipped"} for round_ in rounds
            )
            if not pending and not terminal:
                continue
            kind = "evolution_advance" if pending else "evolution_finalize"
            if await self._ensure_followup(
                run_id=run.id,
                kind=kind,
                payload={"runId": run.id},
                created_by=run.created_by,
            ):
                acted_on.add(("run", run.id, kind))
        return len(acted_on)

    async def _poll_round(
        self,
        round_: EvolutionRoundRecord,
        current: datetime,
    ) -> bool:
        """Poll one round unless it is no longer active or has timed out."""
        run = await self._runs.get_run(round_.run_id)
        if (
            run is None
            or run.mode != "full"
            or run.status != "running"
            or run.push_status == "skipped"
            or round_.eval_external_id is None
        ):
            return False
        if round_.eval_started_at is not None and current - round_.eval_started_at >= self._timeout:
            await self._fail(
                run,
                round_,
                status="timeout",
                message="evaluation timed out",
                current=current,
            )
            return True
        try:
            status = await self._gateway_for_run(run).get_status(
                round_.eval_external_id,
            )
            if status.status not in {"queued", "running", "completed", "failed", "timeout"}:
                raise ValueError(f"unknown evaluation status: {status.status}")
        except Exception as exc:
            await self._fail(
                run,
                round_,
                status="failed",
                message=f"evaluation status request failed: {exc}",
                current=current,
                error=exc,
            )
            return True
        if status.status in {"queued", "running"}:
            next_poll = current + self._poll_interval
            await self._runs.mark_eval_waiting(
                round_.id,
                next_poll_at=next_poll,
            )
            await self._record(
                run.id,
                "eval_polled",
                level="info",
                message=f"评测仍在 {status.status}",
                round_id=round_.id,
                payload={
                    "externalId": round_.eval_external_id,
                    "status": status.status,
                    "nextPollAt": next_poll.isoformat(),
                },
            )
            return True
        if status.status == "completed":
            try:
                result = await self._gateway_for_run(run).get_result(
                    round_.eval_external_id,
                )
                result_payload = sanitize_value(result.to_payload())
                if not isinstance(result_payload, Mapping):
                    raise ValueError("sanitized evaluation result is invalid")
                saved = await self._runs.save_eval_result(
                    run.id,
                    round_.id,
                    result=result_payload,
                    finished_at=current,
                )
            except Exception as exc:
                await self._fail(
                    run,
                    round_,
                    status="failed",
                    message=f"evaluation result request failed: {exc}",
                    current=current,
                    error=exc,
                )
                return True
            if not saved:
                return False
            await self._record(
                run.id,
                "eval_completed",
                level="info",
                message="评测完成，等待应用结果",
                round_id=round_.id,
                payload={
                    "externalId": round_.eval_external_id,
                    **result_payload,
                },
            )
            return True
        await self._fail(
            run,
            round_,
            status=status.status,
            message=status.message or f"evaluation {status.status}",
            current=current,
        )
        return True

    async def _fail(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        *,
        status: str,
        message: str,
        current: datetime,
        error: BaseException | None = None,
    ) -> None:
        """Persist one external failure and its two user-facing events."""
        safe = sanitize_message(message, limit=2_000)
        changed = await self._runs.mark_eval_failed(
            run.id,
            round_.id,
            status=status,
            error=safe,
            finished_at=current,
        )
        if not changed:
            return
        event_type = "eval_timeout" if status == "timeout" else "eval_failed"
        await self._record(
            run.id,
            event_type,
            level="error",
            message=safe,
            round_id=round_.id,
            payload={"externalId": round_.eval_external_id, "status": status},
            error=error,
        )
        if self._events is not None:
            await self._events.record_blocked(
                run.id,
                stage="eval",
                blocked_stage="eval",
                message=safe,
                retry_attempts=0,
                error=error,
            )

    async def _ensure_followup(
        self,
        *,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any],
        created_by: str | None,
    ) -> bool:
        """Create a missing durable follow-up while tolerating an already-active stage."""
        try:
            await self._jobs.submit(
                kind=kind,
                payload=payload,
                ref_id=run_id,
                created_by=created_by,
            )
            return True
        except AppError as exc:
            if exc.code == ErrorCode.RUN_BUSY:
                return False
            logger.exception(
                "failed to create evaluation follow-up job",
                extra={"run_id": run_id, "kind": kind},
            )
            return False
        except Exception:
            logger.exception(
                "failed to create evaluation follow-up job",
                extra={"run_id": run_id, "kind": kind},
            )
            return False

    async def _record(
        self,
        run_id: str,
        event_type: str,
        *,
        level: str,
        message: str,
        round_id: str,
        payload: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Record one best-effort poller event after durable state is committed."""
        if self._events is None:
            return
        try:
            await self._events.record(
                run_id,
                event_type,  # type: ignore[arg-type]
                level=level,  # type: ignore[arg-type]
                stage="eval",
                message=message,
                round_id=round_id,
                payload=payload,
                error=error,
            )
        except Exception:
            logger.exception(
                "failed to record evaluation poller event",
                extra={"run_id": run_id, "event_type": event_type},
            )
