"""Read-only evolution run list projection owned by the backend."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal, Protocol, cast

from pydantic import Field

from app.db.models.evolution import EvolutionRunSummaryRecord
from app.service.evolution.state import step_states, total_units, validate_cursor
from app.service.identity.schemas import IdentityModel

EvolutionRunStatus = Literal[
    "preparing",
    "running",
    "blocked",
    "completed",
    "failed",
    "interrupted",
]
EvolutionRunPushStatus = Literal[
    "pending",
    "pushing",
    "succeeded",
    "failed",
    "skipped",
]
EvolutionRunEvalStatus = Literal[
    "pending",
    "evaluating",
    "completed",
    "failed",
    "timeout",
]
EvolutionRunBlockedStage = Literal[
    "patch",
    "push",
    "eval",
]
_RUNNING_STATUSES = frozenset({"preparing", "running", "blocked"})
_RUN_STAGE_LABELS = (
    "第 1/6 步 · 筛选输入中…",
    "第 2/6 步 · 问题聚类中…",
    "第 3/6 步 · LLM 归因置信度中…",
    "第 4/6 步 · 严重程度排序中…",
)


class EvolutionRunTarget(IdentityModel):
    """Immutable evolution target snapshot shown on one run card."""

    type: Literal["skill", "agent"]
    name: str
    version: str


class EvolutionRunWindow(IdentityModel):
    """Session-time window selected when the run was created."""

    start: date | None = Field(default=None, alias="from")
    to: date | None = None


class EvolutionRunError(IdentityModel):
    """Sanitized terminal or blocked error summary."""

    stage: str
    message: str
    at: datetime


class EvolutionRunAttributeStats(IdentityModel):
    """Probe counts by externally supplied attribution type."""

    model: int = 0
    prompt: int = 0
    error: int = 0
    non_issue: int = 0


class EvolutionRunStats(IdentityModel):
    """Backend-normalized run statistics."""

    sessions: int = 0
    probes: int = 0
    used_probes: int = 0
    clusters: int = 0
    by_attribute_type: EvolutionRunAttributeStats


class EvolutionRunProgress(IdentityModel):
    """Backend-owned cursor, percentages, rounds, and stage copy."""

    unit: int
    total: int
    percent: int
    stage_label: str
    done_rounds: int
    accepted: int


class EvolutionRunFinal(IdentityModel):
    """Terminal evaluation summary shown in the history list."""

    best_version: str
    branch_name: str | None
    push_status: EvolutionRunPushStatus
    verdict: bool
    best_score: float
    init_score: float


class EvolutionRunCard(IdentityModel):
    """One complete run-list card contract."""

    run_id: str
    title: str
    mode: Literal["full", "cluster_only"]
    target: EvolutionRunTarget
    window: EvolutionRunWindow
    models: list[str]
    created_by: str | None
    created_at: datetime
    status: EvolutionRunStatus
    error: EvolutionRunError | None
    stats: EvolutionRunStats
    progress: EvolutionRunProgress
    final: EvolutionRunFinal | None


class EvolutionRunWorkspace(IdentityModel):
    """Repository and worktree state shown by the run detail."""

    repo_url: str
    sub_dir: str
    repo_path: str | None
    base_ref: str | None
    base_commit: str | None
    branch_name: str | None
    workspace_path: str | None
    remote: str
    push_status: EvolutionRunPushStatus
    pushed_at: datetime | None
    push_error: str | None


class EvolutionRunStep(IdentityModel):
    """One backend-owned stepper node with lazy-load eligibility."""

    index: int
    key: Literal[
        "input",
        "clusters",
        "attribution",
        "severity",
        "iteration",
        "final",
    ]
    title: str
    status: Literal["done", "active", "pending"]
    clickable: bool
    detail: str | None = None


class EvolutionRunDetail(EvolutionRunCard):
    """Complete shell consumed by the six-step run-detail page."""

    blocked_stage: EvolutionRunBlockedStage | None
    retry_attempts: int
    next_retry_at: datetime | None
    eval_status: EvolutionRunEvalStatus
    workspace: EvolutionRunWorkspace
    last_event_seq: int
    steps: list[EvolutionRunStep]


class EvolutionRunList(IdentityModel):
    """Running and history groups already ordered by the backend."""

    running: list[EvolutionRunCard]
    history: list[EvolutionRunCard]


class EvolutionRunStore(Protocol):
    """Persistence contract consumed by the run-list service."""

    async def list_summaries(self) -> tuple[EvolutionRunSummaryRecord, ...]:
        """Return all runs with batched list facts."""

    async def get_summary(self, run_id: str) -> EvolutionRunSummaryRecord | None:
        """Return one run with its batched list facts."""


class EvolutionRunEventSeqReader(Protocol):
    """Read the persisted event high-water mark for one run shell."""

    async def max_seq(self, run_id: str) -> int:
        """Return the latest durable event sequence."""


def _non_negative_int(value: object) -> int:
    """Return one optional non-negative integer without coercing booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _stats(summary: EvolutionRunSummaryRecord) -> EvolutionRunStats:
    """Normalize persisted stats and replace cluster count with the real count."""
    raw = summary.run.stats
    by_type_raw = raw.get("byAttributeType")
    by_type = by_type_raw if isinstance(by_type_raw, Mapping) else {}
    attributes = EvolutionRunAttributeStats(
        model=_non_negative_int(by_type.get("model")),
        prompt=_non_negative_int(by_type.get("prompt")),
        error=_non_negative_int(by_type.get("error")),
        non_issue=_non_negative_int(
            by_type.get("nonIssue", by_type.get("non_issue")),
        ),
    )
    return EvolutionRunStats(
        sessions=_non_negative_int(raw.get("sessions")),
        probes=_non_negative_int(raw.get("probes")),
        used_probes=attributes.model + attributes.prompt + attributes.error,
        clusters=summary.cluster_count,
        by_attribute_type=attributes,
    )


