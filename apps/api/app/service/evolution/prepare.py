"""Real evolution preparation: workspace, clustering, scoring, and rounds."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, cast

from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeHitQuery, ProbeHitRow, ProbeQueryRepository
from app.db.models.evolution import (
    ClusterWrite,
    EvolutionRunRecord,
    ProbeClusterRecord,
)
from app.db.models.jobs import JobRunRecord
from app.errors import AppError, ErrorCode
from app.service.evolution.cluster import (
    TYPE_IMPACT,
    ProbeForCluster,
    simple_cluster,
)
from app.service.evolution.engine.contracts import RunBlocked
from app.service.evolution.events import (
    EvolutionRunEventService,
    EvolutionRunEventStage,
    EvolutionRunEventType,
)
from app.service.evolution.git_failures import classify_git_failure
from app.third_party_api.git.workspace import (
    GitOperationError,
    PreparedWorkspace,
)
from app.utils.ids import new_id
from app.utils.redaction import sanitize_message

logger = logging.getLogger(__name__)


class PrepareEventRecorder(Protocol):
    """Durable event operations consumed by prepare orchestration."""

    async def record(
        self,
        run_id: str,
        event_type: EvolutionRunEventType,
        *,
        level: str,
        stage: EvolutionRunEventStage,
        message: str,
        step_index: int | None = None,
        cluster_id: str | None = None,
        round_id: str | None = None,
        job_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> object:
        """Append one sanitized phase event."""


class WorkspacePreparer(Protocol):
    """Git workspace operations consumed by prepare orchestration."""

    async def prepare(self, run: EvolutionRunRecord) -> PreparedWorkspace:
        """Prepare one isolated repository workspace."""


@dataclass(frozen=True, slots=True)
class PrepareProbe:
    """One probe plus every reverse-linked bad case used for clustering."""

    probe_id: str
    attribute_type: str
    session_id: str
    bad_case_ids: tuple[str, ...]
    description: str
    analysis: str


class RepositoryProbeReader:
    """Adapt the focused probe query repository to prepare orchestration."""

    def __init__(self, repository: ProbeQueryRepository) -> None:
        """Bind the read-only provider repository."""
        self._repository = repository

    async def list_matching(self, query: ProbeHitQuery) -> tuple[PrepareProbe, ...]:
        """Group joined hit rows into one stable item per probe."""
        rows = await self._repository.list_hit_rows(query)
        grouped: OrderedDict[str, list[ProbeHitRow]] = OrderedDict()
        for row in rows:
            grouped.setdefault(row.probe_id, []).append(row)
        probes: list[PrepareProbe] = []
        for probe_id, probe_rows in grouped.items():
            first = probe_rows[0]
            if first.attribute_type not in TYPE_IMPACT:
                continue
            probes.append(
                PrepareProbe(
                    probe_id=probe_id,
                    attribute_type=first.attribute_type,
                    session_id=first.probe_session_id,
                    bad_case_ids=tuple(dict.fromkeys(row.bad_case_id for row in probe_rows)),
                    description=first.probe_description,
                    analysis=first.probe_analysis,
                ),
            )
        return tuple(probes)


class EvolutionPrepareService:
    """Run the real prepare workflow through durable repositories."""

    def __init__(
        self,
        runs: EvolutionRepository,
        probes: RepositoryProbeReader,
        workspaces: WorkspacePreparer,
        events: PrepareEventRecorder | EvolutionRunEventService | None = None,
        *,
        skill_core: Any = None,
        skill_states: Any = None,
    ) -> None:
        """Bind persistence, source probes, git workspaces, and events."""
        self._runs = runs
        self._probes = probes
        self._workspaces = workspaces
        self._events = events
        self._skill_core = skill_core
        self._skill_states = skill_states

    async def handle(self, job: JobRunRecord) -> Mapping[str, Any]:
        """Prepare one run idempotently and return its durable summary."""
        run_id = job.ref_id or _payload_run_id(job.payload)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise LookupError("evolution run not found")
        if run.target_type == "skill" and run.mode == "full" and self._skill_core is None:
            existing_state = (
                await self._skill_states.get(run.id) if self._skill_states is not None else None
            )
            if run.eval_config.get("skillEvolution") == "real" or existing_state is not None:
                raise ValueError("Real Skill model is unavailable; refusing placeholder fallback")
        if await self._already_prepared(run):
            return {
                "runId": run.id,
                "status": run.status,
                "progressUnit": run.progress_unit,
                "reused": True,
            }

        if (
            run.target_type == "skill"
            and run.mode == "full"
            and self._skill_core is not None
            and (
                run.eval_config.get("skillEvolution") == "real"
                or (
                    self._skill_states is not None
                    and await self._skill_states.get(run.id) is not None
                )
            )
        ):
            return await self._prepare_skill(run, job)

        query = _query_from_payload(job.payload, run)
        probes = await self._probes.list_matching(query)
        if not probes:
            raise AppError(
                status_code=409,
                code=ErrorCode.NO_PROBES_MATCHED,
                message="no analyzed probe matches the prepared run",
            )

        drafts = simple_cluster(
            tuple(
                ProbeForCluster(
                    probe_id=probe.probe_id,
                    attribute_type=cast(Any, probe.attribute_type),
                )
                for probe in probes
            ),
        )
        cluster_count = len(drafts)
        if run.progress_unit == 0:
            await self._record(
                run.id,
                "prepare_started",
                stage="prepare",
                message="准备阶段已启动",
                job_id=job.id,
            )
            await self._runs.advance_unit(
                run.id,
                expected=0,
                target=1,
                cluster_count=cluster_count,
            )

        workspace = None
        if run.mode == "full":
            try:
                workspace = await self._workspaces.prepare(run)
            except GitOperationError as exc:
                disposition = classify_git_failure(exc)
                if disposition.action == "blocked" and disposition.blocked_stage is not None:
                    await self._mark_blocked(
                        run.id,
                        exc,
                        blocked_stage=disposition.blocked_stage,
                        event_stage=disposition.error_stage,
                    )
                    raise RunBlocked(
                        "run branch publication failed",
                        stage=disposition.blocked_stage,
                        error_stage=disposition.error_stage,
                    ) from None
                raise
            await self._record(
                run.id,
                "workspace_prepared",
                stage="prepare",
                message="隔离 workspace 与 run 分支已准备",
                job_id=job.id,
                payload={
                    "baseCommit": workspace.base_commit,
                    "branch": workspace.branch,
                },
            )

        if run.progress_unit <= 1:
            clusters = await self._runs.replace_clusters(
                run.id,
                tuple(
                    ClusterWrite(
                        type=draft.type,
                        description=draft.description,
                        probes=draft.probes,
                        impact=float(TYPE_IMPACT[draft.type]),
                    )
                    for draft in drafts
                ),
            )
        else:
            clusters = await self._runs.list_clusters(run.id)
        cluster_probes = {
            cluster.probe_cluster_id: drafts[index].probes for index, cluster in enumerate(clusters)
        }
        if run.progress_unit <= 1:
            await self._runs.advance_unit(
                run.id,
                expected=1,
                target=2,
                cluster_count=cluster_count,
            )
            await self._record(
                run.id,
                "clusters_created",
                stage="cluster",
                step_index=1,
                message=f"聚类完成：{len(clusters)} 个簇",  # noqa: RUF001
                job_id=job.id,
                payload={"clusters": len(clusters)},
            )

        probe_by_id = {probe.probe_id: probe for probe in probes}
        if run.progress_unit <= 2:
            confidence_values = {
                cluster.probe_cluster_id: _placeholder_confidence(
                    cluster_probes[cluster.probe_cluster_id],
                    probe_by_id,
                )
                for cluster in clusters
            }
            await self._runs.write_cluster_confidence(run.id, confidence_values)
            await self._record(
                run.id,
                "step_advanced",
                stage="cluster",
                step_index=2,
                message="归因置信度已落库",
                job_id=job.id,
            )
        else:
            confidence_values = {
                cluster.probe_cluster_id: cluster.confidence or 0.0 for cluster in clusters
            }

        if run.progress_unit <= 3:
            severity_values = _severity_values(
                clusters,
                confidence_values,
                cluster_probes,
                probe_by_id,
            )
            await self._runs.write_cluster_severity(run.id, severity_values)
            await self._record(
                run.id,
                "step_advanced",
                stage="cluster",
                step_index=3,
                message="严重程度排序已落库",
                job_id=job.id,
            )
            clusters = await self._runs.list_clusters(run.id)
        else:
            clusters = await self._runs.list_clusters(run.id)

        if run.mode == "full":
            ordered = sorted(
                clusters,
                key=lambda cluster: (
                    cluster.rank or 0,
                    cluster.seq,
                ),
            )
            existing_rounds = await self._runs.list_rounds(run.id)
            if not existing_rounds:
                await self._runs.insert_rounds(
                    tuple(
                        {
                            "id": new_id(),
                            "run_id": run.id,
                            "probe_cluster_id": cluster.probe_cluster_id,
                            "seq": seq,
                            "status": "pending",
                        }
                        for seq, cluster in enumerate(ordered, start=1)
                    ),
                )
            elif len(existing_rounds) != len(ordered):
                raise AppError(
                    status_code=409,
                    code=ErrorCode.VALIDATION_INVALID,
                    message="prepared run has an incomplete round snapshot",
                )

        prepared = await self._runs.mark_prepared(
            run.id,
            status="running" if run.mode == "full" else "completed",
            progress_unit=4,
            repo_path=str(workspace.repo_path) if workspace else None,
            base_ref=workspace.ref if workspace else run.target_version,
            base_commit=workspace.base_commit if workspace else None,
            branch_name=workspace.branch if workspace else None,
            workspace_path=str(workspace.path) if workspace else None,
            stats=_stats(probes, clusters),
        )
        return {
            "runId": prepared.id,
            "status": prepared.status,
            "progressUnit": prepared.progress_unit,
            "clusters": len(clusters),
            "rounds": len(clusters) if run.mode == "full" else 0,
        }

    async def _prepare_skill(self, run: EvolutionRunRecord, job: JobRunRecord) -> Mapping[str, Any]:
        """Freeze inputs once, then persist verified Skill clusters and eligible rounds."""
        from dataclasses import asdict, replace
        from pathlib import Path

        from app.service.evolution.skill_safety import (
            MAX_FILE_BYTES,
            MAX_FILES,
            MAX_TOTAL_BYTES,
            reject_sensitive_payload,
            validate_patch,
        )

        if self._skill_states is None or self._skill_core is None:
            raise ValueError("Skill evolution dependencies are missing")
        state = await self._skill_states.get(run.id)
        frozen_run = (
            replace(run, target_version=str(state.input_snapshot["base_commit"]))
            if state is not None
            else run
        )
        workspace = await self._workspaces.prepare(frozen_run)
        if state is None:
            probes = await self._probes.list_matching(_query_from_payload(job.payload, run))
            if not probes:
                raise ValueError("no analyzed Skill probes matched")
            root = workspace.path / run.sub_dir
            if root.is_symlink():
                raise ValueError("Skill directory cannot be a symlink")
            root.resolve().relative_to(workspace.path.resolve())
            files: dict[str, str] = {}
            total = 0
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                if ".git" in relative.parts:
                    continue
                if path.is_symlink():
                    raise ValueError("Skill tree contains a symlink")
                if not path.is_file():
                    continue
                size = path.stat().st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                    raise ValueError("Skill tree exceeds text snapshot limits")
                files[relative.as_posix()] = path.read_text(encoding="utf-8")
            if "SKILL.md" not in files:
                raise ValueError("Skill tree requires SKILL.md")
            reports = [
                dict(
                    probe_id=p.probe_id,
                    probe_type="skill",
                    attribute_type=p.attribute_type,
                    probe_description=p.description,
                    probe_analysis=p.analysis,
                    session_id=p.session_id,
                    from_bad_cases=[{"bad_case_id": key} for key in p.bad_case_ids],
                )
                for p in probes
            ]
            validate_patch(files, {})
            reject_sensitive_payload(reports)
            state = await self._skill_states.initialize(
                run.id,
                {
                    "reports": reports,
                    "files": files,
                    "base_commit": workspace.base_commit,
                },
            )
        from app.service.evolution.skill_identity import skill_identity

        checkpoint = dict(state.checkpoint)
        identity = skill_identity(self._skill_core)
        if checkpoint.get("identity", identity) != identity:
            raise ValueError("Skill model or strategy changed since preparation")
        checkpoint["identity"] = identity
        if "plan" not in checkpoint:
            plan = await self._skill_core.plan(
                list(state.input_snapshot["reports"]),
                dict(state.input_snapshot["files"]),
            )
            checkpoint.update(plan=asdict(plan), best_files=dict(state.input_snapshot["files"]))
            state = await self._skill_states.save_checkpoint(
                run.id,
                checkpoint,
                expected_revision=state.revision,
            )
        plan_data = checkpoint["plan"]
        planned = plan_data["clusters"]
        current = await self._runs.get_run(run.id)
        if current is None:
            raise LookupError("evolution run disappeared")
        if current.progress_unit == 0:
            await self._runs.advance_unit(run.id, expected=0, target=1, cluster_count=len(planned))
        if current.progress_unit <= 1:
            clusters = await self._runs.replace_clusters(
                run.id,
                tuple(
                    ClusterWrite(
                        type=item["attribute_type"],
                        description=item["probe_cluster_description"],
                        probes=tuple(item["source_probe_ids"]),
                        impact=1.0,
                    )
                    for item in planned
                ),
            )
            await self._runs.advance_unit(run.id, expected=1, target=2, cluster_count=len(planned))
        else:
            clusters = await self._runs.list_clusters(run.id)
        mapping = {
            cluster.probe_cluster_id: item for cluster, item in zip(clusters, planned, strict=True)
        }
        if current.progress_unit <= 2:
            await self._runs.write_cluster_confidence(
                run.id, {key: item["critic"]["confidence"] for key, item in mapping.items()}
            )
        if current.progress_unit <= 3:
            await self._runs.write_cluster_severity(
                run.id, {key: (item["priority"], item["rank"]) for key, item in mapping.items()}
            )
        if not await self._runs.list_rounds(run.id):
            await self._runs.insert_rounds(
                tuple(
                    {
                        "id": new_id(),
                        "run_id": run.id,
                        "probe_cluster_id": key,
                        "seq": seq,
                        "status": "pending" if item["disposition"] == "selected" else "skipped",
                        "eval_status": "pending"
                        if item["disposition"] == "selected"
                        else "skipped",
                    }
                    for seq, (key, item) in enumerate(mapping.items(), 1)
                )
            )
        baseline_ref = f"evo-snapshot/{run.id}/baseline"
        await cast(Any, self._workspaces).publish_skill_snapshot(
            Path(workspace.path),
            branch=baseline_ref,
            commit=workspace.base_commit,
        )
        checkpoint.update(cluster_map=mapping, baseline_ref=baseline_ref)
        await self._skill_states.save_checkpoint(
            run.id, checkpoint, expected_revision=state.revision
        )
        prepared = await self._runs.mark_prepared(
            run.id,
            status="running",
            progress_unit=4,
            repo_path=str(workspace.repo_path),
            base_ref=workspace.ref,
            base_commit=workspace.base_commit,
            branch_name=workspace.branch,
            workspace_path=str(workspace.path),
            stats={"clusters": len(clusters)},
            skipped_rounds=sum(item["disposition"] != "selected" for item in planned),
        )
        return {
            "runId": run.id,
            "status": prepared.status,
            "progressUnit": prepared.progress_unit,
            "clusters": len(clusters),
            "rounds": len(clusters),
        }

    async def _already_prepared(self, run: EvolutionRunRecord) -> bool:
        """Detect a retried prepare that already committed this exact workflow."""
        if run.progress_unit < 4:
            return False
        if run.mode == "cluster_only":
            return run.status == "completed"
        if run.status == "preparing":
            return False
        return bool(await self._runs.list_rounds(run.id))

    async def _record(
        self,
        run_id: str,
        event_type: EvolutionRunEventType,
        *,
        stage: EvolutionRunEventStage,
        message: str,
        job_id: str | None = None,
        step_index: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one best-effort phase event without masking domain work."""
        if self._events is None:
            return
        try:
            await self._events.record(
                run_id,
                event_type,
                level="info",
                stage=stage,
                message=message,
                step_index=step_index,
                job_id=job_id,
                payload=payload,
            )
        except Exception:
            logger.exception(
                "failed to record prepare event",
                extra={"run_id": run_id, "event_type": event_type},
            )

    async def _mark_blocked(
        self,
        run_id: str,
        error: GitOperationError,
        *,
        blocked_stage: str,
        event_stage: str,
    ) -> None:
        """Persist retryable state once; JobRunner owns the blocked event."""
        await self._runs.mark_blocked(
            run_id,
            stage=blocked_stage,
            message=sanitize_message(str(error), limit=2_000),
            error_stage=event_stage,
        )


