"""Strict model-boundary contracts for Skill evolution (not HTTP schemas)."""

from __future__ import annotations

import math
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.service.evolution.skill_safety import validate_path

Attribute = Literal["model", "prompt", "error"]


class StrictModel(BaseModel):
    """Reject unrecognized model output fields."""

    model_config = ConfigDict(extra="forbid")


class SkillStrategy(StrictModel):
    """Source-compatible confidence gate and weighted priority policy."""

    max_rounds: int = Field(default=1, ge=1, le=20, strict=True)
    confidence_threshold: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    severity_weight: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    confidence_weight: float = Field(default=0.2, ge=0, le=1, allow_inf_nan=False)
    frequency_weight: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)
    type_weights: dict[Attribute, float] = Field(
        default_factory=lambda: cast(
            dict[Attribute, float], {"model": 1.0, "prompt": 1.0, "error": 1.0}
        ),
    )
    max_candidates: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_weights(self) -> Self:
        """Require a normalized policy and complete, finite type weights."""
        if not math.isclose(
            self.severity_weight + self.confidence_weight + self.frequency_weight, 1
        ):
            raise ValueError("critic weights must sum to one")
        if set(self.type_weights) != {"model", "prompt", "error"} or any(
            not math.isfinite(v) or not 0 <= v <= 10 for v in self.type_weights.values()
        ):
            raise ValueError("type weights must cover model/prompt/error and lie in [0,10]")
        return self


class SkillCluster(StrictModel):
    """One exhaustive, type-homogeneous model cluster."""

    cluster_id: str = Field(min_length=1, max_length=128)
    attribute_type: Attribute
    probe_ids: list[str] = Field(min_length=1, max_length=1000)
    probe_cluster_description: str = Field(min_length=1, max_length=8000)
    summary: str | None = Field(default=None, min_length=1, max_length=8000)
    reason: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> Any:
        """Accept the source model's summary alias without losing either field."""
        if isinstance(value, dict):
            value = dict(value)
            if "summary" in value and "probe_cluster_description" not in value:
                value["probe_cluster_description"] = value["summary"]
            if "probe_cluster_description" in value and "summary" not in value:
                value["summary"] = value["probe_cluster_description"]
        return value


class PlannedChange(StrictModel):
    """A concrete safe-path repair instruction."""

    path: str = Field(min_length=1, max_length=240)
    action: Literal["add", "modify", "delete"]
    description: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        """Reject paths outside the Skill."""
        validate_path(self.path)
        return self


class RepairPlan(StrictModel):
    """Verified changes and checks supplied to patch generation."""

    objective: str = Field(min_length=1, max_length=4000)
    changes: list[PlannedChange] = Field(max_length=128)
    validation_checks: list[str] = Field(min_length=1, max_length=30)
    risks: list[str] = Field(default_factory=list, max_length=30)


class SkillCritic(StrictModel):
    """Evidence-grounded verification with finite normalized scores."""

    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    severity: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    reason: str = Field(max_length=8000)
    evidence: list[str] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    repair_plan: RepairPlan | None = None


class FeedbackIssue(StrictModel):
    """Next-round issue citing actual evaluation evidence."""

    attribute_type: Attribute
    probe_description: str = Field(min_length=1, max_length=8000)
    probe_analysis: str = Field(min_length=1, max_length=16000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class FeedbackAttribution(StrictModel):
    """Validated attribution rather than a score-only success inference."""

    status: Literal["actionable", "no_remaining_issues", "insufficient_evidence"]
    reason: str = Field(min_length=1, max_length=8000)
    issues: list[FeedbackIssue] = Field(max_length=50)

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        """Actionable attribution must contain issues, other outcomes cannot."""
        if bool(self.issues) != (self.status == "actionable"):
            raise ValueError("Feedback status and issues disagree")
        return self


def strategy_from_environment() -> SkillStrategy:
    """Load bounded server policy without exposing rejected environment contents."""
    import json
    import os

    raw = os.environ.get("HE_SKILL_STRATEGY_JSON")
    if raw is None:
        return SkillStrategy()
    try:
        if len(raw.encode("utf-8")) > 8192:
            raise ValueError("oversized")

        def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=unique_object)
        return SkillStrategy.model_validate(value)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid HE_SKILL_STRATEGY_JSON configuration") from None