def _progress(summary: EvolutionRunSummaryRecord) -> EvolutionRunProgress:
    """Derive every user-visible progress value from the persisted cursor."""
    run = summary.run
    cluster_count = summary.cluster_count
    total = total_units(cluster_count)
    validate_cursor(run.progress_unit, cluster_count)
    unit = run.progress_unit
    if run.status == "completed" and unit == total:
        stage_label = "第 6/6 步 · 测试集终评已完成"
    elif unit < 4:
        stage_label = _RUN_STAGE_LABELS[unit]
    elif unit < 4 + cluster_count:
        stage_label = f"第 5/6 步 · 迭代优化 · 第 {unit - 3}/{cluster_count} 轮"
    else:
        stage_label = "第 6/6 步 · 测试集终评中…"
    return EvolutionRunProgress(
        unit=unit,
        total=total,
        percent=math.floor(unit * 100 / total + 0.5),
        stage_label=stage_label,
        done_rounds=min(max(unit - 4, 0), cluster_count),
        accepted=summary.accepted_count,
    )


def _error(summary: EvolutionRunSummaryRecord) -> EvolutionRunError | None:
    """Return a complete error summary only when all persisted fields exist."""
    run = summary.run
    if run.error_stage is None or run.error_message is None or run.error_at is None:
        return None
    return EvolutionRunError(
        stage=run.error_stage,
        message=run.error_message,
        at=run.error_at,
    )


def _final(summary: EvolutionRunSummaryRecord) -> EvolutionRunFinal | None:
    """Map an optional terminal evaluation row with the run's publish state."""
    final = summary.final
    if final is None:
        return None
    return EvolutionRunFinal(
        best_version=final.best_version,
        branch_name=summary.run.branch_name,
        push_status=cast(EvolutionRunPushStatus, summary.run.push_status),
        verdict=final.verdict,
        best_score=final.best_score,
        init_score=final.init_score,
    )


