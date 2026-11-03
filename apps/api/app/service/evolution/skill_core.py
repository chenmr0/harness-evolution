"""Skill-only evolution rules ported from the proven source engine.

Persistence, Git publication and evaluation scheduling remain harness-owned.
Model outputs are untrusted and are checked before becoming durable decisions.
"""

from __future__ import annotations

import difflib
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from app.service.evolution.skill_contracts import (
    FeedbackAttribution,
    SkillCluster,
    SkillCritic,
    SkillStrategy,
)
from app.service.evolution.skill_safety import (
    reject_secrets,
    reject_sensitive_payload,
    validate_patch,
)

Json = dict[str, Any]


class SkillPatchRejected(ValueError):
    """A generated patch is unsafe or empty; model-provider failures are not this error."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SkillModel(Protocol):
    """Source-compatible asynchronous model port."""

    async def cluster(self, reports: list[Json]) -> list[Json]:
        """Group one attribute type's canonical reports."""

    async def verify(self, cluster: Json, reports: list[Json], files: dict[str, str]) -> Json:
        """Verify the issue against the actual current Skill."""

    async def patch(
        self,
        cluster: Json,
        reports: list[Json],
        files: dict[str, str],
    ) -> dict[str, str | None]:
        """Generate replacements/deletions, not executable instructions."""

    async def attribute(self, feedback: Json, files: dict[str, str]) -> Json:
        """Attribute only validation evidence to next-round issues."""


@dataclass(frozen=True)
class SkillPlan:
    """A complete plan and preserved deduplication provenance."""

    clusters: tuple[Json, ...]
    canonical_reports: tuple[Json, ...]
    aliases: dict[str, str]


@dataclass(frozen=True)
class SkillPatch:
    """Validated candidate files plus a reviewable EditPatch."""

    files: dict[str, str]
    changes: dict[str, str | None]
    edit_patch: Json


def deduplicate(reports: list[Json]) -> tuple[list[Json], dict[str, str]]:
    """Deduplicate normalized descriptions within attribute, retaining aliases."""
    canonical: list[Json] = []
    aliases: dict[str, str] = {}
    seen: dict[tuple[str, str], str] = {}
    ids: set[str] = set()
    for report in reports:
        pid = report["probe_id"]
        if not isinstance(pid, str) or not pid or pid in ids:
            raise ValueError("duplicate or invalid probe id")
        ids.add(pid)
        if report.get("probe_type", "skill") != "skill":
            raise ValueError("only Skill probes are supported")
        if report["attribute_type"] not in ("model", "prompt", "error"):
            raise ValueError("invalid probe attribute")
        key = (report["attribute_type"], " ".join(report["probe_description"].split()).casefold())
        if key in seen:
            aliases[pid] = seen[key]
        else:
            seen[key] = pid
            canonical.append(report)
    return canonical, aliases


class ClusterValidationError(ValueError):
    """Bounded model feedback with a payload-free summary safe for run errors."""

    def __init__(self, counts: dict[str, int], issues: list[Json]) -> None:
        self.feedback: Json = {
            "counts": counts,
            "issues": issues,
            "issues_truncated": sum(counts.values()) > len(issues),
        }
        super().__init__(
            "invalid cluster output: "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
        )


