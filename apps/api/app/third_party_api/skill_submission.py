"""Validated submissions for one isolated CodeAgent call."""

from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.service.evolution.skill_contracts import FeedbackAttribution, RepairPlan, SkillCritic
from app.service.evolution.skill_safety import (
    reject_sensitive_payload,
    validate_patch,
    validate_path,
)


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=8000)
    members: list[int] = Field(min_length=1, max_length=1000)


class Groups(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groups: list[Group] = Field(min_length=1, max_length=1000)


def cluster_input(data: dict[str, Any]) -> dict[str, Any]:
    """Issue short numbers scoped to this attribute group."""
    return {
        "reports": [
            {**{k: v for k, v in report.items() if k != "probe_id"}, "number": i}
            for i, report in enumerate(data["reports"], 1)
        ]
    }


class InvalidSubmission(ValueError):
    """A bounded correction safe to return to the model."""


def submit_clusters(data: dict[str, Any], arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand only an exact partition of the trusted reports."""
    groups = Groups.model_validate(arguments).groups
    reports = data["reports"]
    expected = set(range(1, len(reports) + 1))
    counts = Counter(member for group in groups for member in group.members)
    duplicates = sorted(n for n, count in counts.items() if count > 1)
    missing = sorted(expected - counts.keys())
    unknown = sorted(counts.keys() - expected)
    if duplicates or missing or unknown:
        raise InvalidSubmission(
            f"duplicate={duplicates[:50]}, missing={missing[:50]}, unknown={unknown[:50]}"
        )
    return [
        {
            "cluster_id": f"group-{i}",
            "attribute_type": reports[0]["attribute_type"],
            "probe_ids": [reports[n - 1]["probe_id"] for n in group.members],
            "probe_cluster_description": group.reason,
        }
        for i, group in enumerate(groups, 1)
    ]


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: Literal["report", "file"]
    number: int = Field(ge=1)
    observation: str = Field(min_length=1, max_length=4000)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    severity: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(max_length=8000)
    evidence: list[Citation] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    repair_plan: RepairPlan


class Replace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["replace"]
    file: int
    old: str = Field(min_length=1)
    new: str


class Add(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["add"]
    path: str
    content: str


class Delete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["delete"]
    file: int


class Edits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    edits: list[Annotated[Replace | Add | Delete, Field(discriminator="action")]] = Field(
        max_length=128
    )


def apply_edits(data: dict[str, Any], args: dict[str, Any]) -> dict[str, str | None]:
    """Resolve all anchors against the accepted snapshot before applying any edit."""
    files = data["files"]
    paths = sorted(files)
    changes: dict[str, str | None] = {}
    spans: dict[str, list[tuple[int, int, str]]] = {}
    for edit in Edits.model_validate(args).edits:
        if isinstance(edit, Add):
            validate_path(edit.path)
            if edit.path in files or edit.path in changes:
                raise InvalidSubmission("Added path already exists")
            changes[edit.path] = edit.content
            continue
        if not 1 <= edit.file <= len(paths):
            raise InvalidSubmission(f"Unknown file number {edit.file}")
        path = paths[edit.file - 1]
        if path in changes:
            raise InvalidSubmission(f"Conflicting operations for file {edit.file}")
        if isinstance(edit, Delete):
            if path in spans:
                raise InvalidSubmission(f"Delete conflicts with replacement in file {edit.file}")
            changes[path] = None
        else:
            pos = files[path].find(edit.old)
            if pos < 0 or files[path].find(edit.old, pos + 1) >= 0:
                raise InvalidSubmission(
                    f"File {edit.file}: old text must match exactly once; include context"
                )
            spans.setdefault(path, []).append((pos, pos + len(edit.old), edit.new))
    for path, edits in spans.items():
        text = files[path]
        boundary = len(text)
        for start, end, new in sorted(edits, reverse=True):
            if end > boundary:
                raise InvalidSubmission("Overlapping edits; combine the replacements")
            text = text[:start] + new + text[end:]
            boundary = start
        changes[path] = text
    validate_patch(files, changes)
    return changes


TOOLS: dict[str, tuple[str, type[BaseModel]]] = {
    "cluster": ("submit_clusters", Groups),
    "verify": ("submit_review", Review),
    "patch": ("submit_edits", Edits),
    "draft_patch": ("submit_edits", Edits),
    "attribute": ("submit_feedback", FeedbackAttribution),
}


def submission_input(operation: str, data: dict[str, Any]) -> dict[str, Any]:
    if operation == "cluster":
        return cluster_input(data)
    payload = dict(data)
    payload["files"] = [
        {"number": n, "path": p, "text": data["files"][p]}
        for n, p in enumerate(sorted(data["files"]), 1)
    ]
    if "reports" in data:
        payload["reports"] = cluster_input(data)["reports"]
        payload["cluster"] = {
            k: v
            for k, v in data["cluster"].items()
            if k not in {"probe_ids", "source_probe_ids", "cluster_id"}
        }
    if operation == "attribute":
        fb = dict(data["feedback"])
        fb["evidence"] = [{**e, "id": str(i)} for i, e in enumerate(fb["evidence"], 1)]
        payload["feedback"] = fb
    return payload


def expand(operation: str, data: dict[str, Any], args: dict[str, Any]) -> Any:
    reject_sensitive_payload(args)
    if operation == "cluster":
        return submit_clusters(data, args)
    if operation in ("patch", "draft_patch"):
        return apply_edits(data, args)
    if operation == "verify":
        review = Review.model_validate(args)
        for change in review.repair_plan.changes:
            if (change.path in data["files"]) == (change.action == "add"):
                raise InvalidSubmission(
                    "Repair plan path does not match snapshot for requested action"
                )
        names = {"report": [r["probe_id"] for r in data["reports"]], "file": sorted(data["files"])}
        evidence = []
        for citation in review.evidence:
            refs = names[citation.source]
            if citation.number > len(refs):
                raise InvalidSubmission(f"Unknown {citation.source} number {citation.number}")
            evidence.append(f"{refs[citation.number - 1]}: {citation.observation}")
        return SkillCritic.model_validate({**review.model_dump(), "evidence": evidence}).model_dump(
            mode="json"
        )
    feedback = FeedbackAttribution.model_validate(args)
    sources = {str(i): e for i, e in enumerate(data["feedback"]["evidence"], 1)}
    for issue in feedback.issues:
        ids = issue.evidence_ids
        if len(set(ids)) != len(ids) or any(i not in sources for i in ids):
            raise InvalidSubmission("Use unique evidence numbers from the supplied list")
        if not any(sources[i]["ref"] == data["feedback"]["current_ref"] for i in ids):
            raise InvalidSubmission("Cite evidence for current_ref")
        issue.evidence_ids = [sources[i]["id"] for i in ids]
    return feedback.model_dump(mode="json")


def correction(error: ValidationError | InvalidSubmission) -> str:
    if isinstance(error, InvalidSubmission):
        return str(error)
    # Never echo rejected input or model-controlled extra field names.
    return "Invalid fields: " + ", ".join(
        f"{e['type']} at {e['loc'] if e['type'] != 'extra_forbidden' else 'unexpected field'}"
        for e in error.errors(include_input=False)[:5]
    )
