"""Validation of snapshot-relative edits, review citations and feedback identity."""

import pytest

from app.third_party_api.skill_submission import InvalidSubmission, expand


def replace(old: str, new: str) -> dict[str, object]:
    return {"action": "replace", "file": 1, "old": old, "new": new}


def test_apply_exact_anchors_against_one_snapshot() -> None:
    data = {"files": {"SKILL.md": "a x b x c"}}
    with pytest.raises(InvalidSubmission, match="exactly once"):
        expand("patch", data, {"edits": [replace("a", "z"), replace("x", "y")]})
    with pytest.raises(InvalidSubmission, match="Overlapping"):
        expand("patch", data, {"edits": [replace("a x", "z"), replace("x b", "y")]})
    assert expand("patch", data, {"edits": [replace("a x", "b x"), replace("b x", "b y")]}) == {
        "SKILL.md": "b x b y c"
    }
    assert data["files"]["SKILL.md"] == "a x b x c"


def test_feedback_citations_are_bound_to_current_evidence() -> None:
    data = {
        "feedback": {
            "current_ref": "v2",
            "evidence": [{"ref": "v1", "id": "long-a"}, {"ref": "v2", "id": "long-b"}],
        }
    }
    issue = {
        "attribute_type": "prompt",
        "probe_description": "example",
        "probe_analysis": "reason",
        "evidence_ids": ["1"],
    }
    args = {"status": "actionable", "reason": "example", "issues": [issue]}
    with pytest.raises(InvalidSubmission, match="current_ref"):
        expand("attribute", data, args)
    issue["evidence_ids"] = ["2"]
    assert expand("attribute", data, args)["issues"][0]["evidence_ids"] == ["long-b"]
