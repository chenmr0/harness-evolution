"""Validated per-case evaluation evidence; no provider verdict substitutes for scores."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Literal

from app.utils.redaction import redact_text


class EvaluationError(RuntimeError):
    """Bounded provider failure safe to persist without URLs, tokens or payloads."""


class AmbiguousEvaluationSubmission(EvaluationError):
    """A durable POST intent has no confirmed provider ID and needs reconciliation."""


class InvalidEvaluationEvidence(EvaluationError):
    """Terminal malformed or incomplete evidence, distinct from transport failures."""


@dataclass(frozen=True, slots=True)
class CaseScore:
    """One completed evaluation, preserving the exact case set behind its mean."""

    case_scores: tuple[tuple[str, float], ...]
    report: dict[str, Any] = field(default_factory=dict)

    metric_key: Literal["total_score", "hard"] = "total_score"

    def __post_init__(self) -> None:
        """Reject missing, duplicate, blank and nonfinite case evidence."""
        if not self.case_scores:
            raise InvalidEvaluationEvidence("Completed evaluation has no evaluated cases")
        names: set[str] = set()
        for name, score in self.case_scores:
            if not isinstance(name, str) or not name.strip() or name in names:
                raise InvalidEvaluationEvidence(
                    "Evaluation case identifiers are invalid or duplicated"
                )
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise InvalidEvaluationEvidence("Evaluation case score must be finite and numeric")
            names.add(name)
        object.__setattr__(self, "case_scores", tuple(sorted(self.case_scores)))
        if not math.isfinite(self.score):
            raise InvalidEvaluationEvidence("Evaluation mean is not finite")

    @property
    def score(self) -> float:
        """Use the original metric for archived results and hundredths for hard scores."""
        if self.metric_key == "hard":
            return _hard_mean(
                [metric["value"] for metric in self.report["metrics"] if metric["key"] == "hard"]
            )
        # Divide before summing to avoid overflow for valid large finite scores.
        return math.fsum(value / len(self.case_scores) for _, value in self.case_scores)

    @property
    def case_ids(self) -> tuple[str, ...]:
        """Return stable IDs used to reject incomparable evaluations."""
        return tuple(name for name, _ in self.case_scores)

    def to_payload(self) -> dict[str, Any]:
        """Serialize sufficient evidence to recompute scores after process restart."""
        return {
            "score": self.score,
            "scoreMetric": self.metric_key,
            "caseScores": dict(self.case_scores),
            "report": deepcopy(self.report),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CaseScore:
        """Validate persisted evidence rather than trusting a stored aggregate."""
        cases = payload.get("caseScores")
        if not isinstance(cases, dict):
            raise InvalidEvaluationEvidence("Evaluation case evidence is missing")
        report = payload.get("report", {})
        if not isinstance(report, dict):
            raise InvalidEvaluationEvidence("Evaluation report evidence is invalid")
        key = payload.get("scoreMetric", "total_score")
        if key not in ("hard", "total_score"):
            raise InvalidEvaluationEvidence("Unknown evaluation score metric")
        if key == "hard":
            result = parse_case_score({"status": "completed", "report": report})
            if dict(result.case_scores) != cases:
                raise InvalidEvaluationEvidence("Evaluation case evidence disagrees with report")
        else:
            result = cls(tuple(cases.items()), deepcopy(report))
        score = payload.get("score")
        if type(score) not in (int, float) or score != result.score:
            raise InvalidEvaluationEvidence("Evaluation aggregate disagrees with case evidence")
        return result


def _hard_mean(values: list[float]) -> float:
    """Round the mean of hard observations, weighting each occurrence equally."""
    if not values:
        raise InvalidEvaluationEvidence("Completed evaluation has no case hard metrics")
    with localcontext() as context:
        context.prec = 340
        total = sum((Decimal(str(value)) for value in values), Decimal(0))
        return float((total / len(values)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compare_case_scores(current: CaseScore, candidate: CaseScore) -> bool:
    """Accept strictly greater means only when both evaluated exactly the same cases."""
    if current.metric_key != candidate.metric_key or current.case_ids != candidate.case_ids:
        raise InvalidEvaluationEvidence("Evaluation case sets are not comparable")
    return candidate.score > current.score


def parse_case_score(body: Mapping[str, Any]) -> CaseScore:
    """Normalize the platform's completed report.metrics case hard scores."""
    if str(body.get("status", "")).casefold() != "completed":
        raise InvalidEvaluationEvidence("Evaluation result is not completed")
    report = body.get("report")
    if not isinstance(report, dict):
        raise InvalidEvaluationEvidence("Completed evaluation omitted report")
    for key in ("complete", "cases_complete"):
        if key in report and report[key] is not True:
            raise InvalidEvaluationEvidence("Evaluation case results are incomplete")
    metrics = report.get("metrics")
    if not isinstance(metrics, list) or not metrics or len(metrics) > 10000:
        raise InvalidEvaluationEvidence("Completed evaluation omitted valid metrics")
    cases: dict[str, list[float]] = {}
    reported_cases: set[str] = set()
    for metric in metrics:
        if not isinstance(metric, dict) or not isinstance(metric.get("key"), str):
            raise InvalidEvaluationEvidence("Evaluation metric is invalid")
        tags = metric.get("tags")
        if isinstance(tags, dict) and tags.get("metric_type") == "case":
            name = tags.get("case_name")
            if not isinstance(name, str) or not name.strip():
                raise InvalidEvaluationEvidence("Evaluation case metric lacks identity")
            reported_cases.add(name)
        if metric["key"] != "hard":
            continue
        if not isinstance(tags, dict):
            raise InvalidEvaluationEvidence("Evaluation hard metric tags are invalid")
        score = metric.get("value")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise InvalidEvaluationEvidence("Evaluation case score must be finite and numeric")
        if tags.get("metric_type") == "global":
            continue
        if tags.get("metric_type") != "case" or not isinstance(tags.get("case_name"), str):
            raise InvalidEvaluationEvidence("Evaluation hard metric lacks case identity")
        cases.setdefault(tags["case_name"], []).append(float(score))
    if not cases:
        raise InvalidEvaluationEvidence("Completed evaluation has no case hard metrics")
    scores = tuple((name, _hard_mean(values)) for name, values in cases.items())
    result = CaseScore(scores, _validated_report(report), "hard")
    if "case_count" in report and (
        type(report["case_count"]) is not int or report["case_count"] != len(reported_cases)
    ):
        raise InvalidEvaluationEvidence("Evaluation case count is incomplete or inconsistent")
    return result