def validate_clusters(raw: Any, reports: list[Json]) -> list[Json]:
    """Require exactly-once membership, unique clusters and homogeneous types."""
    # Safety failures are never converted into retryable validation feedback.
    reject_sensitive_payload(raw)
    if not isinstance(raw, list):
        raise ClusterValidationError({"invalid_shape": 1}, [{"code": "invalid_shape"}])
    clusters = []
    for index, item in enumerate(raw):
        try:
            clusters.append(SkillCluster.model_validate(item).model_dump(mode="json"))
        except ValidationError:
            # Do not put Pydantic's raw input values in durable errors or prompts.
            raise ClusterValidationError(
                {"invalid_schema": 1},
                [{"code": "invalid_schema", "cluster_index": index}],
            ) from None
    expected = {r["probe_id"]: r["attribute_type"] for r in reports}
    seen: dict[str, int] = {}
    cluster_ids: set[str] = set()
    counts: dict[str, int] = {}
    issues: list[Json] = []

    def record(code: str, **details: Any) -> None:
        counts[code] = counts.get(code, 0) + 1
        if len(issues) < 50:
            issues.append({"code": code, **details})

    for index, cluster in enumerate(clusters):
        if cluster["cluster_id"] in cluster_ids:
            record("duplicate_cluster_id", cluster_index=index, cluster_id=cluster["cluster_id"])
        cluster_ids.add(cluster["cluster_id"])
        for probe_id in cluster["probe_ids"]:
            if probe_id in seen:
                record(
                    "duplicate_membership",
                    probe_id=probe_id,
                    cluster_index=index,
                    first_cluster_index=seen[probe_id],
                )
            else:
                seen[probe_id] = index
            if probe_id not in expected:
                record("unknown_probe_id", probe_id=probe_id, cluster_index=index)
            elif expected[probe_id] != cluster["attribute_type"]:
                record(
                    "attribute_mismatch",
                    probe_id=probe_id,
                    cluster_index=index,
                    expected_attribute=expected[probe_id],
                )
    for probe_id in sorted(expected.keys() - seen.keys()):
        record("missing_probe_id", probe_id=probe_id)
    if counts:
        raise ClusterValidationError(counts, issues)
    return clusters


def edit_patch(before: dict[str, str], after: dict[str, str]) -> Json:
    """Render deterministic add/modify/delete files and newline-correct unified diff."""
    files, diffs = [], []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        operation = "add" if path not in before else "delete" if path not in after else "modify"
        patch_lines = difflib.unified_diff(
            before.get(path, "").splitlines(keepends=True),
            after.get(path, "").splitlines(keepends=True),
            fromfile="a/" + path if path in before else "/dev/null",
            tofile="b/" + path if path in after else "/dev/null",
        )
        diff = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in patch_lines
        )
        files.append(
            dict(
                path=path,
                operation=operation,
                before=before.get(path),
                after=after.get(path),
                diff=diff,
            )
        )
        diffs.append(diff)
    return {"files": files, "unified_diff": "".join(diffs)}


