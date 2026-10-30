"""Dialect-neutral persistence for evolution runs, clusters, rounds, and finals."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, insert, null, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Database
from app.db.models.evolution import (
    ClusterWrite,
    EvolutionFinalRecord,
    EvolutionRoundRecord,
    EvolutionRunRecord,
    EvolutionRunSummaryRecord,
    ProbeClusterLinkRecord,
    ProbeClusterRecord,
    evolution_finals,
    evolution_rounds,
    evolution_runs,
    probe_cluster_probes,
    probe_clusters,
    record_from_evolution_final,
    record_from_evolution_round,
    record_from_evolution_run,
    record_from_probe_cluster,
    record_from_probe_cluster_link,
)
from app.db.models.skill_evolution import skill_evolution_state
from app.db.models.users import users
from app.service.evolution.state import StageConflict, validate_transition
from app.utils.ids import new_id
from app.utils.time import now_shanghai


class EvolutionRepository:
    """Persist durable evolution state through one database boundary."""

    def __init__(self, database: Database) -> None:
        """Bind the repository to the application database."""
        self._database = database

    async def create_run(self, values: Mapping[str, Any]) -> EvolutionRunRecord:
        """Insert one run with immutable configuration snapshots."""
        now = now_shanghai()
        payload = {
            "id": values.get("id", new_id()),
            "title": values["title"],
            "mode": values["mode"],
            "engine_key": values["engine_key"],
            "eval_config": deepcopy(dict(values["eval_config"])),
            "target_config_id": values["target_config_id"],
            "target_type": values["target_type"],
            "target_name": values["target_name"],
            "target_version": values["target_version"],
            "repo_url": values["repo_url"],
            "sub_dir": values["sub_dir"],
            "repo_path": values.get("repo_path"),
            "base_ref": values.get("base_ref"),
            "base_commit": values.get("base_commit"),
            "branch_name": values.get("branch_name"),
            "workspace_path": values.get("workspace_path"),
            "remote_name": values.get("remote_name", "origin"),
            "push_status": values.get("push_status", "pending"),
            "pushed_at": values.get("pushed_at"),
            "push_error": values.get("push_error"),
            "window_from": values.get("window_from"),
            "window_to": values.get("window_to"),
            "models": deepcopy(list(values.get("models", []))),
            "status": values.get("status", "preparing"),
            "blocked_stage": values.get("blocked_stage"),
            "retry_attempts": values.get("retry_attempts", 0),
            "next_retry_at": values.get("next_retry_at"),
            "eval_status": values.get("eval_status", "pending"),
            "error_stage": values.get("error_stage"),
            "error_message": values.get("error_message"),
            "error_at": values.get("error_at"),
            "progress_unit": values.get("progress_unit", 0),
            "stats": deepcopy(dict(values.get("stats", {}))),
            "created_by": values.get("created_by"),
            "created_at": values.get("created_at", now),
            "finished_at": values.get("finished_at"),
        }
        async with self._database.transaction() as session:
            await session.execute(insert(evolution_runs).values(**payload))
        created = await self.get_run(str(payload["id"]))
        if created is None:
            raise RuntimeError("created evolution run disappeared")
        return created

    async def get_run(self, run_id: str) -> EvolutionRunRecord | None:
        """Return one run by stable id."""
        async with self._database.session() as session:
            row = (
                (
                    await session.execute(
                        select(evolution_runs).where(evolution_runs.c.id == run_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
        return record_from_evolution_run(cast(Mapping[str, Any], row)) if row is not None else None

    async def mark_failed(
        self,
        run_id: str,
        *,
        stage: str,
        message: str,
    ) -> None:
        """Persist one terminal failure while retaining its user-safe reason."""
        now = now_shanghai()
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    status="failed",
                    error_stage=stage,
                    error_message=message,
                    error_at=now,
                    finished_at=now,
                ),
            )

    async def mark_interrupted(
        self,
        run_id: str,
        *,
        stage: str,
        message: str,
    ) -> None:
        """Persist one interrupted terminal state."""
        now = now_shanghai()
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    status="interrupted",
                    error_stage=stage,
                    error_message=message,
                    error_at=now,
                    finished_at=now,
                ),
            )

    async def mark_blocked(
        self,
        run_id: str,
        *,
        stage: str,
        message: str,
        error_stage: str | None = None,
        next_retry_at: datetime | None = None,
        retry_attempts: int | None = None,
    ) -> None:
        """Persist a retryable blocked state and its next-attempt facts."""
        now = now_shanghai()
        values: dict[str, object] = {
            "status": "blocked",
            "blocked_stage": stage,
            "eval_status": "failed" if stage == "eval" else "pending",
            "error_stage": error_stage or stage,
            "error_message": message,
            "error_at": now,
            "next_retry_at": next_retry_at,
            "finished_at": None,
        }
        if stage == "push":
            values.update(
                {
                    "push_status": "failed",
                    "pushed_at": None,
                    "push_error": message,
                },
            )
        if retry_attempts is not None:
            values["retry_attempts"] = retry_attempts
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_runs).where(evolution_runs.c.id == run_id).values(**values),
            )

    async def mark_resumed(self, run_id: str) -> None:
        """Clear a prior blocked state when retry execution begins."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    status="running",
                    blocked_stage=None,
                    eval_status="pending",
                    error_stage=None,
                    error_message=None,
                    error_at=None,
                    next_retry_at=None,
                ),
            )

    async def increment_retry_attempts(self, run_id: str) -> EvolutionRunRecord:
        """Count one already-started automatic retry before it executes."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_runs)
                .where(
                    evolution_runs.c.id == run_id,
                    evolution_runs.c.status == "blocked",
                )
                .values(retry_attempts=evolution_runs.c.retry_attempts + 1),
            )
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError("evolution run not found")
        return run

    async def reset_manual_retry(self, run_id: str) -> EvolutionRunRecord:
        """Restart the backoff ladder for one human-requested retry."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(retry_attempts=0, next_retry_at=None),
            )
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError("evolution run not found")
        return run

    async def list_blocked_due(
        self,
        now: datetime,
        *,
        max_attempts: int,
        limit: int = 100,
    ) -> tuple[EvolutionRunRecord, ...]:
        """Return push-blocked runs whose automatic backoff deadline has passed."""
        if limit < 1:
            raise ValueError("blocked scan limit must be positive")
        if max_attempts < 1:
            raise ValueError("blocked scan attempt ceiling must be positive")
        async with self._database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(evolution_runs)
                        .where(
                            evolution_runs.c.status == "blocked",
                            evolution_runs.c.blocked_stage == "push",
                            evolution_runs.c.next_retry_at.is_not(None),
                            evolution_runs.c.next_retry_at <= now,
                            evolution_runs.c.retry_attempts < max_attempts,
                        )
                        .order_by(
                            evolution_runs.c.next_retry_at,
                            evolution_runs.c.created_at,
                            evolution_runs.c.id,
                        )
                        .limit(limit),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(record_from_evolution_run(cast(Mapping[str, Any], row)) for row in rows)

    async def mark_prepared(
        self,
        run_id: str,
        *,
        status: str,
        progress_unit: int,
        repo_path: str | None,
        base_ref: str | None,
        base_commit: str | None,
        branch_name: str | None,
        workspace_path: str | None,
        stats: Mapping[str, Any],
        skipped_rounds: int = 0,
    ) -> EvolutionRunRecord:
        """Persist the immutable workspace snapshot and prepared run status."""
        if status not in {"running", "completed"}:
            raise ValueError("prepared runs must be running or completed")
        if progress_unit != 4:
            raise ValueError("prepared runs must finish the severity step")
        if skipped_rounds < 0:
            raise ValueError("skipped round count must be nonnegative")
        progress_unit += skipped_rounds
        now = now_shanghai()
        async with self._database.transaction() as session:
            actual_skipped = (
                await session.execute(
                    select(func.count())
                    .select_from(evolution_rounds)
                    .where(
                        evolution_rounds.c.run_id == run_id,
                        evolution_rounds.c.status == "skipped",
                    ),
                )
            ).scalar_one()
            if actual_skipped != skipped_rounds:
                raise ValueError("skipped round count differs from persisted rounds")
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_runs)
                    .where(
                        evolution_runs.c.id == run_id,
                        evolution_runs.c.status.in_(("preparing", "running")),
                        evolution_runs.c.progress_unit <= progress_unit,
                    )
                    .values(
                        status=status,
                        progress_unit=progress_unit,
                        repo_path=repo_path,
                        base_ref=base_ref,
                        base_commit=base_commit,
                        branch_name=branch_name,
                        workspace_path=workspace_path,
                        stats=deepcopy(dict(stats)),
                        finished_at=now if status == "completed" else None,
                    ),
                ),
            )
        if result.rowcount != 1:
            current = await self.get_run(run_id)
            if current is None:
                raise LookupError("evolution run not found")
            if current.progress_unit == progress_unit and current.status == status:
                return current
            raise StageConflict(
                status_code=409,
                current=current.progress_unit,
                target=progress_unit,
                reason="run cannot be marked prepared from its current state",
            )
        prepared = await self.get_run(run_id)
        if prepared is None:
            raise LookupError("evolution run not found")
        return prepared

    async def write_cluster_confidence(
        self,
        run_id: str,
        values: Mapping[str, float],
    ) -> None:
        """Write attribution confidence and advance exactly one cursor unit."""
        await self._write_cluster_step(
            run_id,
            expected=2,
            target=3,
            values={
                cluster_id: {"confidence": confidence} for cluster_id, confidence in values.items()
            },
        )

    async def write_cluster_severity(
        self,
        run_id: str,
        values: Mapping[str, tuple[float, int]],
    ) -> None:
        """Write severity/rank and advance exactly one cursor unit."""
        await self._write_cluster_step(
            run_id,
            expected=3,
            target=4,
            values={
                cluster_id: {"severity": severity, "rank": rank}
                for cluster_id, (severity, rank) in values.items()
            },
        )

    async def _write_cluster_step(
        self,
        run_id: str,
        *,
        expected: int,
        target: int,
        values: Mapping[str, Mapping[str, float | int]],
    ) -> None:
        """Update one cluster step and its cursor in the same transaction."""
        async with self._database.transaction() as session:
            cluster_ids: list[str] = list(
                (
                    await session.execute(
                        select(probe_clusters.c.probe_cluster_id).where(
                            probe_clusters.c.run_id == run_id,
                        ),
                    )
                ).scalars(),
            )
            if set(cluster_ids) != set(values):
                raise StageConflict(
                    status_code=409,
                    current=expected,
                    target=target,
                    reason="cluster writes must cover the complete run",
                )
            cluster_count = len(cluster_ids)
            current, status = await _run_cursor(session, run_id)
            if current != expected or status not in {"preparing", "running", "blocked"}:
                raise StageConflict(
                    status_code=409,
                    current=current,
                    target=target,
                    reason="cluster step is not ready",
                )
            validate_transition(expected, target, cluster_count)
            for cluster_id, cluster_values in values.items():
                await session.execute(
                    update(probe_clusters)
                    .where(probe_clusters.c.probe_cluster_id == cluster_id)
                    .values(**cluster_values),
                )
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(progress_unit=target),
            )

    async def list_summaries(self) -> tuple[EvolutionRunSummaryRecord, ...]:
        """Return every run with batched creator, cluster, round, and final facts."""
        return await self._summaries()

    async def get_summary(self, run_id: str) -> EvolutionRunSummaryRecord | None:
        """Return one run with batched creator, cluster, round, and final facts."""
        summaries = await self._summaries(run_id)
        return summaries[0] if summaries else None

    async def _summaries(
        self,
        run_id: str | None = None,
    ) -> tuple[EvolutionRunSummaryRecord, ...]:
        """Load summary facts for every run or one requested run."""
        async with self._database.session() as session:
            statement = (
                select(
                    evolution_runs,
                    users.c.display_name.label("created_by_name"),
                )
                .select_from(
                    evolution_runs.outerjoin(
                        users,
                        users.c.id == evolution_runs.c.created_by,
                    ),
                )
                .order_by(
                    evolution_runs.c.created_at.desc(),
                    evolution_runs.c.id.desc(),
                )
            )
            if run_id is not None:
                statement = statement.where(evolution_runs.c.id == run_id)
            rows = (await session.execute(statement)).mappings().all()
            run_ids = [str(row["id"]) for row in rows]
            if not run_ids:
                return ()
            cluster_counts = {
                str(row["run_id"]): int(row["count"])
                for row in (
                    await session.execute(
                        select(
                            probe_clusters.c.run_id,
                            func.count().label("count"),
                        )
                        .where(probe_clusters.c.run_id.in_(run_ids))
                        .group_by(probe_clusters.c.run_id),
                    )
                )
                .mappings()
                .all()
            }
            accepted_counts = {
                str(row["run_id"]): int(row["count"])
                for row in (
                    await session.execute(
                        select(
                            evolution_rounds.c.run_id,
                            func.count().label("count"),
                        )
                        .where(
                            evolution_rounds.c.run_id.in_(run_ids),
                            evolution_rounds.c.accepted.is_(True),
                        )
                        .group_by(evolution_rounds.c.run_id),
                    )
                )
                .mappings()
                .all()
            }
            final_by_run = {
                str(row["run_id"]): record_from_evolution_final(
                    cast(Mapping[str, Any], row),
                )
                for row in (
                    await session.execute(
                        select(evolution_finals).where(
                            evolution_finals.c.run_id.in_(run_ids),
                        ),
                    )
                )
                .mappings()
                .all()
            }
        return tuple(
            EvolutionRunSummaryRecord(
                run=record_from_evolution_run(cast(Mapping[str, Any], row)),
                cluster_count=cluster_counts.get(str(row["id"]), 0),
                accepted_count=accepted_counts.get(str(row["id"]), 0),
                final=final_by_run.get(str(row["id"])),
                created_by_name=str(row["created_by_name"])
                if row["created_by_name"] is not None
                else None,
            )
            for row in rows
        )

    async def advance_unit(
        self,
        run_id: str,
        *,
        expected: int,
        target: int,
        cluster_count: int,
    ) -> EvolutionRunRecord:
        """Advance one cursor unit only when the persisted cursor still matches."""
        validate_transition(expected, target, cluster_count)
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_runs)
                    .where(
                        evolution_runs.c.id == run_id,
                        evolution_runs.c.progress_unit == expected,
                    )
                    .values(progress_unit=target),
                ),
            )
        if result.rowcount != 1:
            current = await self.get_run(run_id)
            if current is None:
                raise LookupError("evolution run not found")
            raise StageConflict(
                status_code=409,
                current=current.progress_unit,
                target=target,
                reason="stale cursor",
            )
        advanced = await self.get_run(run_id)
        if advanced is None:
            raise LookupError("evolution run not found")
        return advanced

    async def replace_clusters(
        self,
        run_id: str,
        clusters: Sequence[ClusterWrite],
    ) -> tuple[ProbeClusterRecord, ...]:
        """Replace one run's complete cluster snapshot in one transaction."""
        now = now_shanghai()
        cluster_rows: list[dict[str, Any]] = []
        link_rows: list[dict[str, str]] = []
        for seq, cluster in enumerate(clusters, start=1):
            cluster_id = new_id()
            cluster_rows.append(
                {
                    "probe_cluster_id": cluster_id,
                    "run_id": run_id,
                    "seq": seq,
                    "probe_cluster_description": cluster.description,
                    "probe_cluster_type": cluster.type,
                    "confidence": None,
                    "impact": cluster.impact,
                    "severity": None,
                    "rank": None,
                    "created_at": now,
                },
            )
            link_rows.extend(
                {
                    "probe_cluster_id": cluster_id,
                    "probe_id": probe_id,
                }
                for probe_id in cluster.probes
            )

        async with self._database.transaction() as session:
            await _require_run_stage(
                session,
                run_id,
                expected=1,
                statuses=("preparing", "running"),
                reason="cluster writes require the cluster stage",
            )
            existing_ids: list[str] = list(
                (
                    await session.execute(
                        select(probe_clusters.c.probe_cluster_id).where(
                            probe_clusters.c.run_id == run_id,
                        ),
                    )
                ).scalars(),
            )
            if existing_ids:
                await session.execute(
                    delete(probe_cluster_probes).where(
                        probe_cluster_probes.c.probe_cluster_id.in_(existing_ids),
                    ),
                )
            await session.execute(
                delete(probe_clusters).where(probe_clusters.c.run_id == run_id),
            )
            if cluster_rows:
                await session.execute(insert(probe_clusters), cluster_rows)
            if link_rows:
                await session.execute(insert(probe_cluster_probes), link_rows)
        return await self.list_clusters(run_id)

    async def list_clusters(self, run_id: str) -> tuple[ProbeClusterRecord, ...]:
        """Return one run's clusters in creation order."""
        async with self._database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(probe_clusters)
                        .where(probe_clusters.c.run_id == run_id)
                        .order_by(probe_clusters.c.seq),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(record_from_probe_cluster(cast(Mapping[str, Any], row)) for row in rows)

    async def list_cluster_probes(self, run_id: str) -> tuple[ProbeClusterLinkRecord, ...]:
        """Return one run's cluster-to-probe links in cluster sequence order."""
        async with self._database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(probe_cluster_probes)
                        .select_from(
                            probe_cluster_probes.join(
                                probe_clusters,
                                probe_clusters.c.probe_cluster_id
                                == probe_cluster_probes.c.probe_cluster_id,
                            ),
                        )
                        .where(probe_clusters.c.run_id == run_id)
                        .order_by(probe_clusters.c.seq, probe_cluster_probes.c.id),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(record_from_probe_cluster_link(cast(Mapping[str, Any], row)) for row in rows)

    async def get_final(self, run_id: str) -> EvolutionFinalRecord | None:
        """Return one run's terminal evaluation row when present."""
        async with self._database.session() as session:
            row = (
                (
                    await session.execute(
                        select(evolution_finals).where(evolution_finals.c.run_id == run_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
        return (
            record_from_evolution_final(cast(Mapping[str, Any], row)) if row is not None else None
        )

    async def insert_rounds(self, rounds: Sequence[Mapping[str, Any]]) -> None:
        """Insert the pending execution rounds for one prepared run."""
        if not rounds:
            return
        values = []
        for round_ in rounds:
            values.append(
                {
                    "id": round_.get("id", new_id()),
                    "run_id": round_["run_id"],
                    "probe_cluster_id": round_["probe_cluster_id"],
                    "seq": round_["seq"],
                    "status": round_.get("status", "pending"),
                    "started_at": round_.get("started_at"),
                    "finished_at": round_.get("finished_at"),
                    "duration_ms": round_.get("duration_ms"),
                    "error_message": round_.get("error_message"),
                    "patch": _nullable_json(round_.get("patch")),
                    "candidate_ref": round_.get("candidate_ref"),
                    "candidate_commit": round_.get("candidate_commit"),
                    "candidate_message": round_.get("candidate_message"),
                    "push_status": round_.get("push_status", "pending"),
                    "push_attempts": round_.get("push_attempts", 0),
                    "pushed_at": round_.get("pushed_at"),
                    "push_error": round_.get("push_error"),
                    "eval_external_id": round_.get("eval_external_id"),
                    "eval_status": round_.get("eval_status", "pending"),
                    "eval_attempts": round_.get("eval_attempts", 0),
                    "eval_next_poll_at": round_.get("eval_next_poll_at"),
                    "eval_started_at": round_.get("eval_started_at"),
                    "eval_finished_at": round_.get("eval_finished_at"),
                    "eval_current": round_.get("eval_current"),
                    "eval_candidate": round_.get("eval_candidate"),
                    "eval_result": _nullable_json(round_.get("eval_result")),
                    "eval_error": round_.get("eval_error"),
                    "accepted": round_.get("accepted", False),
                    "commit_hash": round_.get("commit_hash"),
                    "commit_message": round_.get("commit_message"),
                },
            )
        run_ids = {str(round_["run_id"]) for round_ in values}
        if len(run_ids) != 1:
            raise ValueError("rounds must belong to exactly one run")
        run_id = run_ids.pop()
        cluster_ids = {str(round_["probe_cluster_id"]) for round_ in values}
        async with self._database.transaction() as session:
            await _require_run_stage(
                session,
                run_id,
                expected=4,
                statuses=("preparing", "running"),
                reason="round loading requires completed severity ranking",
            )
            owners = {
                str(row["probe_cluster_id"]): str(row["run_id"])
                for row in (
                    await session.execute(
                        select(
                            probe_clusters.c.probe_cluster_id,
                            probe_clusters.c.run_id,
                        ).where(
                            probe_clusters.c.probe_cluster_id.in_(cluster_ids),
                        ),
                    )
                )
                .mappings()
                .all()
            }
            if set(owners) != cluster_ids or any(owner != run_id for owner in owners.values()):
                raise StageConflict(
                    status_code=409,
                    current=4,
                    target=4,
                    reason="round clusters must belong to the same run",
                )
            await session.execute(insert(evolution_rounds), values)

    async def list_rounds(self, run_id: str) -> tuple[EvolutionRoundRecord, ...]:
        """Return one run's execution rounds in sequence order."""
        async with self._database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(evolution_rounds)
                        .where(evolution_rounds.c.run_id == run_id)
                        .order_by(evolution_rounds.c.seq),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(record_from_evolution_round(cast(Mapping[str, Any], row)) for row in rows)

    async def get_round(self, round_id: str) -> EvolutionRoundRecord | None:
        """Return one round by stable identifier."""
        async with self._database.session() as session:
            row = (
                (
                    await session.execute(
                        select(evolution_rounds).where(evolution_rounds.c.id == round_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
        return record_from_evolution_round(cast(Mapping[str, Any], row)) if row else None

    async def mark_round_patching(
        self,
        round_id: str,
        *,
        started_at: datetime,
    ) -> EvolutionRoundRecord:
        """Enter patching or resume the same unfinished round."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_rounds)
                .where(
                    evolution_rounds.c.id == round_id,
                    evolution_rounds.c.status.in_(("pending", "patching")),
                )
                .values(status="patching", started_at=started_at, error_message=None),
            )
        round_ = await self.get_round(round_id)
        if round_ is None or round_.status != "patching":
            raise StageConflict(
                status_code=409,
                current=0,
                target=1,
                reason="round cannot enter patching",
            )
        return round_

    async def claim_round_for_advance(
        self,
        round_id: str,
        *,
        started_at: datetime,
    ) -> bool:
        """Atomically claim one untouched round for sequential candidate work."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.status == "pending",
                    )
                    .values(
                        status="patching",
                        started_at=started_at,
                        error_message=None,
                    ),
                ),
            )
        return result.rowcount == 1

    async def mark_round_candidate(
        self,
        round_id: str,
        *,
        candidate_ref: str,
        candidate_commit: str,
        candidate_message: str,
        patch: Mapping[str, Any],
    ) -> EvolutionRoundRecord:
        """Persist the committed and real on-disk candidate snapshot."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_rounds)
                .where(
                    evolution_rounds.c.id == round_id,
                    evolution_rounds.c.status == "patching",
                )
                .values(
                    candidate_ref=candidate_ref,
                    candidate_commit=candidate_commit,
                    candidate_message=candidate_message,
                    patch=deepcopy(dict(patch)),
                    push_status="pending",
                    push_error=None,
                ),
            )
        return await self._required_round(round_id)

    async def mark_round_pushing(
        self,
        round_id: str,
        *,
        attempt: int,
    ) -> EvolutionRoundRecord:
        """Enter pushing and record the next attempt count."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_rounds)
                .where(
                    evolution_rounds.c.id == round_id,
                    evolution_rounds.c.status.in_(("patching", "pushing")),
                    evolution_rounds.c.candidate_ref.is_not(None),
                )
                .values(
                    status="pushing",
                    push_status="pushing",
                    push_attempts=attempt,
                    push_error=None,
                ),
            )
        return await self._required_round(round_id)

    async def mark_round_push_succeeded(
        self,
        run_id: str,
        round_id: str,
        *,
        pushed_at: datetime,
        attempts: int,
    ) -> EvolutionRoundRecord:
        """Persist a successful candidate and aggregate run push atomically."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.run_id == run_id,
                        evolution_rounds.c.status == "pushing",
                    )
                    .values(
                        push_status="succeeded",
                        push_attempts=attempts,
                        pushed_at=pushed_at,
                        push_error=None,
                    ),
                ),
            )
            if result.rowcount != 1:
                raise StageConflict(
                    status_code=409,
                    current=0,
                    target=1,
                    reason="round push cannot succeed",
                )
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    push_status="succeeded",
                    pushed_at=pushed_at,
                    push_error=None,
                ),
            )
        return await self._required_round(round_id)

    async def mark_round_and_run_push_skipped(
        self,
        run_id: str,
        round_id: str,
    ) -> EvolutionRoundRecord:
        """Atomically persist a local-only candidate and run publication state."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.run_id == run_id,
                        evolution_rounds.c.status.in_(("patching", "pushing")),
                        evolution_rounds.c.candidate_ref.is_not(None),
                    )
                    .values(
                        status="skipped",
                        push_status="skipped",
                        push_attempts=0,
                        pushed_at=None,
                        push_error=None,
                        eval_status="pending",
                        finished_at=now_shanghai(),
                    ),
                ),
            )
            if result.rowcount != 1:
                raise StageConflict(
                    status_code=409,
                    current=0,
                    target=1,
                    reason="round cannot be marked as a skipped push",
                )
            progress_result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_runs)
                    .where(
                        evolution_runs.c.id == run_id,
                        evolution_runs.c.status == "running",
                    )
                    .values(progress_unit=evolution_runs.c.progress_unit + 1),
                ),
            )
            if progress_result.rowcount != 1:
                raise StageConflict(
                    status_code=409,
                    current=0,
                    target=1,
                    reason="skipped round cannot advance the run cursor",
                )
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    push_status="skipped",
                    pushed_at=None,
                    push_error=None,
                ),
            )
        return await self._required_round(round_id, expected_status="skipped")

    async def mark_round_push_failed(
        self,
        run_id: str,
        round_id: str,
        *,
        attempts: int,
        error: str,
    ) -> EvolutionRoundRecord:
        """Persist round and aggregate run failure while retaining the candidate."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.run_id == run_id,
                        evolution_rounds.c.status == "pushing",
                    )
                    .values(
                        push_status="failed",
                        push_attempts=attempts,
                        push_error=error,
                    ),
                ),
            )
            if result.rowcount != 1:
                raise StageConflict(
                    status_code=409,
                    current=0,
                    target=1,
                    reason="round push cannot fail",
                )
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    push_status="failed",
                    pushed_at=None,
                    push_error=error,
                ),
            )
        return await self._required_round(round_id)

    async def mark_round_evaluating(self, round_id: str) -> EvolutionRoundRecord:
        """Move a published candidate into evaluation without resubmitting it."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_rounds)
                .where(
                    evolution_rounds.c.id == round_id,
                    evolution_rounds.c.status == "pushing",
                    evolution_rounds.c.push_status == "succeeded",
                )
                .values(status="evaluating", eval_status="pending"),
            )
        return await self._required_round(round_id)

    async def mark_eval_submitted(
        self,
        run_id: str,
        round_id: str,
        *,
        external_id: str,
        started_at: datetime,
        next_poll_at: datetime,
    ) -> EvolutionRoundRecord:
        """Persist the external evaluation identity and first durable poll deadline."""
        if not external_id.strip():
            raise ValueError("external evaluation id must not be blank")
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.run_id == run_id,
                        evolution_rounds.c.status == "evaluating",
                        evolution_rounds.c.eval_external_id.is_(None),
                    )
                    .values(
                        eval_external_id=external_id,
                        eval_status="evaluating",
                        eval_attempts=evolution_rounds.c.eval_attempts + 1,
                        eval_next_poll_at=next_poll_at,
                        eval_started_at=started_at,
                        eval_finished_at=None,
                        eval_result=null(),
                        eval_error=None,
                    ),
                ),
            )
            if result.rowcount != 1:
                changed = False
            else:
                changed = True
                await session.execute(
                    update(evolution_runs)
                    .where(evolution_runs.c.id == run_id)
                    .values(eval_status="evaluating"),
                )
        if not changed:
            current = await self.get_round(round_id)
            if current is not None and current.eval_external_id == external_id:
                return current
            raise StageConflict(
                status_code=409,
                current=0,
                target=1,
                reason="round cannot enter external evaluation",
            )
        return await self._required_round(round_id)

    async def list_eval_due(
        self,
        now: datetime,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionRoundRecord, ...]:
        """Return due, actively evaluated rounds in stable creation order."""
        if limit < 1:
            raise ValueError("evaluation scan limit must be positive")
        async with self._database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(evolution_rounds)
                        .join(
                            evolution_runs,
                            evolution_runs.c.id == evolution_rounds.c.run_id,
                        )
                        .where(
                            evolution_rounds.c.status == "evaluating",
                            evolution_rounds.c.eval_status == "evaluating",
                            evolution_rounds.c.eval_external_id.is_not(None),
                            evolution_rounds.c.eval_next_poll_at.is_not(None),
                            evolution_rounds.c.eval_next_poll_at <= now,
                            evolution_runs.c.mode == "full",
                            evolution_runs.c.status == "running",
                        )
                        .order_by(
                            evolution_rounds.c.eval_next_poll_at,
                            evolution_rounds.c.run_id,
                            evolution_rounds.c.seq,
                        )
                        .limit(limit),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(record_from_evolution_round(cast(Mapping[str, Any], row)) for row in rows)

    async def list_eval_followups(
        self,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionRoundRecord, ...]:
        """Return completed evaluations whose apply job still needs durable creation."""
        if limit < 1:
            raise ValueError("evaluation follow-up limit must be positive")
        async with self._database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(evolution_rounds)
                        .join(
                            evolution_runs,
                            evolution_runs.c.id == evolution_rounds.c.run_id,
                        )
                        .where(
                            evolution_rounds.c.status == "evaluating",
                            evolution_rounds.c.eval_status == "completed",
                            evolution_rounds.c.eval_result.is_not(None),
                            evolution_runs.c.mode == "full",
                            evolution_runs.c.status == "running",
                        )
                        .order_by(
                            evolution_rounds.c.eval_finished_at,
                            evolution_rounds.c.run_id,
                            evolution_rounds.c.seq,
                        )
                        .limit(limit),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(record_from_evolution_round(cast(Mapping[str, Any], row)) for row in rows)

    async def list_running_full_runs(
        self,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionRunRecord, ...]:
        """Return running full-mode runs for terminal follow-up recovery scans."""
        if limit < 1:
            raise ValueError("run follow-up limit must be positive")
        async with self._database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(evolution_runs)
                        .where(
                            evolution_runs.c.mode == "full",
                            evolution_runs.c.status == "running",
                        )
                        .order_by(evolution_runs.c.created_at, evolution_runs.c.id)
                        .limit(limit),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(record_from_evolution_run(cast(Mapping[str, Any], row)) for row in rows)

    async def mark_eval_waiting(
        self,
        round_id: str,
        *,
        next_poll_at: datetime,
    ) -> EvolutionRoundRecord:
        """Defer the next poll without changing a queued/running evaluation."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.status == "evaluating",
                        evolution_rounds.c.eval_status == "evaluating",
                        evolution_rounds.c.eval_external_id.is_not(None),
                    )
                    .values(eval_next_poll_at=next_poll_at),
                ),
            )
        if result.rowcount != 1:
            raise StageConflict(
                status_code=409,
                current=0,
                target=1,
                reason="evaluation is not waiting for a poll",
            )
        return await self._required_round(round_id)

    async def save_eval_result(
        self,
        run_id: str,
        round_id: str,
        *,
        result: Mapping[str, Any],
        finished_at: datetime,
    ) -> bool:
        """Persist one normalized result exactly once with its UI score projection."""
        current_score = result.get("currentScore")
        candidate_score = result.get("candidateScore")
        if (
            isinstance(current_score, bool)
            or isinstance(candidate_score, bool)
            or not isinstance(current_score, (int, float))
            or not isinstance(candidate_score, (int, float))
        ):
            raise ValueError("evaluation result scores are invalid")
        async with self._database.transaction() as session:
            changed = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.run_id == run_id,
                        evolution_rounds.c.status == "evaluating",
                        evolution_rounds.c.eval_status == "evaluating",
                        evolution_rounds.c.eval_external_id.is_not(None),
                        evolution_rounds.c.eval_result.is_(None),
                    )
                    .values(
                        eval_status="completed",
                        eval_next_poll_at=None,
                        eval_finished_at=finished_at,
                        eval_result=deepcopy(dict(result)),
                        eval_current=float(current_score),
                        eval_candidate=float(candidate_score),
                        eval_error=None,
                    ),
                ),
            )
            if changed.rowcount != 1:
                return False
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(eval_status="completed"),
            )
        return True

    async def mark_eval_failed(
        self,
        run_id: str,
        round_id: str,
        *,
        status: str,
        error: str,
        finished_at: datetime,
    ) -> bool:
        """Persist one terminal external failure and block the aggregate run."""
        if status not in {"failed", "timeout"}:
            raise ValueError("evaluation terminal status must be failed or timeout")
        async with self._database.transaction() as session:
            changed = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.run_id == run_id,
                        evolution_rounds.c.status == "evaluating",
                        evolution_rounds.c.eval_status.in_(("pending", "evaluating")),
                    )
                    .values(
                        eval_status=status,
                        eval_next_poll_at=None,
                        eval_finished_at=finished_at,
                        eval_error=error,
                    ),
                ),
            )
            if changed.rowcount != 1:
                return False
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    status="blocked",
                    blocked_stage="eval",
                    eval_status="failed",
                    error_stage="eval",
                    error_message=error,
                    error_at=finished_at,
                    next_retry_at=None,
                    finished_at=None,
                ),
            )
        return True

    async def reset_round_for_eval_retry(
        self,
        run_id: str,
        round_id: str,
    ) -> EvolutionRoundRecord:
        """Clear one failed external evaluation so it can be submitted again."""
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_rounds)
                .where(
                    evolution_rounds.c.id == round_id,
                    evolution_rounds.c.run_id == run_id,
                    evolution_rounds.c.status == "evaluating",
                    evolution_rounds.c.push_status == "succeeded",
                )
                .values(
                    status="evaluating",
                    eval_status="pending",
                    eval_external_id=None,
                    eval_next_poll_at=None,
                    eval_started_at=None,
                    eval_finished_at=None,
                    eval_result=null(),
                    eval_error=None,
                ),
            )
        return await self._required_round(round_id, expected_status="evaluating")

    async def mark_round_deciding(self, round_id: str) -> EvolutionRoundRecord:
        """Enter the apply phase without resubmitting an already evaluated candidate."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.status.in_(("evaluating", "deciding")),
                        evolution_rounds.c.eval_status == "completed",
                    )
                    .values(status="deciding"),
                ),
            )
        if result.rowcount != 1:
            raise StageConflict(
                status_code=409,
                current=0,
                target=1,
                reason="round cannot enter deciding",
            )
        return await self._required_round(round_id, expected_status="deciding")

    async def accept_round(
        self,
        round_id: str,
        *,
        final_commit: str,
        final_message: str,
        finished_at: datetime,
    ) -> EvolutionRoundRecord:
        """Persist one accepted amended commit and successful run push."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.status == "deciding",
                    )
                    .values(
                        status="accepted",
                        accepted=True,
                        commit_hash=final_commit,
                        commit_message=final_message,
                        finished_at=finished_at,
                    ),
                ),
            )
            if result.rowcount != 1:
                raise StageConflict(
                    status_code=409,
                    current=0,
                    target=1,
                    reason="only deciding rounds can be accepted",
                )
            run_id: str = (
                await session.execute(
                    select(evolution_rounds.c.run_id).where(evolution_rounds.c.id == round_id),
                )
            ).scalar_one()
            state = (
                (
                    await session.execute(
                        select(skill_evolution_state)
                        .where(
                            skill_evolution_state.c.run_id == run_id,
                        )
                        .with_for_update(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if state is not None:
                patch: Any = (
                    await session.execute(
                        select(evolution_rounds.c.patch).where(
                            evolution_rounds.c.id == round_id,
                        )
                    )
                ).scalar_one()
                files = patch.get("files") if isinstance(patch, dict) else None
                if (
                    not isinstance(files, dict)
                    or not files
                    or any(
                        not isinstance(path, str) or not isinstance(content, str)
                        for path, content in files.items()
                    )
                ):
                    raise ValueError("accepted Skill patch requires a validated file snapshot")
                checkpoint = deepcopy(dict(state["checkpoint"]))
                checkpoint["best_files"] = deepcopy(files)
                await session.execute(
                    update(skill_evolution_state)
                    .where(
                        skill_evolution_state.c.run_id == run_id,
                    )
                    .values(
                        checkpoint=checkpoint,
                        revision=state["revision"] + 1,
                        updated_at=finished_at,
                    )
                )
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(
                    push_status="succeeded",
                    pushed_at=finished_at,
                    push_error=None,
                    progress_unit=evolution_runs.c.progress_unit + 1,
                ),
            )
        return await self._required_round(round_id)

    async def reject_invalid_patch(
        self,
        round_id: str,
        *,
        reason: str,
        finished_at: datetime,
    ) -> EvolutionRoundRecord:
        """Reject only a validated invalid/no-op proposal, never a provider failure."""
        if reason not in ("invalid_patch", "noop_patch"):
            raise ValueError("invalid patch rejection requires a safe reason code")
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.status == "patching",
                    )
                    .values(
                        status="rejected",
                        accepted=False,
                        eval_status="skipped",
                        eval_error=reason,
                        error_message=reason,
                        finished_at=finished_at,
                        commit_hash=None,
                        commit_message=None,
                    ),
                ),
            )
            if result.rowcount != 1:
                raise StageConflict(
                    status_code=409,
                    current=0,
                    target=1,
                    reason="only patching rounds can reject invalid proposals",
                )
            run_id: str = (
                await session.execute(
                    select(evolution_rounds.c.run_id).where(evolution_rounds.c.id == round_id),
                )
            ).scalar_one()
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(progress_unit=evolution_runs.c.progress_unit + 1),
            )
        return await self._required_round(round_id, expected_status="rejected")

    async def reject_round(
        self,
        round_id: str,
        *,
        finished_at: datetime,
    ) -> EvolutionRoundRecord:
        """Persist one rejected round while keeping the run branch untouched."""
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(evolution_rounds)
                    .where(
                        evolution_rounds.c.id == round_id,
                        evolution_rounds.c.status == "deciding",
                    )
                    .values(
                        status="rejected",
                        accepted=False,
                        finished_at=finished_at,
                        commit_hash=None,
                        commit_message=None,
                    ),
                ),
            )
            if result.rowcount != 1:
                raise StageConflict(
                    status_code=409,
                    current=0,
                    target=1,
                    reason="only deciding rounds can be rejected",
                )
            run_id: str = (
                await session.execute(
                    select(evolution_rounds.c.run_id).where(evolution_rounds.c.id == round_id),
                )
            ).scalar_one()
            await session.execute(
                update(evolution_runs)
                .where(evolution_runs.c.id == run_id)
                .values(progress_unit=evolution_runs.c.progress_unit + 1),
            )
        return await self._required_round(round_id, expected_status="rejected")

    async def complete_run(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        push_status: str = "succeeded",
        stats: Mapping[str, Any] | None = None,
    ) -> EvolutionRunRecord:
        """Mark the run complete only after its run branch has been published."""
        if push_status not in {"succeeded", "skipped"}:
            raise ValueError("completed run requires succeeded or skipped push state")
        values: dict[str, Any] = {
            "status": "completed",
            "push_status": push_status,
            "pushed_at": finished_at if push_status == "succeeded" else None,
            "finished_at": finished_at,
            "blocked_stage": None,
            "next_retry_at": None,
            "error_stage": None,
            "error_message": None,
            "error_at": None,
        }
        if stats is not None:
            values["stats"] = deepcopy(dict(stats))
        async with self._database.transaction() as session:
            await session.execute(
                update(evolution_runs).where(evolution_runs.c.id == run_id).values(**values),
            )
        completed = await self.get_run(run_id)
        if completed is None:
            raise LookupError("evolution run not found")
        return completed

    async def _required_round(
        self,
        round_id: str,
        *,
        expected_status: str | None = None,
    ) -> EvolutionRoundRecord:
        """Return one round or fail fast when a write targeted a missing row."""
        round_ = await self.get_round(round_id)
        if round_ is None:
            raise LookupError("evolution round not found")
        if expected_status is not None and round_.status != expected_status:
            raise StageConflict(
                status_code=409,
                current=0,
                target=1,
                reason=f"round is {round_.status}, not {expected_status}",
            )
        return round_

    async def write_final(self, values: Mapping[str, Any]) -> EvolutionFinalRecord:
        """Insert or replace a run's single final-evaluation row."""
        run_id = str(values["run_id"])
        payload = {
            "best_version": values["best_version"],
            "verdict": values["verdict"],
            "best_score": values["best_score"],
            "init_score": values["init_score"],
            "metrics": deepcopy(values["metrics"]),
        }
        async with self._database.transaction() as session:
            current, status = await _run_cursor(session, run_id)
            cluster_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(probe_clusters)
                        .where(probe_clusters.c.run_id == run_id),
                    )
                ).scalar_one(),
            )
            expected = 4 + cluster_count
            if current != expected or status != "running":
                raise StageConflict(
                    status_code=409,
                    current=current,
                    target=expected,
                    reason="final writes require all iterations to finish",
                )
            existing = (
                await session.execute(
                    select(evolution_finals.c.run_id).where(
                        evolution_finals.c.run_id == run_id,
                    ),
                )
            ).scalar_one_or_none()
            if existing is None:
                await session.execute(
                    insert(evolution_finals).values(run_id=run_id, **payload),
                )
            else:
                await session.execute(
                    update(evolution_finals)
                    .where(evolution_finals.c.run_id == run_id)
                    .values(**payload),
                )
        async with self._database.session() as session:
            row = (
                (
                    await session.execute(
                        select(evolution_finals).where(
                            evolution_finals.c.run_id == run_id,
                        ),
                    )
                )
                .mappings()
                .one()
            )
        return record_from_evolution_final(cast(Mapping[str, Any], row))


def _nullable_json(value: Any) -> Any:
    """Persist an absent JSON value as SQL NULL so null predicates match."""
    return null() if value is None else deepcopy(value)


async def _run_cursor(session: AsyncSession, run_id: str) -> tuple[int, str]:
    """Return the persisted cursor and status or fail for a missing run."""
    row = (
        (
            await session.execute(
                select(
                    evolution_runs.c.progress_unit,
                    evolution_runs.c.status,
                ).where(evolution_runs.c.id == run_id),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LookupError("evolution run not found")
    return int(row["progress_unit"]), str(row["status"])


async def _require_run_stage(
    session: AsyncSession,
    run_id: str,
    *,
    expected: int,
    statuses: tuple[str, ...],
    reason: str,
) -> None:
    """Reject a stage-owned write unless the run remains in its expected state."""
    current, status = await _run_cursor(session, run_id)
    if current != expected or status not in statuses:
        raise StageConflict(
            status_code=409,
            current=current,
            target=expected,
            reason=reason,
        )