def _validated_report(report: dict[str, Any]) -> dict[str, Any]:
    preserved = {"metrics": deepcopy(report["metrics"])}
    for metric in preserved["metrics"]:
        value = metric.get("value")
        tags = metric.get("tags")
        if (
            not isinstance(value, (str, int, float))
            or (isinstance(value, float) and not math.isfinite(value))
            or not isinstance(tags, dict)
            or not all(
                isinstance(v, str)
                or (
                    k == "parent_case_name_array_json"
                    and isinstance(v, list)
                    and all(isinstance(parent, str) for parent in v)
                )
                for k, v in tags.items()
            )
        ):
            raise InvalidEvaluationEvidence("Evaluation report metric is malformed")
    if "bad_cases" in report:
        cases = report["bad_cases"]
        if not isinstance(cases, list) or len(cases) > 100:
            raise InvalidEvaluationEvidence("Evaluation bad cases are invalid")
        names: set[str] = set()
        for case in cases:
            if not isinstance(case, dict) or set(case) - {
                "case_id",
                "input",
                "expected",
                "actual",
                "reason",
            }:
                raise InvalidEvaluationEvidence("Evaluation bad case schema is invalid")
            name = case.get("case_id")
            if not isinstance(name, str) or not name.strip() or len(name) > 128 or name in names:
                raise InvalidEvaluationEvidence("Evaluation bad case IDs are invalid")
            names.add(name)
            for key, value in case.items():
                if not isinstance(value, str) or len(value) > (8000 if key == "reason" else 16000):
                    raise InvalidEvaluationEvidence("Evaluation bad case content is invalid")
        preserved["bad_cases"] = deepcopy(cases)
    if "bad_cases_complete" in report:
        if type(report["bad_cases_complete"]) is not bool or "bad_cases" not in report:
            raise InvalidEvaluationEvidence("Evaluation bad case coverage is invalid")
        preserved["bad_cases_complete"] = report["bad_cases_complete"]
    encoded = json.dumps(preserved, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 2 * 1024 * 1024 or redact_text(encoded) != encoded:
        raise InvalidEvaluationEvidence("Evaluation evidence is oversized or contains credentials")
    return preserved
