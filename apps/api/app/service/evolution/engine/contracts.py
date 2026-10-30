"""Dependencies and immutable values used by evolution engines."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast

from app.db.models.evolution import (
    ClusterWrite,
    EvolutionFinalRecord,
    EvolutionRunRecord,
    ProbeClusterRecord,
)
from app.db.models.jobs import JobRunRecord
from app.service.evolution.events import EvolutionRunEventService


class ConfigError(RuntimeError):
    """Raised when an engine selection or stage capability is invalid."""


EvolutionBlockedStage = Literal["patch", "push", "eval"]


class RunBlocked(RuntimeError):
    """A retryable evolution failure that must leave the run blocked."""

    def __init__(
        self,
        message: str,
        *,
        stage: EvolutionBlockedStage,
        error_stage: str | None = None,
        next_retry_at: datetime | None = None,
        retry_attempts: int = 0,
        retryable: bool = True,
    ) -> None:
        """Carry the safe reason and retry facts across the job boundary."""
        super().__init__(message)
        self.stage = stage
        self.error_stage = error_stage or stage
        self.next_retry_at = next_retry_at
        self.retry_attempts = retry_attempts
        self.retryable = retryable


class ProbeReader(Protocol):
    """Read probes needed by a future prepare stage."""

    async def list_matching(
        self,
        query: Mapping[str, Any],
    ) -> Sequence[object]:
        """Return probes matching the frozen run query."""


class EvolutionRepositoryProtocol(Protocol):
    """Persistence operations used by engine orchestration."""

    async def advance_unit(
        self,
        run_id: str,
        *,
        expected: int,
        target: int,
        cluster_count: int,
    ) -> EvolutionRunRecord:
        """Advance one run cursor."""

    async def replace_clusters(
        self,
        run_id: str,
        clusters: Sequence[ClusterWrite],
    ) -> tuple[ProbeClusterRecord, ...]:
        """Replace one run's cluster snapshot."""

    async def insert_rounds(self, rounds: Sequence[Mapping[str, Any]]) -> None:
        """Insert pending iteration rounds."""

    async def write_final(self, values: Mapping[str, Any]) -> EvolutionFinalRecord:
        """Write the final evaluation row."""


type FrozenScalar = str | int | float | bool | None
type FrozenValue = FrozenScalar | tuple[FrozenValue, ...] | Mapping[str, FrozenValue]


def freeze_json(value: object) -> FrozenValue:
    """Deep-copy JSON-like values into immutable containers."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_json(item) for key, item in value.items()},
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"unsupported eval config value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class EvalGatewayConfig(Mapping[str, FrozenValue]):
    """Immutable creation-time configuration passed to an evaluation gateway."""

    _values: Mapping[str, FrozenValue]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> EvalGatewayConfig:
        """Freeze a run's JSON configuration snapshot."""
        frozen = MappingProxyType(
            {str(key): freeze_json(value) for key, value in values.items()},
        )
        return cls(frozen)

    def __getitem__(self, key: str) -> FrozenValue:
        """Return one frozen configuration value."""
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate configuration keys in insertion order."""
        return iter(self._values)

    def __len__(self) -> int:
        """Return the number of configuration keys."""
        return len(self._values)


class EvalGateway(Protocol):
    """External evaluation seam consumed by later iteration stages."""

    @property
    def config(self) -> EvalGatewayConfig:
        """Expose the frozen gateway configuration."""

    async def submit(self, request: EvalRequest) -> EvalSubmission:
        """Submit one immutable candidate for external evaluation."""

    async def get_status(self, eval_id: str) -> EvalStatus:
        """Return the external evaluation's current lifecycle state."""

    async def get_result(self, eval_id: str) -> EvalResult:
        """Return normalized scores and an external verdict."""


EvalStatusValue = Literal["queued", "running", "completed", "failed", "timeout"]
EvalVerdict = Literal["accept", "reject"]


@dataclass(frozen=True, slots=True)
class EvalRequest:
    """Stable internal request passed to every evaluation implementation."""

    run_id: str
    evolution_round_id: str
    repo_url: str
    ref: str
    commit: str
    target_type: str
    target_name: str
    target_version: str
    cluster_id: str
    idempotency_key: str
    baseline_ref: str | None = None
    baseline_commit: str | None = None


@dataclass(frozen=True, slots=True)
class EvalSubmission:
    """External evaluation identifier returned after submission."""

    eval_id: str