class SkillEvolutionCore:
    """Plan, verify, rank, safely patch and attribute using injected model IO."""

    def __init__(self, model: SkillModel, strategy: SkillStrategy | None = None) -> None:
        """Bind the model and explicit policy; never silently use a mock."""
        self.model = model
        self.strategy = strategy or SkillStrategy()

    async def plan(
        self,
        reports: list[Json],
        files: dict[str, str],
        round_number: int = 1,
    ) -> SkillPlan:
        """Produce verified source-compatible ranking without publishing changes."""
        partition = await self.partition(reports, files, round_number)
        checked = [
            await self.verify_cluster(cluster, partition, files) for cluster in partition.clusters
        ]
        return self.rank(partition, checked)

    async def partition(
        self, reports: list[Json], files: dict[str, str], round_number: int = 1
    ) -> SkillPlan:
        """Validate the entire partition before exposing any group for review."""
        if not reports or len(reports) > 1000 or round_number < 1:
            raise ValueError("invalid Skill planning input")
        reject_sensitive_payload(reports)
        for content in files.values():
            reject_secrets(content)
        canonical, aliases = deduplicate(reports)
        # Provenance must not bias clustering; retain it separately in the checkpoint.
        canonical = [
            dict(
                probe_id=r["probe_id"],
                probe_type="skill",
                attribute_type=r["attribute_type"],
                probe_description=r["probe_description"],
                probe_analysis=r.get("probe_analysis", ""),
                **(
                    {"feedback_evidence": r["evidence_trace_range"]["evidence"]}
                    if isinstance(r.get("evidence_trace_range"), dict)
                    and r["evidence_trace_range"].get("source") == "evaluation_feedback"
                    else {}
                ),
            )
            for r in canonical
        ]
        groups: dict[str, list[Json]] = defaultdict(list)
        for report in canonical:
            groups[report["attribute_type"]].append(report)
        raw = []
        for attribute, group in sorted(groups.items()):
            part = validate_clusters(await self.model.cluster(group), group)
            for item in part:
                prefix = f"r{round_number}:" if round_number > 1 else ""
                item["cluster_id"] = f"{prefix}{attribute}:{item['cluster_id']}"
            raw.extend(part)
        clusters = validate_clusters(raw, canonical)
        for cluster in clusters:
            cluster["source_probe_ids"] = cluster["probe_ids"] + sorted(
                alias for alias, source in aliases.items() if source in cluster["probe_ids"]
            )
        return SkillPlan(tuple(clusters), tuple(canonical), aliases)

    async def verify_cluster(
        self, cluster: Json, partition: SkillPlan, files: dict[str, str]
    ) -> Json:
        """Review one cluster against the frozen Skill files."""
        members = [r for r in partition.canonical_reports if r["probe_id"] in cluster["probe_ids"]]
        inputs = {k: v for k, v in cluster.items() if k != "source_probe_ids"}
        critic = SkillCritic.model_validate(await self.model.verify(inputs, members, files))
        critic_data = critic.model_dump(mode="json")
        reject_sensitive_payload(critic_data)
        config = self.strategy
        support = len(members)
        priority = (
            config.severity_weight * critic.severity
            + config.confidence_weight * critic.confidence
            + config.frequency_weight
            * math.log1p(support)
            / math.log1p(len(partition.canonical_reports))
        ) * config.type_weights[cluster["attribute_type"]]
        return dict(
            cluster,
            critic=critic_data,
            support=support,
            priority=priority,
            eligible=critic.confidence >= config.confidence_threshold,
        )

    def rank(self, partition: SkillPlan, ranked: list[Json]) -> SkillPlan:
        """Select candidates only after all reviews are durable."""
        config = self.strategy
        ranked.sort(key=lambda c: (-c["priority"], c["cluster_id"]))
        remaining = config.max_candidates if config.max_candidates is not None else len(ranked)
        for rank, cluster in enumerate(ranked, 1):
            cluster["rank"] = rank
            disposition = (
                "low_confidence"
                if not cluster["eligible"]
                else "candidate_limit"
                if remaining == 0
                else "selected"
            )
            cluster["disposition"] = disposition
            cluster["decision_reason"] = {
                "low_confidence": "below confidence threshold",
                "candidate_limit": "candidate limit reached",
                "selected": "selected by priority",
            }[disposition]
            if disposition == "selected":
                remaining -= 1
        return SkillPlan(tuple(ranked), partition.canonical_reports, partition.aliases)

    async def patch(self, cluster: Json, reports: list[Json], files: dict[str, str]) -> SkillPatch:
        """Generate against the current accepted tree and validate every change."""
        reject_sensitive_payload({"cluster": cluster, "reports": reports})
        for content in files.values():
            reject_secrets(content)
        changes = await self.model.patch(cluster, reports, dict(files))
        if not isinstance(changes, dict) or any(
            not isinstance(k, str) or (v is not None and not isinstance(v, str))
            for k, v in changes.items()
        ):
            raise SkillPatchRejected("invalid_patch")
        try:
            result = validate_patch(files, changes)
        except ValueError:
            raise SkillPatchRejected("invalid_patch") from None
        if result == files:
            raise SkillPatchRejected("noop_patch")
        return SkillPatch(result, changes, edit_patch(files, result))

    async def attribute(self, feedback: Json, files: dict[str, str]) -> Json:
        """Validate feedback references; final-test evidence is never model input."""
        reject_sensitive_payload(feedback)
        for content in files.values():
            reject_secrets(content)
        if any(not e["name"].startswith("validation/") for e in feedback["evaluations"]):
            raise ValueError("feedback must contain only validation evaluations")
        result = FeedbackAttribution.model_validate(
            await self.model.attribute(feedback, files),
        ).model_dump(mode="json")
        reject_sensitive_payload(result)
        evidence = {item["id"]: item for item in feedback["evidence"]}
        for issue in result["issues"]:
            ids = issue["evidence_ids"]
            if len(set(ids)) != len(ids) or any(i not in evidence for i in ids):
                raise ValueError("Feedback cites unknown or duplicate evidence")
            if not any(evidence[i]["ref"] == feedback["current_ref"] for i in ids):
                raise ValueError("Feedback must cite the current accepted version")
        if result["status"] == "no_remaining_issues":
            current = next(
                e["report"] for e in feedback["evaluations"] if e["ref"] == feedback["current_ref"]
            )
            if not (current.get("bad_cases_complete") is True and current.get("bad_cases") == []):
                return {
                    "status": "insufficient_evidence",
                    "issues": [],
                    "reason": "No complete empty failure set supports this conclusion.",
                }
        return result


