"""Partition diagnostics and bounded repair; all model IO is synthetic."""

import asyncio
import copy
import json
import logging
from typing import Any

import httpx
import pytest

from app.service.evolution.skill_core import (
    ClusterValidationError,
    SkillEvolutionCore,
    validate_clusters,
)
from app.third_party_api.skill_model import AdapterError, AnthropicModel, StructuredSkillModel

REPORTS = [
    {"probe_id": f"p{i}", "attribute_type": "prompt", "probe_description": f"Issue {i}"}
    for i in range(27)
]


def cluster(ids: list[str], name: str = "one", attribute: str = "prompt") -> dict[str, Any]:
    return {"cluster_id": name, "attribute_type": attribute, "probe_ids": ids, "summary": "Issue"}


def invalid_partition() -> list[dict[str, Any]]:
    return [cluster([f"p{i}" for i in range(17)]), cluster(["p0"], "two")]


def test_duplicate_and_missing_members_are_reported_together() -> None:
    with pytest.raises(ClusterValidationError) as raised:
        validate_clusters(invalid_partition(), REPORTS)
    feedback = raised.value.feedback
    assert feedback["counts"] == {"duplicate_membership": 1, "missing_probe_id": 10}
    assert feedback["issues"][0] == {
        "code": "duplicate_membership",
        "probe_id": "p0",
        "cluster_index": 1,
        "first_cluster_index": 0,
    }
    assert {item["probe_id"] for item in feedback["issues"][1:]} == {f"p{i}" for i in range(17, 27)}
    assert feedback["issues_truncated"] is False


@pytest.mark.parametrize(
    "raw,code",
    [
        ({}, "invalid_shape"),
        ([{"summary": "not a cluster"}], "invalid_schema"),
        ([cluster(["p0", "p0"])], "duplicate_membership"),
        ([cluster(["p0"]), cluster(["p1"])], "duplicate_cluster_id"),
        ([cluster(["invented"])], "unknown_probe_id"),
        ([cluster(["p0"], attribute="error")], "attribute_mismatch"),
        ([], "missing_probe_id"),
    ],
)
def test_invalid_partitions_have_specific_diagnostics(raw: Any, code: str) -> None:
    with pytest.raises(ClusterValidationError) as raised:
        validate_clusters(raw, REPORTS)
    assert raised.value.feedback["counts"][code] >= 1
    assert code in str(raised.value)


def test_diagnostics_are_bounded_without_truncating_probe_identity() -> None:
    probe_id = "long-" + "x" * 500
    reports = [{**REPORTS[0], "probe_id": probe_id}] + [
        {**REPORTS[0], "probe_id": f"p{i}"} for i in range(99)
    ]
    with pytest.raises(ClusterValidationError) as raised:
        validate_clusters([], reports)
    assert raised.value.feedback["counts"] == {"missing_probe_id": 100}
    assert len(raised.value.feedback["issues"]) == 50
    assert raised.value.feedback["issues"][0]["probe_id"] == probe_id
    assert raised.value.feedback["issues_truncated"] is True
    assert probe_id not in str(raised.value)


class Model(StructuredSkillModel):
    """Scripted generator that exercises the production repair boundary."""

    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []
        self.verified: list[str] = []

    async def _generate(self, operation: str, data: dict[str, Any]) -> Any:
        assert operation == "cluster"
        self.calls.append(copy.deepcopy(data))
        output = self.outputs[len(self.calls) - 1]
        if isinstance(output, BaseException):
            raise output
        return output

    async def verify(
        self,
        cluster: dict[str, Any],
        reports: list[dict[str, Any]],
        files: dict[str, str],
    ) -> dict[str, Any]:
        self.verified.extend(r["probe_id"] for r in reports)
        return {"confidence": 0.9, "severity": 0.5, "reason": "Synthetic evidence"}


@pytest.mark.parametrize("failures", [0, 1, 2])
async def test_repair_preserves_all_inputs_and_verifies_exactly_once(
    failures: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.third_party_api.skill_model")
    valid = [cluster([r["probe_id"] for r in REPORTS])]
    model = Model([invalid_partition()] * failures + [valid])
    original = copy.deepcopy(REPORTS)
    canonical = [{**r, "probe_type": "skill", "probe_analysis": ""} for r in original]
    plan = await SkillEvolutionCore(model).plan(REPORTS, {"SKILL.md": "# Skill"})
    assert original == REPORTS
    assert len(model.calls) == failures + 1
    assert model.calls[0] == {"reports": canonical}
    for call in model.calls[1:]:
        assert call["reports"] == canonical
        assert call["validation_feedback"]["counts"] == {
            "duplicate_membership": 1,
            "missing_probe_id": 10,
        }
    assert model.verified == [r["probe_id"] for r in original]
    assert plan.clusters[0]["support"] == 27
    assert len(caplog.records) == failures + 1


async def test_exhaustion_fails_before_verify_with_safe_actionable_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = Model([invalid_partition()] * 3)
    with pytest.raises(
        AdapterError, match=r"after 3 attempts.*duplicate_membership=1.*missing_probe_id=10"
    ):
        await SkillEvolutionCore(model).plan(REPORTS, {"SKILL.md": "# Skill"})
    assert len(model.calls) == 3
    assert model.verified == []
    assert len(caplog.records) == 3
    assert "Issue" not in caplog.text


@pytest.mark.parametrize("raw", [{}, [{"summary": "malformed"}]])
async def test_shape_failures_can_be_corrected(raw: Any) -> None:
    model = Model([raw, [cluster([r["probe_id"] for r in REPORTS])]])
    assert len(await model.cluster(REPORTS)) == 1
    assert len(model.calls) == 2


@pytest.mark.parametrize("error", [AdapterError("transport failure"), asyncio.CancelledError()])
async def test_transport_errors_and_cancellation_are_not_retried(error: BaseException) -> None:
    model = Model([error])
    with pytest.raises(type(error)):
        await model.cluster(REPORTS)
    assert len(model.calls) == 1


async def test_sensitive_output_is_not_retried_or_echoed() -> None:
    secret = "ghp_" + "x" * 30
    model = Model([[cluster([secret])]])
    with pytest.raises(ValueError, match="Potential secret") as raised:
        await model.cluster(REPORTS)
    assert secret not in str(raised.value)
    assert len(model.calls) == 1


async def test_http_transport_receives_feedback_and_complete_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILL_TEST_KEY", "dummy-test-key")
    calls: list[dict[str, Any]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        ids = list(range(1, 28)) if len(calls) > 1 else [1, 1]
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "req",
                        "name": "submit_clusters",
                        "input": {"groups": [{"reason": "Issue", "members": ids}]},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = AnthropicModel(enabled=True, api_key_env="SKILL_TEST_KEY", client=client)
        result = await model.cluster(REPORTS)
    assert len(result[0]["probe_ids"]) == 27
    assert len(calls) == 2
    assert calls[1]["messages"][0] == calls[0]["messages"][0]
    assert "missing=" in calls[1]["messages"][-1]["content"][0]["content"]