def _query_from_payload(
    payload: Mapping[str, Any],
    run: EvolutionRunRecord,
) -> ProbeHitQuery:
    """Reconstruct the frozen launch query or fall back to run snapshots."""
    raw = payload.get("query")
    query = raw if isinstance(raw, Mapping) else {}
    models_value = query.get("models", run.models)
    models = (
        tuple(item for item in models_value if isinstance(item, str))
        if isinstance(models_value, (list, tuple))
        else ()
    )
    return ProbeHitQuery(
        target_type=_text(query.get("targetType")) or run.target_type,
        target_name=_text(query.get("targetName")) or run.target_name,
        target_version=_text(query.get("targetVersion")) or run.target_version,
        window_from=_date(query.get("from")) or run.window_from,
        window_to=_date(query.get("to")) or run.window_to,
        models=models,
    )


def _payload_run_id(payload: Mapping[str, Any]) -> str:
    """Read the camelCase run identifier used by queued launch jobs."""
    value = payload.get("runId", payload.get("run_id"))
    if isinstance(value, str) and value:
        return value
    raise ValueError("evolution prepare job has no run id")


def _text(value: object) -> str | None:
    """Return a non-empty text value without coercing foreign input."""
    return value if isinstance(value, str) and value else None


def _date(value: object) -> date | None:
    """Parse one optional ISO launch-window date."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _placeholder_confidence(
    probe_ids: tuple[str, ...],
    probes: Mapping[str, PrepareProbe],
) -> float:
    """Derive a stable V1 confidence from analysis presence and cluster size."""
    linked = [probes[probe_id] for probe_id in probe_ids if probe_id in probes]
    if not linked:
        return 0.0
    analyzed = sum(1 for probe in linked if probe.analysis.strip())
    return round(min(0.95, 0.6 + 0.35 * analyzed / len(linked)), 2)


def _severity_values(
    clusters: tuple[ProbeClusterRecord, ...],
    confidence: Mapping[str, float],
    cluster_probes: Mapping[str, tuple[str, ...]],
    probes: Mapping[str, PrepareProbe],
) -> dict[str, tuple[float, int]]:
    """Compute bounded severity and assign deterministic ranks."""
    raw: list[tuple[str, float]] = []
    for cluster in clusters:
        bad_cases = {
            bad_case_id
            for probe_id in cluster_probes.get(cluster.probe_cluster_id, ())
            for bad_case_id in probes.get(
                probe_id, PrepareProbe("", "", "", (), "", "")
            ).bad_case_ids
        }
        report_count = max(len(bad_cases), 1)
        weight = 1 + 0.1 * min(report_count - 1, 5)
        severity = round(
            min(
                10.0,
                confidence[cluster.probe_cluster_id] * float(cluster.impact or 0) * weight,
            ),
            1,
        )
        raw.append((cluster.probe_cluster_id, severity))
    ordered = sorted(
        raw,
        key=lambda item: (-item[1], next(c.seq for c in clusters if c.probe_cluster_id == item[0])),
    )
    return {
        cluster_id: (severity, rank) for rank, (cluster_id, severity) in enumerate(ordered, start=1)
    }


def _stats(
    probes: tuple[PrepareProbe, ...],
    clusters: tuple[ProbeClusterRecord, ...],
) -> dict[str, Any]:
    """Persist list-level counts from the prepared probe and cluster facts."""
    by_type = {"model": 0, "prompt": 0, "error": 0, "nonIssue": 0}
    for probe in probes:
        if probe.attribute_type in by_type:
            by_type[probe.attribute_type] += 1
    return {
        "sessions": len({probe.session_id for probe in probes}),
        "probes": len(probes),
        "clusters": len(clusters),
        "byAttributeType": by_type,
    }