def _card(summary: EvolutionRunSummaryRecord) -> EvolutionRunCard:
    """Map one repository summary to the public list card."""
    run = summary.run
    return EvolutionRunCard(
        run_id=run.id,
        title=run.title,
        mode=cast(Literal["full", "cluster_only"], run.mode),
        target=EvolutionRunTarget(
            type=cast(Literal["skill", "agent"], run.target_type),
            name=run.target_name,
            version=run.target_version,
        ),
        window=EvolutionRunWindow.model_validate(
            {"from": run.window_from, "to": run.window_to},
        ),
        models=run.models,
        created_by=summary.created_by_name,
        created_at=run.created_at,
        status=cast(
            Literal[
                "preparing",
                "running",
                "blocked",
                "completed",
                "failed",
                "interrupted",
            ],
            run.status,
        ),
        error=_error(summary),
        stats=_stats(summary),
        progress=_progress(summary),
        final=_final(summary),
    )


def _detail_steps(
    summary: EvolutionRunSummaryRecord,
) -> list[EvolutionRunStep]:
    """Project the shared cursor state machine into stepper nodes."""
    run = summary.run
    states = step_states(
        run.progress_unit,
        summary.cluster_count,
        run.status,
    )
    steps: list[EvolutionRunStep] = []
    for state in states:
        detail = None
        if state.key == "iteration" and state.status == "active" and summary.cluster_count > 0:
            round_number = min(
                max(run.progress_unit - 3, 1),
                summary.cluster_count,
            )
            detail = f"第 {round_number}/{summary.cluster_count} 轮"
        steps.append(
            EvolutionRunStep(
                index=state.index,
                key=cast(
                    Literal[
                        "input",
                        "clusters",
                        "attribution",
                        "severity",
                        "iteration",
                        "final",
                    ],
                    state.key,
                ),
                title=state.title,
                status=state.status,
                clickable=state.clickable,
                detail=detail,
            ),
        )
    return steps


class EvolutionRunService:
    """Project repository summaries into backend-grouped run cards."""

    def __init__(
        self,
        store: EvolutionRunStore,
        events: EvolutionRunEventSeqReader | None = None,
    ) -> None:
        """Bind the service to its run summary store."""
        self._store = store
        self._events = events

    async def list(self) -> EvolutionRunList:
        """Return repository-ordered running and history groups."""
        cards = [_card(summary) for summary in await self._store.list_summaries()]
        return EvolutionRunList(
            running=[card for card in cards if card.status in _RUNNING_STATUSES],
            history=[card for card in cards if card.status not in _RUNNING_STATUSES],
        )

    async def detail(self, run_id: str) -> EvolutionRunDetail | None:
        """Return one run shell with workspace and backend-owned step states."""
        summary = await self._store.get_summary(run_id)
        if summary is None:
            return None
        card = _card(summary)
        run = summary.run
        last_event_seq = await self._events.max_seq(run_id) if self._events is not None else 0
        return EvolutionRunDetail(
            **card.model_dump(),
            blocked_stage=cast(
                EvolutionRunBlockedStage | None,
                run.blocked_stage,
            ),
            retry_attempts=run.retry_attempts,
            next_retry_at=run.next_retry_at,
            eval_status=cast(EvolutionRunEvalStatus, run.eval_status),
            workspace=EvolutionRunWorkspace(
                repo_url=run.repo_url,
                sub_dir=run.sub_dir,
                repo_path=run.repo_path,
                base_ref=run.base_ref,
                base_commit=run.base_commit,
                branch_name=run.branch_name,
                workspace_path=run.workspace_path,
                remote=run.remote_name,
                push_status=cast(EvolutionRunPushStatus, run.push_status),
                pushed_at=run.pushed_at,
                push_error=run.push_error,
            ),
            last_event_seq=last_event_seq,
            steps=_detail_steps(summary),
        )