@dataclass(frozen=True, slots=True)
class EvalStatus:
    """One external evaluation status and optional safe message."""

    status: EvalStatusValue
    message: str | None = None


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Normalized evaluation result consumed by the placeholder decision rule."""

    current_score: float
    candidate_score: float
    external_verdict: EvalVerdict
    metrics: tuple[Mapping[str, FrozenValue], ...] = ()

    def __post_init__(self) -> None:
        """Reject malformed scores before they can become persisted JSON."""
        _strict_score(self.current_score)
        _strict_score(self.candidate_score)

    def to_payload(self) -> dict[str, Any]:
        """Serialize the result into the round's stable JSON representation."""
        return {
            "currentScore": self.current_score,
            "candidateScore": self.candidate_score,
            "externalVerdict": self.external_verdict,
            "metrics": [_thaw_json(metric) for metric in self.metrics],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EvalResult:
        """Read a persisted result and reject malformed external data."""
        current = payload.get("currentScore")
        candidate = payload.get("candidateScore")
        verdict = payload.get("externalVerdict")
        metrics = payload.get("metrics", ())
        current_score = _strict_score(current, persisted=True)
        candidate_score = _strict_score(candidate, persisted=True)
        if verdict not in {"accept", "reject"}:
            raise ValueError("persisted external verdict is invalid")
        if not isinstance(metrics, (list, tuple)):
            raise ValueError("persisted evaluation metrics are invalid")
        if any(not isinstance(metric, Mapping) for metric in metrics):
            raise ValueError("persisted evaluation metrics must contain objects")
        frozen_metrics: list[Mapping[str, FrozenValue]] = []
        for metric in metrics:
            frozen_metrics.append(
                cast(Mapping[str, FrozenValue], freeze_json(metric)),
            )
        return cls(
            current_score=current_score,
            candidate_score=candidate_score,
            external_verdict=verdict,
            metrics=tuple(frozen_metrics),
        )


@dataclass(frozen=True, slots=True)
class FakeEvalGateway:
    """Deterministic V1 gateway that needs no external service or memory state."""

    config: EvalGatewayConfig

    async def submit(self, request: EvalRequest) -> EvalSubmission:
        """Return a stable identifier derived from the durable round identity."""
        return EvalSubmission(
            eval_id=f"fake:{request.run_id}:{request.evolution_round_id}:{request.commit}",
        )

    async def get_status(self, eval_id: str) -> EvalStatus:
        """Return the configured placeholder lifecycle state."""
        self._require_eval_id(eval_id)
        configured = self.config.get("status", "completed")
        if configured not in {"queued", "running", "completed", "failed", "timeout"}:
            raise ValueError("fake evaluation status is invalid")
        message = self.config.get("message")
        return EvalStatus(
            status=cast(EvalStatusValue, configured),
            message=str(message) if message is not None else None,
        )

    async def get_result(self, eval_id: str) -> EvalResult:
        """Return configured normalized scores without retaining process state."""
        self._require_eval_id(eval_id)
        current = self.config.get("currentScore", 80.0)
        candidate = self.config.get("candidateScore", 85.0)
        verdict = self.config.get("verdict", "accept")
        metrics = self.config.get("metrics", ())
        current_score = _strict_score(current)
        candidate_score = _strict_score(candidate)
        if verdict not in {"accept", "reject"}:
            raise ValueError("fake evaluation verdict must be accept or reject")
        if not isinstance(metrics, tuple) or any(
            not isinstance(metric, Mapping) for metric in metrics
        ):
            raise ValueError("fake evaluation metrics must be objects")
        return EvalResult(
            current_score=current_score,
            candidate_score=candidate_score,
            external_verdict=cast(EvalVerdict, verdict),
            metrics=tuple(cast(Mapping[str, FrozenValue], metric) for metric in metrics),
        )

    @staticmethod
    def _require_eval_id(eval_id: str) -> None:
        """Reject blank/foreign identifiers before producing a misleading result."""
        if not eval_id.startswith("fake:") or len(eval_id.split(":")) < 4:
            raise ValueError("fake evaluation id is invalid")


@dataclass(frozen=True, slots=True)
class DefaultEvalGateway(FakeEvalGateway):
    """Backward-compatible name for the deterministic V1 gateway."""


type GatewayFactory = Callable[[EvalGatewayConfig], EvalGateway]
type StageHandler = Callable[[JobRunRecord], Awaitable[None]]
type ClusterPrepare = Callable[
    [EvolutionRunRecord, Mapping[str, Any]],
    Awaitable[None],
]


def default_gateway_factory(config: EvalGatewayConfig) -> EvalGateway:
    """Build the deterministic gateway used by the V1 evolution loop."""
    if config.get("mode", "fake") != "fake":
        raise ConfigError("Real evaluation requires a configured durable gateway factory")
    return FakeEvalGateway(config=config)


def _thaw_json(value: FrozenValue) -> Any:
    """Convert immutable frozen config values back to JSON-compatible data."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _strict_score(value: object, *, persisted: bool = False) -> float:
    """Validate one score from construction, a gateway result, or persisted JSON."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        source = "persisted evaluation scores" if persisted else "evaluation scores"
        raise ValueError(f"{source} must be numeric")
    if not math.isfinite(value):
        source = "persisted evaluation scores" if persisted else "evaluation scores"
        raise ValueError(f"{source} must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class EngineDeps:
    """External collaborators injected into one engine instance."""

    reads: ProbeReader
    writes: EvolutionRepositoryProtocol
    prepare_cluster_only: ClusterPrepare | None = None
    full_stages: Mapping[str, StageHandler] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    gateway_factory: GatewayFactory = default_gateway_factory
    events: EvolutionRunEventService | None = None

    def __post_init__(self) -> None:
        """Freeze the stage-handler mapping so later caller mutation cannot drift."""
        object.__setattr__(
            self,
            "full_stages",
            MappingProxyType(dict(self.full_stages)),
        )