def build_feedback_input(state: Json, round_state: Json) -> Json:
    """Build source-compatible validation-only evidence from a durable checkpoint."""
    names = ["validation/baseline"] + [
        f"validation/candidate/{i}"
        for i in range(round_state["attempt_start"], round_state["attempt_end"])
        if f"validation/candidate/{i}" in state["evaluations"]
    ]
    current_name = next(
        name
        for name, record in state["evaluations"].items()
        if name.startswith("validation/") and record["ref"] == state["current_ref"]
    )
    names = list(dict.fromkeys([*names, current_name]))
    evaluations, evidence = [], []
    for index, name in enumerate(names):
        record = state["evaluations"][name]
        report = record.get("report", {})
        evaluations.append(
            dict(
                name=name,
                run_id=record["run_id"],
                ref=record["ref"],
                score=record["score"],
                report=report,
            )
        )
        items = [("score", {"value": record["score"]})]
        items += [("metric", metric) for metric in report.get("metrics", [])]
        items += [("bad_case", case) for case in report.get("bad_cases", [])]
        if report.get("bad_cases_complete") is True:
            items.append(
                (
                    "case_coverage",
                    {
                        "complete": True,
                        "bad_case_count": len(report["bad_cases"]),
                    },
                )
            )
        for j, (kind, data) in enumerate(items):
            evidence.append(
                dict(
                    id=f"e{index}-{j}",
                    kind=kind,
                    evaluation=name,
                    run_id=record["run_id"],
                    ref=record["ref"],
                    data=data,
                )
            )
    return {
        "round_number": round_state["number"],
        "current_ref": state["current_ref"],
        "current_score": state["current_score"],
        "evaluations": evaluations,
        "evidence": evidence,
        "initial_reports": state["source_reports"][: state["initial_report_count"]],
        "history": [
            {
                k: a.get(k)
                for k in (
                    "round_number",
                    "cluster_id",
                    "accepted",
                    "reason",
                    "base_ref",
                    "ref",
                    "score",
                    "previous_score",
                )
            }
            for a in state["attempts"]
        ],
    }


def feedback_reports(attribution: Json, feedback: Json, run_id: str) -> list[Json]:
    """Turn validated actionable feedback into traceable next-round Skill probes."""
    number = feedback["round_number"] + 1
    evidence = {item["id"]: item for item in feedback["evidence"]}
    result = []
    for i, issue in enumerate(attribution["issues"]):
        selected = [evidence[key] for key in issue["evidence_ids"]]
        result.append(
            {
                "probe_id": f"feedback-r{number}-{i}",
                "probe_type": "skill",
                "attribute_type": issue["attribute_type"],
                "probe_description": issue["probe_description"],
                "probe_analysis": issue["probe_analysis"],
                "session_id": f"feedback-{run_id}-r{number}",
                "from_bad_cases": [
                    {
                        "bad_case_id": item["data"]["case_id"],
                        "reason": item["data"].get("reason", ""),
                    }
                    for item in selected
                    if item["kind"] == "bad_case"
                ],
                "evidence_trace_range": {
                    "source": "evaluation_feedback",
                    "from_round": number - 1,
                    "accepted_ref": feedback["current_ref"],
                    "evidence": selected,
                },
            }
        )
    return result
