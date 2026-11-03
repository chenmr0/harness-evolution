"""Mock-only platform protocol, aggregate validation and durable pair coverage."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from app.service.evolution.engine.contracts import EvalGatewayConfig, EvalRequest
from app.service.evolution.engine.http_eval import HttpEvalGateway
from app.third_party_api.evaluation.http import HttpEvaluationClient
from app.third_party_api.evaluation.models import (
    CaseScore,
    EvaluationError,
    compare_case_scores,
    parse_case_score,
)


def completed(*scores: Any) -> dict[str, Any]:
    """Build platform metrics, not a fabricated aggregate verdict."""
    return {
        "status": "completed",
        "report": {
            "metrics": [
                {
                    "key": "hard",
                    "value": score,
                    "tags": {"metric_type": "case", "case_name": f"case-{index}"},
                }
                for index, score in enumerate(scores)
            ]
        },
    }


def config() -> dict[str, Any]:
    return {
        "mode": "http",
        "enabled": True,
        "baseUrl": "https://eval.invalid/api/eval/v1",
        "templateId": "template",
        "tokenEnv": "TEST_EVALUATION_TOKEN",
    }


async def test_protocol_branch_payload_and_mean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_EVALUATION_TOKEN", "test-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = {"task_id": "run-1"} if request.method == "POST" else completed(2, 4, 9)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = HttpEvaluationClient(config(), client=transport)
        assert await client.submit("refs/heads/eval/r1", "template", "stable-key") == "run-1"
        assert await client.get_status("run-1") == "completed"
        result = await client.get_score("run-1")
    assert result.score == 5
    assert result.case_ids == ("case-0", "case-1", "case-2")
    assert json.loads(requests[0].content) == {
        "template_id": "template",
        "params": {"commit": "eval/r1"},
    }
    assert requests[0].headers["Authorization"] == "test-token"
    assert requests[0].headers["Idempotency-Key"] == "stable-key"
    assert requests[0].url.path == "/api/eval/v1/trigger/task_template"
    assert requests[1].url.path == "/api/eval/v1/task/run-1"


@pytest.mark.parametrize("ref", ["refs/tags/v1", "a" * 40, "", "bad..ref", "bad ref"])
async def test_only_branches_can_be_submitted(ref: str) -> None:
    with pytest.raises(EvaluationError, match="branch"):
        await HttpEvaluationClient(config()).submit(ref, "template", "key")


@pytest.mark.parametrize(
    "body",
    [
        {"status": "running"},
        {"status": "completed"},
        completed(),
        completed(True),
        completed(float("nan")),
        completed(float("inf")),
        completed("2"),
        {"status": "completed", "score": 100},
    ],
)
def test_missing_or_invalid_case_results_fail(body: dict[str, Any]) -> None:
    with pytest.raises(EvaluationError):
        parse_case_score(body)


def test_hard_occurrences_and_incomplete_reports() -> None:
    duplicate = completed(1, 0)
    duplicate["report"]["metrics"].append(duplicate["report"]["metrics"][0])
    missing = completed(2)
    missing["report"]["metrics"].append(
        {
            "key": "duration",
            "value": 12,
            "tags": {"metric_type": "case", "case_name": "case-without-score"},
        }
    )
    incomplete = completed(2)
    incomplete["report"]["cases_complete"] = False
    count_mismatch = completed(2)
    count_mismatch["report"]["case_count"] = 2
    assert parse_case_score(duplicate).score == 0.67
    assert CaseScore.from_payload(parse_case_score(duplicate).to_payload()).score == 0.67
    assert parse_case_score(missing).score == 2
    for body in (incomplete, count_mismatch):
        with pytest.raises(EvaluationError):
            parse_case_score(body)


def test_real_report_counts_only_hard_occurrences() -> None:
    body = completed(0, 1, 0, 1)
    metrics = body["report"]["metrics"]
    metrics.extend(
        [
            {"key": "soft", "value": 0.5, "tags": {"metric_type": "case", "case_name": "case-1"}},
            {
                "key": "matched",
                "value": "false",
                "tags": {"metric_type": "case", "case_name": "case-2"},
            },
            {
                "key": "token消耗",
                "value": 3014030,
                "tags": {
                    "metric_type": "case",
                    "case_name": "case45",
                    "parent_case_name_array_json": [],
                },
            },
            {
                "key": "total_score",
                "value": 99,
                "tags": {"metric_type": "case", "case_name": "case-1"},
            },
        ]
    )
    parsed = parse_case_score(body)
    assert parsed.score == 0.50
    assert "case45" not in parsed.case_ids
    assert CaseScore.from_payload(parsed.to_payload()) == parsed
    assert parse_case_score(completed(1, 0, 0)).score == 0.33
    assert parse_case_score(completed(0.125)).score == 0.13
    assert not compare_case_scores(
        parse_case_score(completed(0.331)), parse_case_score(completed(0.334))
    )


def test_strict_comparable_mean_and_persisted_evidence() -> None:
    baseline = parse_case_score(completed(2, 4))
    assert not compare_case_scores(baseline, parse_case_score(completed(1, 5)))
    assert not compare_case_scores(baseline, parse_case_score(completed(1, 4)))
    assert compare_case_scores(baseline, parse_case_score(completed(2, 5)))
    with pytest.raises(EvaluationError, match="comparable"):
        compare_case_scores(baseline, parse_case_score(completed(100)))
    assert CaseScore.from_payload(baseline.to_payload()) == baseline
    with pytest.raises(EvaluationError):
        CaseScore.from_payload({"score": 100, "caseScores": {"a": 1}})


async def test_disabled_missing_credentials_and_http_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="token=do-not-leak")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(EvaluationError, match="disabled"):
            disabled = HttpEvaluationClient({**config(), "enabled": False}, client=transport)
            await disabled.get_status("x")
        monkeypatch.delenv("TEST_EVALUATION_TOKEN", raising=False)
        with pytest.raises(EvaluationError, match="not set"):
            await HttpEvaluationClient(config(), client=transport).get_status("x")
        assert not calls
        monkeypatch.setenv("TEST_EVALUATION_TOKEN", "test-token")
        with pytest.raises(EvaluationError, match="HTTP status 503"):
            await HttpEvaluationClient(config(), client=transport).submit("eval", "template", "key")
        assert len(calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        '{"status":"completed","status":"running"}',
        '{"status":"completed","x":NaN}',
        "[]",
        "invalid",
    ],
)
async def test_malformed_json_fails(text: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_EVALUATION_TOKEN", "test-token")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=text))
    ) as transport:
        with pytest.raises(EvaluationError):
            await HttpEvaluationClient(config(), client=transport).get_status("x")


class MemoryStates:
    """CAS-style test checkpoint keeping unrelated orchestration state intact."""

    def __init__(self) -> None:
        self.checkpoint: dict[str, Any] = {"other": {"keep": True}}
        self.revision = 0

    async def get(self, run_id: str) -> Any:
        return SimpleNamespace(checkpoint=copy.deepcopy(self.checkpoint), revision=self.revision)

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: dict[str, Any],
        *,
        expected_revision: int,
    ) -> Any:
        assert expected_revision == self.revision
        self.checkpoint = copy.deepcopy(checkpoint)
        self.revision += 1
        return await self.get(run_id)


class Platform:
    integration_identity = "test-platform-v1"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_candidate = True

    def validate_submission(self, ref: str, template: str, key: str) -> None:
        pass

    async def submit(self, ref: str, template: str, key: str) -> str:
        self.calls.append(ref)
        if ref == "candidate" and self.fail_candidate:
            raise EvaluationError("submission failed")
        return ref + "-id"

    async def get_status(self, eval_id: str) -> str:
        return "completed"

    async def get_score(self, eval_id: str) -> CaseScore:
        return parse_case_score(completed(2, 4) if eval_id == "baseline-id" else completed(3, 5))


async def test_pair_survives_partial_submission_restart_and_reuses_result() -> None:
    states = MemoryStates()
    platform = Platform()
    request = EvalRequest(
        "run",
        "round",
        "repo",
        "candidate",
        "b" * 40,
        "skill",
        "name",
        "v1",
        "cluster",
        "stable",
        baseline_ref="baseline",
        baseline_commit="a" * 40,
    )

    def gateway() -> HttpEvalGateway:
        return HttpEvalGateway(
            EvalGatewayConfig.from_mapping(config()),
            run_id="run",
            states=cast(Any, states),
            client=cast(Any, platform),
            server_config={"idempotencyGuaranteed": True},
        )

    with pytest.raises(EvaluationError, match="outcome is unknown"):
        await gateway().submit(request)
    platform.fail_candidate = False
    submission = await gateway().submit(request)
    assert len(submission.eval_id) < 128
    assert platform.calls == ["baseline", "candidate", "candidate"]
    assert (await gateway().get_status(submission.eval_id)).status == "completed"
    result = await gateway().get_result(submission.eval_id)
    assert result.current_score == 3 and result.candidate_score == 4
    assert result.external_verdict == "accept"
    assert await gateway().get_result(submission.eval_id) == result
    assert states.checkpoint["other"] == {"keep": True}
    with pytest.raises(EvaluationError, match="conflicts"):
        await gateway().submit(replace(request, ref="different"))


async def test_expected_cases_and_echoed_secrets_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EVALUATION_TOKEN", "Bearer private-eval-secret")
    bodies = [completed(1), {"task_id": "private-eval-secret"}, completed(1)]
    bodies[2]["report"]["metrics"][0]["tags"]["case_name"] = "private-eval-secret"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bodies.pop(0)))
    ) as transport:
        client = HttpEvaluationClient({**config(), "expectedCaseIds": ["other"]}, client=transport)
        with pytest.raises(EvaluationError, match="expected cases"):
            await client.get_score("id")
        with pytest.raises(EvaluationError, match="sensitive"):
            await client.submit("eval", "template", "key")
        with pytest.raises(EvaluationError, match="sensitive"):
            await client.get_score("id")


async def test_run_config_cannot_supply_trusted_connection_settings() -> None:
    with pytest.raises(EvaluationError, match="trusted server"):
        HttpEvalGateway(
            EvalGatewayConfig.from_mapping(config()), run_id="run", states=cast(Any, MemoryStates())
        )


async def test_pair_incomparable_results_never_persist_acceptance() -> None:
    class Incomparable(Platform):
        async def get_score(self, eval_id: str) -> CaseScore:
            return parse_case_score(completed(1) if eval_id == "baseline-id" else completed(10, 20))

    platform = Incomparable()
    platform.fail_candidate = False
    states = MemoryStates()
    gateway = HttpEvalGateway(
        EvalGatewayConfig.from_mapping(config()),
        run_id="run",
        states=cast(Any, states),
        client=cast(Any, platform),
    )
    submission = await gateway.submit(
        EvalRequest(
            "run",
            "round",
            "repo",
            "candidate",
            "b" * 40,
            "skill",
            "name",
            "v1",
            "cluster",
            "stable",
            baseline_ref="baseline",
            baseline_commit="a" * 40,
        )
    )
    with pytest.raises(EvaluationError, match="comparable"):
        await gateway.get_result(submission.eval_id)
    assert "result" not in states.checkpoint["evaluations"][submission.eval_id]


def test_feedback_report_is_preserved_and_malformed_or_secret_feedback_fails() -> None:
    body = completed(2, 3)
    body["report"].update(
        bad_cases=[
            {
                "case_id": "case-0",
                "input": "example",
                "expected": "ok",
                "actual": "bad",
                "reason": "failure detail",
            }
        ],
        bad_cases_complete=True,
    )
    result = parse_case_score(body)
    assert result.to_payload()["report"] == body["report"]
    assert CaseScore.from_payload(result.to_payload()) == result
    update: dict[str, Any]
    for update in (
        {"bad_cases_complete": "true"},
        {"bad_cases": [{}]},
        {"bad_cases": body["report"]["bad_cases"] * 2},
        {"bad_cases": [{"case_id": "x", "reason": "password=private-value"}]},
    ):
        malformed = copy.deepcopy(body)
        malformed["report"].update(update)
        with pytest.raises(EvaluationError):
            parse_case_score(malformed)


async def test_restart_rejects_changed_provider_identity() -> None:
    states = MemoryStates()
    platform = Platform()
    platform.fail_candidate = False
    request = EvalRequest(
        "run",
        "round",
        "repo",
        "candidate",
        "b" * 40,
        "skill",
        "name",
        "v1",
        "cluster",
        "stable",
        baseline_ref="baseline",
        baseline_commit="a" * 40,
    )
    gateway = HttpEvalGateway(
        EvalGatewayConfig.from_mapping(config()),
        run_id="run",
        states=cast(Any, states),
        client=cast(Any, platform),
    )
    submission = await gateway.submit(request)
    platform.integration_identity = "different-server"
    with pytest.raises(EvaluationError, match="conflicts"):
        await gateway.submit(request)
    assert platform.calls == ["baseline", "candidate"]
    with pytest.raises(EvaluationError, match="identity changed"):
        await gateway.get_status(submission.eval_id)
    with pytest.raises(EvaluationError, match="identity changed"):
        await gateway.get_result(submission.eval_id)


@pytest.mark.parametrize(
    "status, expected",
    [
        ("Running", "running"),
        ("queued", "queued"),
        ("COMPLETED", "completed"),
        ("crashed", "failed"),
        ("cancelled", "failed"),
        ("failed", "failed"),
    ],
)
async def test_platform_lifecycle_mapping(
    status: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EVALUATION_TOKEN", "test-token")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": status}))
    ) as transport:
        assert await HttpEvaluationClient(config(), client=transport).get_status("x") == expected


def test_server_template_default_supports_existing_wizard() -> None:
    gateway = HttpEvalGateway(
        EvalGatewayConfig.from_mapping({}),
        run_id="run",
        states=cast(Any, MemoryStates()),
        server_config={**config(), "templateId": "server-default"},
    )
    assert gateway.config == EvalGatewayConfig.from_mapping({})


@pytest.mark.parametrize(
    "change",
    [
        {"baseUrl": 2},
        {"baseUrl": "http://eval.invalid"},
        {"baseUrl": "https://user:password@eval.invalid"},
        {"tokenEnv": "bad-name"},
        {"timeoutSeconds": 0},
        {"timeoutSeconds": True},
        {"expectedCaseIds": []},
        {"expectedCaseIds": ["a", "a"]},
    ],
)
def test_invalid_connection_configuration_is_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(EvaluationError):
        HttpEvaluationClient({**config(), **change})


async def test_invalid_request_response_and_transport_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EVALUATION_TOKEN", "test-token")
    bodies = [{"task_id": ""}, {"status": "unknown"}]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bodies.pop(0)))
    ) as transport:
        client = HttpEvaluationClient(config(), client=transport)
        for template, key in (("", "key"), ("template", "")):
            with pytest.raises(EvaluationError):
                await client.submit("branch", template, key)
        with pytest.raises(EvaluationError):
            await client.get_status("..")
        with pytest.raises(EvaluationError):
            await client.submit("branch", "template", "key")
        with pytest.raises(EvaluationError):
            await client.get_status("id")
        assert client.integration_identity == HttpEvaluationClient(config()).integration_identity

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private internal URL", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as transport:
        with pytest.raises(EvaluationError, match="HTTP request failed"):
            await HttpEvaluationClient(config(), client=transport).get_status("id")


async def test_pair_wait_failure_unknown_id_and_cas_conflict() -> None:
    from app.db.crud.skill_evolution import SkillCheckpointConflict

    class ConflictingStates(MemoryStates):
        conflicts = 1

        async def save_checkpoint(
            self,
            run_id: str,
            checkpoint: dict[str, Any],
            *,
            expected_revision: int,
        ) -> Any:
            if self.conflicts:
                self.conflicts -= 1
                raise SkillCheckpointConflict("another writer advanced")
            return await super().save_checkpoint(
                run_id,
                checkpoint,
                expected_revision=expected_revision,
            )

    class WaitingPlatform(Platform):
        status = "running"

        async def get_status(self, eval_id: str) -> str:
            return self.status

    states = ConflictingStates()
    platform = WaitingPlatform()
    platform.fail_candidate = False
    gateway = HttpEvalGateway(
        EvalGatewayConfig.from_mapping(config()),
        run_id="run",
        states=cast(Any, states),
        client=cast(Any, platform),
    )
    request = EvalRequest(
        "run",
        "round",
        "repo",
        "candidate",
        "b" * 40,
        "skill",
        "name",
        "v1",
        "cluster",
        "stable",
        baseline_ref="baseline",
        baseline_commit="a" * 40,
    )
    with pytest.raises(EvaluationError):
        await gateway.get_status("foreign-id")
    with pytest.raises(EvaluationError):
        await gateway.submit(replace(request, baseline_ref=None))
    with pytest.raises(EvaluationError):
        await gateway.submit(replace(request, idempotency_key=""))
    submission = await gateway.submit(request)
    assert (await gateway.get_status(submission.eval_id)).status == "running"
    platform.status = "failed"
    assert (await gateway.get_status(submission.eval_id)).status == "failed"
    del states.checkpoint["evaluations"][submission.eval_id]["candidateId"]
    with pytest.raises(EvaluationError, match="incomplete"):
        await gateway.get_status(submission.eval_id)


@pytest.mark.parametrize(
    "metric",
    [
        None,
        {"key": "hard", "value": 5},
        {"key": "hard", "value": 5, "tags": {"metric_type": "unknown"}},
        {"key": "duration", "value": 5, "tags": {"metric_type": "case"}},
    ],
)
def test_malformed_metrics_fail(metric: Any) -> None:
    body = completed(1)
    body["report"]["metrics"].append(metric)
    with pytest.raises(EvaluationError):
        parse_case_score(body)


def test_all_hard_entries_contribute_and_bad_case_coverage_not_invented() -> None:
    body = completed(2, 4)
    body["report"]["metrics"].append(
        {
            "key": "hard",
            "value": 999,
            "tags": {"metric_type": "global"},
        }
    )
    assert parse_case_score(body).score == 335
    body["report"]["bad_cases_complete"] = True
    with pytest.raises(EvaluationError):
        parse_case_score(body)


async def test_unknown_post_outcome_is_not_retransmitted_after_restart() -> None:
    from app.third_party_api.evaluation.models import AmbiguousEvaluationSubmission

    states = MemoryStates()
    platform = Platform()
    request = EvalRequest(
        "run",
        "round",
        "repo",
        "candidate",
        "b" * 40,
        "skill",
        "name",
        "v1",
        "cluster",
        "stable",
        baseline_ref="baseline",
        baseline_commit="a" * 40,
    )

    def gateway() -> HttpEvalGateway:
        return HttpEvalGateway(
            EvalGatewayConfig.from_mapping(config()),
            run_id="run",
            states=cast(Any, states),
            client=cast(Any, platform),
        )

    with pytest.raises(AmbiguousEvaluationSubmission, match="reconcile"):
        await gateway().submit(request)
    entry = next(iter(states.checkpoint["evaluations"].values()))
    assert entry["baselineId"] == "baseline-id"
    assert entry["candidateSubmissionIntent"] == {"key": "stable/candidate"}
    assert "candidateId" not in entry
    platform.fail_candidate = False
    with pytest.raises(AmbiguousEvaluationSubmission, match="retransmission is disabled"):
        await gateway().submit(request)
    assert platform.calls == ["baseline", "candidate"]


async def test_missing_credentials_preflight_does_not_record_ambiguous_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_EVALUATION_TOKEN", raising=False)
    states = MemoryStates()
    client = HttpEvaluationClient(config())
    gateway = HttpEvalGateway(
        EvalGatewayConfig.from_mapping(config()),
        run_id="run",
        states=cast(Any, states),
        client=client,
    )
    request = EvalRequest(
        "run",
        "round",
        "repo",
        "candidate",
        "b" * 40,
        "skill",
        "name",
        "v1",
        "cluster",
        "stable",
        baseline_ref="baseline",
        baseline_commit="a" * 40,
    )
    with pytest.raises(EvaluationError, match="not set"):
        await gateway.submit(request)
    entry = next(iter(states.checkpoint["evaluations"].values()))
    assert "baselineSubmissionIntent" not in entry
    assert "candidateSubmissionIntent" not in entry


async def test_concurrent_submitters_cannot_both_claim_unverified_post() -> None:
    import asyncio

    from app.third_party_api.evaluation.models import AmbiguousEvaluationSubmission

    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowPlatform(Platform):
        async def submit(self, ref: str, template: str, key: str) -> str:
            self.calls.append(ref)
            if ref == "baseline":
                entered.set()
                await release.wait()
            return ref + "-id"

    platform = SlowPlatform()
    states = MemoryStates()
    request = EvalRequest(
        "run",
        "round",
        "repo",
        "candidate",
        "b" * 40,
        "skill",
        "name",
        "v1",
        "cluster",
        "stable",
        baseline_ref="baseline",
        baseline_commit="a" * 40,
    )
    gateway = HttpEvalGateway(
        EvalGatewayConfig.from_mapping(config()),
        run_id="run",
        states=cast(Any, states),
        client=cast(Any, platform),
    )
    first = asyncio.create_task(gateway.submit(request))
    await entered.wait()
    try:
        with pytest.raises(AmbiguousEvaluationSubmission):
            await gateway.submit(request)
    finally:
        release.set()
        await first
    assert platform.calls == ["baseline", "candidate"]
