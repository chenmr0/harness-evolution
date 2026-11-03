"""Six-step read-only stage projection owned by the backend (05 §3.6, 07 §5.10).

One shared `build_step_payload` assembles each step DTO from persisted columns;
confidence, impact, severity, and rank are read from `probe_clusters` and never
recomputed, and step status/clickable come from the single m5-01 unit-cursor
state machine instead of any per-endpoint copy.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime
from typing import Any, Literal, cast

from pydantic import Field

from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeHitRow, ProbeQueryRepository
from app.db.crud.skill_evolution import SkillEvolutionRepository
from app.db.models.evolution import (
    EvolutionRoundRecord,
    EvolutionRunRecord,
    ProbeClusterRecord,
)
from app.errors import AppError, ErrorCode
from app.service.evolution.probe_query import (
    AttributeStats,
    ProbeHitSummary,
    ProbePreviewItem,
    probe_row_of,
)
from app.service.evolution.state import (
    STEP_KEYS,
    STEP_TITLES,
    StepNotReady,
    ensure_step_readable,
    step_states,
)
from app.service.identity.schemas import IdentityModel

StepKey = Literal["input", "clusters", "attribution", "severity", "iteration", "final"]
StepStatus = Literal["done", "active", "pending"]
ClusterType = Literal["model", "prompt", "error"]
RoundStatus = Literal[
    "pending",
    "patching",
    "pushing",
    "evaluating",
    "deciding",
    "accepted",
    "rejected",
    "skipped",
    "failed",
]
RoundPushStatus = Literal["pending", "pushing", "succeeded", "skipped", "failed"]
RoundEvalStatus = Literal["pending", "evaluating", "completed", "failed", "timeout", "skipped"]
_CLUSTER_TYPES: tuple[str, ...] = ("model", "prompt", "error")


class StepCondition(IdentityModel):
    """Filter condition snapshotted when the run was admitted."""

    target_type: Literal["skill", "agent"]
    target_name: str
    target_version: str
    window_from: date | None = Field(default=None, alias="from")
    window_to: date | None = Field(default=None, alias="to")
    models: list[str]


class StepInputRow(ProbePreviewItem):
    """Shared ProbeRow plus the owning cluster for the input table."""

    cluster_id: str | None = None


class StepInputPayload(IdentityModel):
    """Filter condition, hit statistics, and probe rows of step 0."""

    condition: StepCondition
    hits: ProbeHitSummary
    rows: list[StepInputRow]


class StepCluster(IdentityModel):
    """One persisted cluster with its probe rows and stored scores."""

    probe_cluster_id: str
    review_status: Literal["pending", "verifying", "verified", "failed", "interrupted"] | None = (
        None
    )
    review_error: str | None = None
    title: str
    description: str
    type: ClusterType
    probe_count: int
    confidence: float | None
    severity: float | None
    probes: list[ProbePreviewItem]


class StepClustersPayload(IdentityModel):
    """Cluster list of step 1 in creation order."""

    clusters: list[StepCluster]


class StepAttributionItem(IdentityModel):
    """One probe attribution entry with its cluster-level confidence."""

    cluster_id: str
    probe_id: str
    probe_object: str
    object_version: str | None
    attribute_type: ClusterType
    description: str
    analysis: str
    confidence: float | None


class StepAttributionPayload(IdentityModel):
    """Per-probe attribution entries of step 2."""

    items: list[StepAttributionItem]


class StepSeverityRow(IdentityModel):
    """One ranked cluster row backed by persisted severity columns."""

    rank: int | None
    cluster_id: str
    title: str
    type: ClusterType
    probe_count: int
    report_count: int
    confidence: float | None
    impact: float | None
    severity: float | None


class StepSeverityPayload(IdentityModel):
    """Severity rows of step 3 ordered by stored severity."""

    rows: list[StepSeverityRow]


class StepCandidate(IdentityModel):
    """Pushed candidate ref backing one evaluation round."""

    ref: str
    commit: str
    message: str


class StepRoundPatch(IdentityModel):
    """Placeholder patch preview stored with the round."""

    file: str
    lines: list[str]


class StepRoundPush(IdentityModel):
    """Candidate push outcome stored on the round."""

    status: RoundPushStatus
    attempts: int
    error: str | None


class StepRoundEval(IdentityModel):
    """External evaluation state stored on the round."""

    status: RoundEvalStatus
    current: float | None
    candidate: float | None
    next_poll_at: datetime | None = None


class StepFinalCommit(IdentityModel):
    """Amended run-branch commit produced when a round was accepted."""

    hash: str
    message: str


class StepRound(IdentityModel):
    """One execution round rendered by the iteration step."""

    round_id: str
    cluster_id: str
    title: str
    type: ClusterType
    probe_count: int
    confidence: float | None
    severity: float | None
    probes: list[ProbePreviewItem]
    status: RoundStatus
    candidate: StepCandidate | None
    patch: StepRoundPatch | None
    push: StepRoundPush
    eval: StepRoundEval
    accepted: bool
    final_commit: StepFinalCommit | None = None


class StepIterationPayload(IdentityModel):
    """Rounds of step 4 in sequence order."""

    rounds: list[StepRound]


class StepFinalChange(IdentityModel):
    """One accepted round inside the terminal modification list."""

    commit: str
    cluster_id: str
    title: str
    type: ClusterType
    report_count: int


class StepFinalPayload(IdentityModel):
    """Terminal evaluation verdict with metrics and accepted changes.

    The four verdict fields stay null until `evolution_finals` has a row, so an
    active final step cannot be mistaken for a failing verdict.
    """

    verdict: bool | None
    best_version: str | None
    best_score: float | None
    init_score: float | None
    metrics: list[dict[str, Any]]
    changes: list[StepFinalChange]


class EvolutionStepShell(IdentityModel):
    """Envelope fields shared by the six step endpoints."""

    index: int
    title: str
    status: StepStatus
    clickable: bool
    detail: str | None = None


class EvolutionStepInput(EvolutionStepShell):
    """Step 0 response: key-discriminated input projection."""

    key: Literal["input"] = "input"
    payload: StepInputPayload


class EvolutionStepClusters(EvolutionStepShell):
    """Step 1 response: key-discriminated cluster projection."""

    key: Literal["clusters"] = "clusters"
    payload: StepClustersPayload


class EvolutionStepAttribution(EvolutionStepShell):
    """Step 2 response: key-discriminated attribution projection."""

    key: Literal["attribution"] = "attribution"
    payload: StepAttributionPayload


class EvolutionStepSeverity(EvolutionStepShell):
    """Step 3 response: key-discriminated severity projection."""

    key: Literal["severity"] = "severity"
    payload: StepSeverityPayload


class EvolutionStepIteration(EvolutionStepShell):
    """Step 4 response: key-discriminated iteration projection."""

    key: Literal["iteration"] = "iteration"
    payload: StepIterationPayload


class EvolutionStepFinal(EvolutionStepShell):
    """Step 5 response: key-discriminated final projection."""

    key: Literal["final"] = "final"
    payload: StepFinalPayload


EvolutionStep = (
    EvolutionStepInput
    | EvolutionStepClusters
    | EvolutionStepAttribution
    | EvolutionStepSeverity
    | EvolutionStepIteration
    | EvolutionStepFinal
)


class EvolutionStageService:
    """Project one run's persisted stage data through shared readiness rules."""

    def __init__(
        self,
        runs: EvolutionRepository,
        probes: ProbeQueryRepository,
        *,
        skill_states: SkillEvolutionRepository | None = None,
    ) -> None:
        """Bind the service to its run and probe read repositories."""
        self._runs = runs
        self._probes = probes
        self._skill_states = skill_states

    async def step(self, run_id: str, key: StepKey) -> EvolutionStep:
        """Return one readable step or fail with the documented 404 codes.

        Args:
            run_id: Stable run identifier from the URL path.
            key: Requested step key inside the closed six-key vocabulary.

        Returns:
            The key-discriminated step envelope with its payload.

        Raises:
            AppError: With `RUN_NOT_FOUND` for an unknown run and
                `STEP_NOT_READY` when the step has not been reached or the run
                mode hides it (cluster_only keeps only the first two steps).
        """
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AppError(
                status_code=404,
                code=ErrorCode.RUN_NOT_FOUND,
                message="evolution run not found",
            )
        clusters = await self._runs.list_clusters(run_id)
        try:
            ensure_step_readable(run.progress_unit, len(clusters), run.status, key, run.mode)
        except StepNotReady as exc:
            raise AppError(
                status_code=404,
                code=ErrorCode.STEP_NOT_READY,
                message="requested evolution step has not been reached",
                details={"key": key},
            ) from exc
        return await self.build_step_payload(run, clusters, key)

    async def build_step_payload(
        self,
        run: EvolutionRunRecord,
        clusters: Sequence[ProbeClusterRecord],
        key: StepKey,
    ) -> EvolutionStep:
        """Assemble one step DTO from persisted columns without recomputing scores.

        Args:
            run: Persisted run row carrying cursor, status, and snapshots.
            clusters: The run's clusters in creation order.
            key: Requested step key inside the closed six-key vocabulary.

        Returns:
            The key-discriminated step envelope with its payload.
        """
        state = await self._skill_states.get(run.id) if self._skill_states is not None else None
        probes_of_cluster: dict[str, list[str]] = OrderedDict()
        if state is not None:
            reports = {report["probe_id"]: report for report in state.input_snapshot["reports"]}
            reports.update(
                {
                    report["probe_id"]: report
                    for report in state.checkpoint.get("source_reports", [])
                }
            )
            for cluster in clusters:
                item = state.checkpoint.get("cluster_map", {}).get(cluster.probe_cluster_id, {})
                probes_of_cluster[cluster.probe_cluster_id] = list(item.get("source_probe_ids", []))
            cluster_of_probe = {pid: cid for cid, ids in probes_of_cluster.items() for pid in ids}
            selected_reports = (
                state.input_snapshot["reports"] if key == "input" else list(reports.values())
            )
            grouped = _snapshot_rows(run, list(selected_reports))
        else:
            links = await self._runs.list_cluster_probes(run.id)
            probe_ids = tuple(dict.fromkeys(link.probe_id for link in links))
            grouped = await self._probe_rows(probe_ids)
            cluster_of_probe = {link.probe_id: link.probe_cluster_id for link in links}
            for link in links:
                probes_of_cluster.setdefault(link.probe_cluster_id, []).append(link.probe_id)
        builders: dict[StepKey, Callable[[], Awaitable[EvolutionStep]]] = {
            "input": lambda: self._input(run, clusters, cluster_of_probe, grouped),
            "clusters": lambda: self._clusters(
                run, clusters, probes_of_cluster, grouped, state.checkpoint if state else {}
            ),
            "attribution": lambda: self._attribution(
                run,
                clusters,
                probes_of_cluster,
                grouped,
            ),
            "severity": lambda: self._severity(run, clusters, probes_of_cluster, grouped),
            "iteration": lambda: self._iteration(run, clusters, probes_of_cluster, grouped),
            "final": lambda: self._final(run, clusters, probes_of_cluster, grouped),
        }
        return await builders[key]()

    async def _probe_rows(
        self,
        probe_ids: tuple[str, ...],
    ) -> dict[str, list[ProbeHitRow]]:
        """Group joined hit rows per probe while preserving repository order.

        Args:
            probe_ids: Probe identifiers linked to the run's clusters.

        Returns:
            Mapping from probe id to its joined case/session hit rows.
        """
        rows_by_probe: dict[str, list[ProbeHitRow]] = OrderedDict()
        for row in await self._probes.list_rows_by_ids(probe_ids):
            rows_by_probe.setdefault(row.probe_id, []).append(row)
        return rows_by_probe

    def _shell(
        self,
        run: EvolutionRunRecord,
        cluster_count: int,
        key: StepKey,
        rounds: Sequence[EvolutionRoundRecord] = (),
    ) -> dict[str, Any]:
        """Return the shared envelope fields from the unit-cursor state machine.

        Args:
            run: Persisted run row carrying cursor and status.
            cluster_count: Number of clusters behind the run's cursor range.
            key: Requested step key inside the closed six-key vocabulary.
            rounds: Persisted rounds used only for the active-round detail copy.

        Returns:
            Keyword fields for one step envelope constructor.
        """
        index = STEP_KEYS.index(key)
        state = step_states(run.progress_unit, cluster_count, run.status)[index]
        detail = None
        if state.status == "active" and key == "iteration" and rounds:
            done = max(run.progress_unit - 4, 0)
            detail = f"第 {done + 1}/{len(rounds)} 轮"
        return {
            "index": index,
            "title": STEP_TITLES[index],
            "status": state.status,
            "clickable": state.clickable,
            "detail": detail,
        }

    def _report_count(
        self,
        probe_ids: Sequence[str],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> int:
        """Count distinct source bad cases behind one cluster's probes.

        Args:
            probe_ids: Probe identifiers linked to one cluster.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The distinct source bad-case count used by severity and changes.
        """
        cases: set[str] = set()
        for probe_id in probe_ids:
            for row in grouped.get(probe_id, []):
                if row.bad_case_id:
                    cases.add(row.bad_case_id)
        return len(cases)

    def _probe_items(
        self,
        probe_ids: Sequence[str],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> list[ProbePreviewItem]:
        """Render one cluster's ProbeRow list in stored link order.

        Args:
            probe_ids: Probe identifiers linked to one cluster.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            Shared ProbeRow DTOs in link order.
        """
        items: list[ProbePreviewItem] = []
        for probe_id in probe_ids:
            rows = grouped.get(probe_id, [])
            if rows:
                item = _project_probe(rows)
                if item is not None:
                    items.append(item)
        return items

    async def _input(
        self,
        run: EvolutionRunRecord,
        clusters: Sequence[ProbeClusterRecord],
        cluster_of_probe: dict[str, str],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> EvolutionStepInput:
        """Assemble the condition, hits, and probe rows of step 0.

        Args:
            run: Persisted run row carrying the condition snapshot.
            clusters: The run's clusters behind the shell state.
            cluster_of_probe: Mapping from probe id to owning cluster id.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The step 0 envelope with condition, hits, and rows.
        """
        distribution = AttributeStats()
        items: list[StepInputRow] = []
        for probe_id, rows in grouped.items():
            item = _project_probe(rows)
            if item is None:
                continue
            items.append(
                StepInputRow.model_validate(
                    {**item.model_dump(), "clusterId": cluster_of_probe.get(probe_id)},
                ),
            )
            distribution.model += item.attribute_type == "model"
            distribution.prompt += item.attribute_type == "prompt"
            distribution.error += item.attribute_type == "error"
        bad_cases = {
            row.bad_case_id for rows in grouped.values() for row in rows if row.bad_case_id
        }
        analyzed = {
            row.bad_case_id
            for rows in grouped.values()
            for row in rows
            if row.analysis_status == "analyzed_issue" and row.bad_case_id
        }
        sessions = {
            row.probe_session_id
            for rows in grouped.values()
            for row in rows
            if row.probe_session_id
        }
        probes = len(items)
        return EvolutionStepInput(
            **self._shell(run, len(clusters), "input"),
            payload=StepInputPayload(
                condition=StepCondition.model_validate(
                    {
                        "targetType": run.target_type,
                        "targetName": run.target_name,
                        "targetVersion": run.target_version,
                        "from": run.window_from,
                        "to": run.window_to,
                        "models": run.models,
                    },
                ),
                hits=ProbeHitSummary(
                    sessions=len(sessions),
                    bad_cases=len(bad_cases),
                    analyzed=len(analyzed),
                    unanalyzed=0,
                    probes=probes,
                    used_probes=(distribution.model + distribution.prompt + distribution.error),
                    by_attribute_type=distribution,
                ),
                rows=items,
            ),
        )

    async def _clusters(
        self,
        run: EvolutionRunRecord,
        clusters: Sequence[ProbeClusterRecord],
        probes_of_cluster: dict[str, list[str]],
        grouped: dict[str, list[ProbeHitRow]],
        checkpoint: dict[str, Any],
    ) -> EvolutionStepClusters:
        """Assemble the cluster list of step 1.

        Args:
            run: Persisted run row behind the step shell.
            clusters: The run's clusters in creation order.
            probes_of_cluster: Cluster id to linked probe ids.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The step 1 envelope with clusters and stored scores.
        """
        reviews = checkpoint.get("reviews", {})

        def review_status(cluster: ProbeClusterRecord) -> Any:
            review = reviews.get(cluster.probe_cluster_id)
            if review:
                if review["status"] == "verifying" and run.status in {"failed", "interrupted"}:
                    return "interrupted"
                return review["status"]
            if checkpoint.get("cluster_map", {}).get(cluster.probe_cluster_id, {}).get("critic"):
                return "verified"
            return "pending" if "partition" in checkpoint else None

        return EvolutionStepClusters(
            **self._shell(run, len(clusters), "clusters"),
            payload=StepClustersPayload(
                clusters=[
                    StepCluster(
                        probe_cluster_id=cluster.probe_cluster_id,
                        title=cluster.probe_cluster_description,
                        description=cluster.probe_cluster_description,
                        type=cluster.probe_cluster_type,
                        probe_count=len(probes_of_cluster.get(cluster.probe_cluster_id, [])),
                        review_status=review_status(cluster),
                        review_error=reviews.get(cluster.probe_cluster_id, {}).get("error"),
                        confidence=cluster.confidence
                        if cluster.confidence is not None
                        else reviews.get(cluster.probe_cluster_id, {})
                        .get("result", {})
                        .get("critic", {})
                        .get("confidence"),
                        severity=cluster.severity,
                        probes=self._probe_items(
                            probes_of_cluster.get(cluster.probe_cluster_id, []),
                            grouped,
                        ),
                    )
                    for cluster in clusters
                ],
            ),
        )

    async def _attribution(
        self,
        run: EvolutionRunRecord,
        clusters: Sequence[ProbeClusterRecord],
        probes_of_cluster: dict[str, list[str]],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> EvolutionStepAttribution:
        """Assemble per-probe attribution entries of step 2.

        Args:
            run: Persisted run row behind the step shell.
            clusters: The run's clusters carrying the shared confidence.
            probes_of_cluster: Cluster id to linked probe ids.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The step 2 envelope with one item per probe.
        """
        items: list[StepAttributionItem] = []
        for cluster in clusters:
            for probe_id in probes_of_cluster.get(cluster.probe_cluster_id, []):
                rows = grouped.get(probe_id, [])
                if not rows or rows[0].attribute_type not in _CLUSTER_TYPES:
                    continue
                first = rows[0]
                items.append(
                    StepAttributionItem(
                        cluster_id=cluster.probe_cluster_id,
                        probe_id=first.probe_id,
                        probe_object=first.probe_object,
                        object_version=first.object_version,
                        attribute_type=cast(ClusterType, first.attribute_type),
                        description=first.probe_description,
                        analysis=first.probe_analysis,
                        confidence=cluster.confidence,
                    ),
                )
        return EvolutionStepAttribution(
            **self._shell(run, len(clusters), "attribution"),
            payload=StepAttributionPayload(items=items),
        )

    async def _severity(
        self,
        run: EvolutionRunRecord,
        clusters: Sequence[ProbeClusterRecord],
        probes_of_cluster: dict[str, list[str]],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> EvolutionStepSeverity:
        """Assemble severity rows of step 3 ordered by stored severity.

        Args:
            run: Persisted run row behind the step shell.
            clusters: The run's clusters carrying stored severity columns.
            probes_of_cluster: Cluster id to linked probe ids.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The step 3 envelope with rows sorted by persisted severity.
        """
        rows = [
            StepSeverityRow(
                rank=cluster.rank,
                cluster_id=cluster.probe_cluster_id,
                title=cluster.probe_cluster_description,
                type=cluster.probe_cluster_type,
                probe_count=len(probes_of_cluster.get(cluster.probe_cluster_id, [])),
                report_count=self._report_count(
                    probes_of_cluster.get(cluster.probe_cluster_id, []),
                    grouped,
                ),
                confidence=cluster.confidence,
                impact=cluster.impact,
                severity=cluster.severity,
            )
            for cluster in clusters
        ]
        rows.sort(
            key=lambda row: (
                row.severity is None,
                -(row.severity or 0.0),
                row.rank if row.rank is not None else len(rows) + 1,
            ),
        )
        return EvolutionStepSeverity(
            **self._shell(run, len(clusters), "severity"),
            payload=StepSeverityPayload(rows=rows),
        )

    async def _iteration(
        self,
        run: EvolutionRunRecord,
        clusters: Sequence[ProbeClusterRecord],
        probes_of_cluster: dict[str, list[str]],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> EvolutionStepIteration:
        """Assemble the execution rounds of step 4.

        Args:
            run: Persisted run row behind the step shell.
            clusters: The run's clusters carrying shared facts per round.
            probes_of_cluster: Cluster id to linked probe ids.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The step 4 envelope with rounds in sequence order.
        """
        rounds = list(await self._runs.list_rounds(run.id))
        cluster_by_id = {cluster.probe_cluster_id: cluster for cluster in clusters}
        payload = [
            self._round(
                round_,
                cluster_by_id.get(round_.probe_cluster_id),
                probes_of_cluster,
                grouped,
            )
            for round_ in rounds
        ]
        return EvolutionStepIteration(
            **self._shell(run, len(clusters), "iteration", rounds=rounds),
            payload=StepIterationPayload(rounds=payload),
        )

    def _round(
        self,
        round_: EvolutionRoundRecord,
        cluster: ProbeClusterRecord | None,
        probes_of_cluster: dict[str, list[str]],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> StepRound:
        """Render one persisted round with its cluster facts and outcomes.

        Args:
            round_: Persisted round row to project.
            cluster: The round's owning cluster when still present.
            probes_of_cluster: Cluster id to linked probe ids.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The iteration round DTO with candidate, push, eval, and outcome.
        """
        probe_ids = probes_of_cluster.get(round_.probe_cluster_id, [])
        patch = round_.patch
        return StepRound(
            round_id=round_.id,
            cluster_id=round_.probe_cluster_id,
            title=cluster.probe_cluster_description if cluster else "",
            type=cluster.probe_cluster_type if cluster else "error",
            probe_count=len(probe_ids),
            confidence=cluster.confidence if cluster else None,
            severity=cluster.severity if cluster else None,
            probes=self._probe_items(probe_ids, grouped),
            status=cast(RoundStatus, round_.status),
            candidate=(
                StepCandidate(
                    ref=round_.candidate_ref or "",
                    commit=round_.candidate_commit or "",
                    message=round_.candidate_message or "",
                )
                if round_.candidate_ref is not None
                else None
            ),
            patch=_project_patch(patch),
            push=StepRoundPush(
                status=cast(RoundPushStatus, round_.push_status),
                attempts=round_.push_attempts,
                error=round_.push_error,
            ),
            eval=StepRoundEval(
                status=cast(RoundEvalStatus, round_.eval_status),
                current=round_.eval_current,
                candidate=round_.eval_candidate,
                next_poll_at=round_.eval_next_poll_at,
            ),
            accepted=round_.accepted,
            final_commit=(
                StepFinalCommit(
                    hash=round_.commit_hash or "",
                    message=round_.commit_message or "",
                )
                if round_.commit_hash is not None
                else None
            ),
        )

    async def _final(
        self,
        run: EvolutionRunRecord,
        clusters: Sequence[ProbeClusterRecord],
        probes_of_cluster: dict[str, list[str]],
        grouped: dict[str, list[ProbeHitRow]],
    ) -> EvolutionStepFinal:
        """Assemble the terminal evaluation of step 5.

        The verdict fields are null while `evolution_finals` has no row; the
        accepted changes stay visible so the active step still shows progress.

        Args:
            run: Persisted run row behind the step shell.
            clusters: The run's clusters carrying titles and types for changes.
            probes_of_cluster: Cluster id to linked probe ids.
            grouped: Joined hit rows grouped by probe id.

        Returns:
            The step 5 envelope with verdict, metrics, and accepted changes.
        """
        final = await self._runs.get_final(run.id)
        rounds = list(await self._runs.list_rounds(run.id))
        cluster_by_id = {cluster.probe_cluster_id: cluster for cluster in clusters}
        changes = [
            StepFinalChange(
                commit=round_.commit_hash or "",
                cluster_id=round_.probe_cluster_id,
                title=(
                    cluster_by_id[round_.probe_cluster_id].probe_cluster_description
                    if round_.probe_cluster_id in cluster_by_id
                    else ""
                ),
                type=(
                    cluster_by_id[round_.probe_cluster_id].probe_cluster_type
                    if round_.probe_cluster_id in cluster_by_id
                    else "error"
                ),
                report_count=self._report_count(
                    probes_of_cluster.get(round_.probe_cluster_id, []),
                    grouped,
                ),
            )
            for round_ in rounds
            if round_.accepted and round_.commit_hash is not None
        ]
        payload = StepFinalPayload(
            verdict=final.verdict if final is not None else None,
            best_version=final.best_version if final is not None else None,
            best_score=final.best_score if final is not None else None,
            init_score=final.init_score if final is not None else None,
            metrics=final.metrics if final is not None else [],
            changes=changes,
        )
        return EvolutionStepFinal(
            **self._shell(run, len(clusters), "final"),
            payload=payload,
        )


def _snapshot_rows(
    run: EvolutionRunRecord, reports: list[dict[str, Any]]
) -> dict[str, list[ProbeHitRow]]:
    """Adapt frozen provenance to existing DTO builders without upstream reads or writes."""
    grouped: dict[str, list[ProbeHitRow]] = OrderedDict()
    for report in reports:
        # Empty case identity keeps a case-less feedback probe visible, never counted as a case.
        cases = report.get("from_bad_cases", []) or [{"bad_case_id": ""}]
        grouped[report["probe_id"]] = [
            ProbeHitRow(
                bad_case_id=case["bad_case_id"],
                analysis_status="analyzed_issue",
                probe_id=report["probe_id"],
                probe_session_id=report.get("session_id") or "",
                probe_type="skill",
                probe_object=report.get("probe_object") or run.target_name,
                object_version=report.get("object_version") or run.target_version,
                attribute_type=report["attribute_type"],
                probe_description=report["probe_description"],
                probe_analysis=report.get("probe_analysis", ""),
                session_title=report.get("session_title"),
                case_agent_model=report.get("model"),
                session_model=None,
                probe_started_at=None,
            )
            for case in cases
        ]
    return grouped


def _project_probe(rows: list[ProbeHitRow]) -> ProbePreviewItem | None:
    """Preserve the existing ProbeRow contract for feedback without source bad cases."""
    item = probe_row_of(rows)
    if item is not None:
        item.source_bad_case_ids = [case for case in item.source_bad_case_ids if case]
    return item


def _project_patch(patch: Any) -> StepRoundPatch | None:
    """Render actual Skill diff lines through the unchanged legacy preview contract."""
    if not isinstance(patch, dict):
        return None
    if "changes" in patch:
        edit = patch.get("edit_patch", {})
        return StepRoundPatch(file=patch["file"], lines=edit.get("unified_diff", "").splitlines())
    return StepRoundPatch.model_validate(patch)
