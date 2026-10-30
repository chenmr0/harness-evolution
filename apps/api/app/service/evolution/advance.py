"""Production orchestration for the first real candidate round."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeHitRow
from app.db.models.jobs import JobRunRecord
from app.service.evolution.git_candidate import (
    CandidateSource,
    CandidateTrace,
    EvolutionCandidateService,
)

logger = logging.getLogger(__name__)


class JobSubmitter(Protocol):
    """Queue operation used by terminal lifecycle orchestration."""

    async def submit(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        ref_id: str | None,
        created_by: str | None,
    ) -> object:
        """Persist and schedule one job."""


class ProbeSourceReader(Protocol):
    """Probe/case facts needed to build one candidate trace."""

    async def list_rows_by_ids(self, probe_ids: Sequence[str]) -> tuple[ProbeHitRow, ...]:
        """Return complete source rows for selected probe identifiers."""


class EvalSubmitter(Protocol):
    """Submit one published candidate to its frozen external gateway."""

    async def submit(
        self,
        run: Any,
        round_: Any,
    ) -> Any:
        """Persist the external evaluation id and first poll deadline."""


class WorkspaceLifecycleManager(Protocol):
    """Workspace operations used for retention and candidate scheduling."""

    supports_candidate_lifecycle: bool

    def cleanup_workspaces(
        self,
        *,
        active_run_ids: set[str],
        keep: int = 20,
    ) -> tuple[Path, ...]:
        """Delete only inactive workspaces beyond the retention window."""


class EvolutionAdvanceService:
    """Turn the first pending prepared round into a durable candidate."""

    def __init__(
        self,
        runs: EvolutionRepository,
        candidates: EvolutionCandidateService,
        probes: ProbeSourceReader,
        eval_submitter: EvalSubmitter | None = None,
        *,
        skill_states: Any = None,
    ) -> None:
        """Bind durable rounds, candidate publication, and source evidence."""
        self._runs = runs
        self._candidates = candidates
        self._probes = probes
        self._eval_submitter = eval_submitter
        self._skill_states = skill_states

    async def handle(self, job: JobRunRecord) -> Mapping[str, Any]:
        """Publish exactly one pending round and leave evaluation to m5-10."""
        run_id = job.ref_id or str(job.payload.get("runId") or "")
        run = await self._runs.get_run(run_id)
        if run is None:
            raise LookupError("evolution run not found")
        if run.mode != "full" or run.status != "running":
            return {
                "runId": run.id,
                "status": run.status,
                "published": False,
                "reason": "run is not ready for candidate advancement",
            }
        rounds = await self._runs.list_rounds(run.id)
        target = None
        for round_ in rounds:
            if round_.status in {"accepted", "rejected", "skipped"}:
                continue
            if round_.status != "pending":
                return {
                    "runId": run.id,
                    "roundId": round_.id,
                    "status": run.status,
                    "published": False,
                    "reason": f"earlier round is {round_.status}",
                }
            target = round_
            break
        if target is None:
            return {
                "runId": run.id,
                "status": run.status,
                "published": False,
                "reason": "no pending round",
            }
        claimed = await self._runs.claim_round_for_advance(
            target.id,
            started_at=datetime.now().astimezone(),
        )
        if not claimed:
            return {
                "runId": run.id,
                "roundId": target.id,
                "status": run.status,
                "published": False,
                "reason": "pending round was already claimed",
            }
        cluster = next(
            (
                item
                for item in await self._runs.list_clusters(run.id)
                if item.probe_cluster_id == target.probe_cluster_id
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
            trace = build_frozen_candidate_trace(
                state,
                cluster.probe_cluster_id,
                cluster.probe_cluster_description,
                links,
            )
        else:
            rows = await self._probes.list_rows_by_ids(links)
            trace = build_candidate_trace(
                cluster.probe_cluster_id,
                cluster.probe_cluster_description,
                links,
                rows,
            )
        current = await self._candidates.publish(
            run,
            target,
            trace,
            job_id=job.id,
        )
        if (
            self._eval_submitter is not None
            and current.status == "evaluating"
            and current.eval_external_id is None
        ):
            current = await self._eval_submitter.submit(run, current)
        return {
            "runId": run.id,
            "roundId": current.id,
            "status": current.status,
            "candidateRef": current.candidate_ref,
            "published": True,
        }


class EvolutionJobLifecycle:
    """Connect completed preparation to candidate work and workspace retention."""

    def __init__(
        self,
        runs: EvolutionRepository,
        jobs: JobSubmitter,
        workspaces: WorkspaceLifecycleManager,
        *,
        before_terminal: Callable[[JobRunRecord], Awaitable[None]] | None = None,
        keep_workspaces: int = 20,
        schedule_candidate_advance: bool = True,
    ) -> None:
        """Bind durable run state, job scheduling, cleanup, and prior hooks."""
        self._runs = runs
        self._jobs = jobs
        self._workspaces = workspaces
        self._before_terminal = before_terminal
        self._keep_workspaces = keep_workspaces
        self._schedule_candidate_advance = schedule_candidate_advance

    async def on_terminal(self, job: JobRunRecord) -> None:
        """Schedule the next real stage and enforce the workspace retention policy."""
        if self._before_terminal is not None:
            await self._before_terminal(job)
        if (
            self._schedule_candidate_advance
            and job.kind in {"evolution_prepare", "evolution_advance", "evolution_apply"}
            and job.status == "completed"
            and job.ref_id
        ):
            run = await self._runs.get_run(job.ref_id)
            if run is not None and run.mode == "full" and run.status == "running":
                rounds = await self._runs.list_rounds(run.id)
                active = {
                    "patching",
                    "pushing",
                    "evaluating",
                    "deciding",
                    "failed",
                }
                if not any(round_.status in active for round_ in rounds):
                    pending = next(
                        (round_ for round_ in rounds if round_.status == "pending"),
                        None,
                    )
                    kind = "evolution_advance" if pending is not None else "evolution_finalize"
                    await self._jobs.submit(
                        kind=kind,
                        payload={"runId": run.id},
                        ref_id=run.id,
                        created_by=job.created_by,
                    )
        try:
            summaries = await self._runs.list_summaries()
            active = {
                summary.run.id
                for summary in summaries
                if summary.run.status in {"preparing", "running", "blocked"}
            }
            self._workspaces.cleanup_workspaces(
                active_run_ids=active,
                keep=self._keep_workspaces,
            )
        except Exception:
            logger.exception("workspace retention failed", extra={"job_id": job.id})


def build_candidate_trace(
    cluster_id: str,
    title: str,
    probe_ids: Sequence[str],
    rows: Sequence[ProbeHitRow],
) -> CandidateTrace:
    """Build complete candidate trace data from one cluster and probe hit rows."""
    by_probe: OrderedDict[str, list[ProbeHitRow]] = OrderedDict()
    for row in rows:
        by_probe.setdefault(row.probe_id, []).append(row)
    sources = tuple(
        CandidateSource(
            probe_id=probe_id,
            bad_case_ids=tuple(dict.fromkeys(row.bad_case_id for row in grouped)),
            session_id=grouped[0].probe_session_id,
            description=grouped[0].probe_description,
            analysis=grouped[0].probe_analysis,
        )
        for probe_id, grouped in by_probe.items()
    )
    return CandidateTrace(
        cluster_id=cluster_id,
        cluster_title=title,
        probes=tuple(dict.fromkeys(probe_ids)),
        bad_cases=tuple(
            dict.fromkeys(row.bad_case_id for row in rows),
        ),
        analysis="\n".join(
            dict.fromkeys(row.probe_analysis for row in rows if row.probe_analysis.strip()),
        )
        or title,
        sources=sources,
    )


def build_frozen_candidate_trace(
    state: Any,
    cluster_id: str,
    title: str,
    links: tuple[str, ...] = (),
) -> CandidateTrace:
    """Render original or feedback provenance without re-reading mutable upstream rows."""
    if not links:
        links = tuple(state.checkpoint["cluster_map"][cluster_id]["source_probe_ids"])
    frozen_reports = state.checkpoint.get("source_reports", state.input_snapshot["reports"])
    reports = [report for report in frozen_reports if report["probe_id"] in links]
    return CandidateTrace(
        cluster_id=cluster_id,
        cluster_title=title,
        probes=links,
        bad_cases=tuple(
            dict.fromkeys(
                case["bad_case_id"] for report in reports for case in report["from_bad_cases"]
            )
        ),
        analysis="\n".join(report["probe_analysis"] for report in reports),
        sources=tuple(
            CandidateSource(
                probe_id=report["probe_id"],
                bad_case_ids=tuple(case["bad_case_id"] for case in report["from_bad_cases"]),
                session_id=report.get("session_id"),
                description=report["probe_description"],
                analysis=report["probe_analysis"],
            )
            for report in reports
        ),
    )


__all__ = [
    "EvolutionAdvanceService",
    "EvolutionJobLifecycle",
    "build_candidate_trace",
]
