"""Atomic persistence boundary for Skill input snapshots and algorithm checkpoints."""

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, cast

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import CursorResult, RowMapping

from app.db.base import Database
from app.db.models.evolution import evolution_rounds, evolution_runs, probe_clusters
from app.db.models.skill_evolution import SkillEvolutionStateRecord, skill_evolution_state
from app.utils.ids import new_id
from app.utils.time import as_shanghai, now_shanghai


class SkillCheckpointConflict(RuntimeError):
    """A stale writer must reload the committed state before advancing."""


def _payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = deepcopy(dict(value))
    json.dumps(payload, allow_nan=False)
    return payload


def _record(row: RowMapping) -> SkillEvolutionStateRecord:
    return SkillEvolutionStateRecord(
        run_id=str(row["run_id"]),
        input_snapshot=deepcopy(dict(row["input_snapshot"])),
        checkpoint=deepcopy(dict(row["checkpoint"])),
        revision=int(row["revision"]),
        created_at=as_shanghai(row["created_at"]),
        updated_at=as_shanghai(row["updated_at"]),
    )


class SkillEvolutionRepository:
    """Preserve source inputs once and commit completed intermediate facts atomically."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def initialize(
        self,
        run_id: str,
        input_snapshot: Mapping[str, Any],
    ) -> SkillEvolutionStateRecord:
        """Freeze one Skill run's input; identical retries do not overwrite checkpoints."""
        snapshot = _payload(input_snapshot)
        async with self._database.transaction() as session:
            parent = (
                await session.execute(
                    select(evolution_runs.c.target_type)
                    .where(evolution_runs.c.id == run_id)
                    .with_for_update(),
                )
            ).scalar_one_or_none()
            if parent != "skill":
                raise ValueError("Skill checkpoint requires an existing Skill run")
            row = (
                (
                    await session.execute(
                        select(skill_evolution_state).where(
                            skill_evolution_state.c.run_id == run_id
                        ),
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["input_snapshot"] != snapshot:
                    raise ValueError("Skill input snapshot is immutable")
                return _record(row)
            now = now_shanghai()
            await session.execute(
                insert(skill_evolution_state).values(
                    run_id=run_id,
                    input_snapshot=snapshot,
                    checkpoint={},
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return SkillEvolutionStateRecord(run_id, snapshot, {}, 0, now, now)

    async def get(self, run_id: str) -> SkillEvolutionStateRecord | None:
        """Read independently owned copies of the frozen input and latest checkpoint."""
        async with self._database.session() as session:
            row = (
                (
                    await session.execute(
                        select(skill_evolution_state).where(
                            skill_evolution_state.c.run_id == run_id
                        ),
                    )
                )
                .mappings()
                .first()
            )
            return _record(row) if row is not None else None

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> SkillEvolutionStateRecord:
        """Replace completed state using compare-and-swap, never modifying frozen input."""
        payload = _payload(checkpoint)
        async with self._database.transaction() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(skill_evolution_state)
                    .where(skill_evolution_state.c.run_id == run_id)
                    .where(skill_evolution_state.c.revision == expected_revision)
                    .values(
                        checkpoint=payload,
                        revision=expected_revision + 1,
                        updated_at=now_shanghai(),
                    ),
                ),
            )
            if result.rowcount != 1:
                raise SkillCheckpointConflict("Skill checkpoint revision changed or is missing")
            row = (
                (
                    await session.execute(
                        select(skill_evolution_state).where(
                            skill_evolution_state.c.run_id == run_id
                        ),
                    )
                )
                .mappings()
                .one()
            )
            return _record(row)

    async def append_feedback_plan(
        self,
        run_id: str,
        plan: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> SkillEvolutionStateRecord:
        """Atomically add snapshot-owned feedback rounds without writing upstream probes.

        Call only after all existing candidate units finish, before finalization.
        The cursor remains unchanged; the additional clusters extend its total.
        Caller supplies already-validated plan and feedback provenance in checkpoint.
        """
        plan_data = _payload(plan)
        payload = _payload(checkpoint)
        clusters = plan_data.get("clusters")
        if not isinstance(clusters, (list, tuple)) or not clusters:
            raise ValueError("feedback plan requires clusters")
        mapping = payload.setdefault("cluster_map", {})
        if not isinstance(mapping, dict):
            raise ValueError("invalid feedback cluster map")
        now = now_shanghai()
        async with self._database.transaction() as session:
            run = (
                (
                    await session.execute(
                        select(evolution_runs)
                        .where(evolution_runs.c.id == run_id)
                        .with_for_update(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                run is None
                or run["target_type"] != "skill"
                or run["mode"] != "full"
                or run["status"] != "running"
            ):
                raise SkillCheckpointConflict("feedback requires an active full Skill run")
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(probe_clusters)
                    .where(probe_clusters.c.run_id == run_id),
                )
            ).scalar_one()
            rounds: Sequence[str] = (
                (
                    await session.execute(
                        select(evolution_rounds.c.status).where(
                            evolution_rounds.c.run_id == run_id
                        ),
                    )
                )
                .scalars()
                .all()
            )
            if (
                run["progress_unit"] != 4 + count
                or len(rounds) != count
                or any(status not in ("accepted", "rejected", "skipped") for status in rounds)
            ):
                raise SkillCheckpointConflict("feedback requires completed candidate units")
            for offset, cluster in enumerate(clusters, 1):
                if (
                    not isinstance(cluster, dict)
                    or cluster.get("attribute_type") not in ("model", "prompt", "error")
                    or cluster.get("disposition")
                    not in ("selected", "low_confidence", "candidate_limit")
                ):
                    raise ValueError("invalid feedback cluster")
                cluster_id = new_id()
                mapping[cluster_id] = cluster
                critic = cluster["critic"]
                await session.execute(
                    insert(probe_clusters).values(
                        probe_cluster_id=cluster_id,
                        run_id=run_id,
                        seq=count + offset,
                        probe_cluster_description=cluster["probe_cluster_description"],
                        probe_cluster_type=cluster["attribute_type"],
                        confidence=critic["confidence"],
                        impact=1.0,
                        severity=cluster["priority"],
                        rank=count + offset,
                        created_at=now,
                    )
                )
                await session.execute(
                    insert(evolution_rounds).values(
                        id=new_id(),
                        run_id=run_id,
                        probe_cluster_id=cluster_id,
                        seq=count + offset,
                        status="pending" if cluster["disposition"] == "selected" else "skipped",
                        eval_status="pending"
                        if cluster["disposition"] == "selected"
                        else "skipped",
                    )
                )
            payload["plan"] = plan_data
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(skill_evolution_state)
                    .where(
                        skill_evolution_state.c.run_id == run_id,
                        skill_evolution_state.c.revision == expected_revision,
                    )
                    .values(checkpoint=payload, revision=expected_revision + 1, updated_at=now),
                ),
            )
            if result.rowcount != 1:
                raise SkillCheckpointConflict("Skill checkpoint revision changed or is missing")
            stats = deepcopy(dict(run["stats"] or {}))
            stats["clusters"] = count + len(clusters)
            await session.execute(
                update(evolution_runs)
                .where(
                    evolution_runs.c.id == run_id,
                )
                .values(
                    stats=stats,
                    progress_unit=evolution_runs.c.progress_unit
                    + sum(cluster["disposition"] != "selected" for cluster in clusters),
                )
            )
            row = (
                (
                    await session.execute(
                        select(skill_evolution_state).where(
                            skill_evolution_state.c.run_id == run_id,
                        )
                    )
                )
                .mappings()
                .one()
            )
            return _record(row)
