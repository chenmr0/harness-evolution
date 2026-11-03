"""Offline model boundary tests: no external service is used."""

import json
from typing import Any

import httpx
import pytest

from app.third_party_api.skill_model import AdapterError, AnthropicModel, model_from_environment


async def test_disabled_model_never_calls_transport() -> None:
    async def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not run")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        model = AnthropicModel(client=client)
        with pytest.raises(AdapterError, match="disabled"):
            await model.cluster([])


async def test_real_model_uses_structured_plan_and_replacements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILL_TEST_KEY", "dummy-test-key")
    calls: list[dict[str, Any]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.headers["x-api-key"] == "dummy-test-key"
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_edits",
                        "id": "req1",
                        "input": {
                            "edits": [
                                {"action": "replace", "file": 1, "old": "Old", "new": "Better\n"}
                            ]
                        },
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = AnthropicModel(enabled=True, api_key_env="SKILL_TEST_KEY", client=client)
        assert await model.patch({}, [], {"SKILL.md": "# Old"}) == {"SKILL.md": "# Better\n"}
    assert "untrusted data" in calls[0]["system"]
    assert "dummy-test-key" not in json.dumps(calls)


@pytest.mark.parametrize(
    "body",
    [
        {"stop_reason": "max_tokens", "content": []},
        {"stop_reason": "end_turn", "content": [{"type": "tool_use"}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"a":1,"a":2}'}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"a":NaN}'}]},
    ],
)
async def test_incomplete_or_ambiguous_model_output_errors(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    monkeypatch.setenv("SKILL_TEST_KEY", "dummy-test-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body),
        )
    ) as client:
        model = AnthropicModel(enabled=True, api_key_env="SKILL_TEST_KEY", client=client)
        with pytest.raises(AdapterError):
            await model.patch({}, [], {"SKILL.md": "old"})


def test_model_environment_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILL_EVOLUTION_MODEL_ENABLED", raising=False)
    assert not model_from_environment().enabled
