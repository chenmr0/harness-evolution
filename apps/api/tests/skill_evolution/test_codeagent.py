"""Real subprocess tests for bounded, opt-in CodeAgent transport."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.third_party_api.codeagent import CodeAgentModel, codeagent_from_environment
from app.third_party_api.skill_model import AdapterError, model_from_environment


def adapter(tmp_path: Path, body: str, **kwargs: Any) -> CodeAgentModel:
    script = tmp_path / "fake_cli.py"
    script.write_text(body, encoding="utf-8")
    return CodeAgentModel(
        sys.executable, tmp_path / "calls", script_path=script, enabled=True, **kwargs
    )


def response(value: Any) -> str:
    return (
        "import json,sys\nsys.stdin.read()\nprint("
        + repr(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {"result": value},
                    "usage": {"input_tokens": 12, "output_tokens": 5},
                }
            )
        )
        + ")\n"
    )


async def test_cli_uses_stdin_without_tools_and_records_only_metadata(tmp_path: Path) -> None:
    body = """import json,sys,os
from pathlib import Path
prompt=sys.stdin.read()
assert 'business-only-value' in prompt
assert 'business-only-value' not in ' '.join(sys.argv)
assert sys.argv[sys.argv.index('--tools')+1] == ''
assert sys.argv[sys.argv.index('--setting-sources')+1] == ''
assert '--restricted' in sys.argv and '--strict-mcp-config' in sys.argv
schema=json.loads(sys.argv[sys.argv.index('--json-schema')+1])
assert schema['properties']['result']['type'] == 'array'
assert 'summary' in schema['properties']['result']['items']['required']
assert 'probe_ids' in schema['properties']['result']['items']['required']
assert os.environ['HOME'] == str(Path.cwd())
""" + response(
        [
            {
                "cluster_id": "one",
                "attribute_type": "prompt",
                "probe_ids": ["probe-1"],
                "summary": "bounded issue",
            }
        ]
    ).replace("sys.stdin.read()", "")
    model = adapter(tmp_path, body)
    result = await model.cluster([{"probe_id": "probe-1", "description": "business-only-value"}])
    assert result[0]["probe_ids"] == ["probe-1"]
    assert model.last_call is not None and model.last_call["status"] == "succeeded"
    assert model.last_call["usage"]["input_tokens"] == 12
    metadata = next((tmp_path / "calls").glob("*/metadata.json")).read_text()
    assert "business-only-value" not in metadata


@pytest.mark.parametrize(
    "operation,value",
    [
        ("verify", {"confidence": 0.9, "severity": 0.8, "reason": "evidence", "repair_plan": {}}),
        ("patch", {"SKILL.md": "new content"}),
        ("attribute", {"status": "insufficient_evidence", "reason": "No cases", "issues": []}),
    ],
)
async def test_cli_shared_operations(tmp_path: Path, operation: str, value: Any) -> None:
    model = adapter(tmp_path, response(value))
    if operation == "attribute":
        assert await model.attribute({}, {}) == value
    else:
        assert await getattr(model, operation)({}, [], {}) == value


@pytest.mark.parametrize(
    "body,message",
    [
        ("import sys; sys.stdin.read(); print('not-json')", "invalid JSON"),
        (response({}).replace('"is_error": false', '"is_error": true'), "complete successfully"),
        (response({}).replace('"structured_output"', '"missing"'), "structured result"),
        ("import sys; sys.stdin.read(); sys.exit(2)", "exited unsuccessfully"),
        (response({}) + response({}), "complete successfully"),
    ],
)
async def test_cli_rejects_failed_or_malformed_results(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(AdapterError, match=message):
        await adapter(tmp_path, body).cluster([])


async def test_cli_bounds_output_and_time(tmp_path: Path) -> None:
    model = adapter(
        tmp_path, "import sys; sys.stdin.read(); print('X'*10000)", max_output_bytes=100
    )
    with pytest.raises(AdapterError, match="size limit"):
        await model.cluster([])
    model = adapter(tmp_path, "import sys,time; sys.stdin.read(); time.sleep(60)", timeout=0.2)
    with pytest.raises(AdapterError, match="timed out"):
        await asyncio.wait_for(model.cluster([]), 5)


async def test_cli_cancellation_cleans_up(tmp_path: Path) -> None:
    model = adapter(tmp_path, "import sys,time; sys.stdin.read(); time.sleep(60)")
    task = asyncio.create_task(model.cluster([]))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert model.last_call is not None and model.last_call["status"] == "cancelled"


async def test_cli_disabled_and_secrets_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = adapter(tmp_path, response([]))
    model.enabled = False
    with pytest.raises(AdapterError, match="disabled"):
        await model.cluster([])
    monkeypatch.setenv("TEST_PROVIDER_TOKEN", "fixture-credential-value")
    model = adapter(
        tmp_path,
        response([{"summary": "fixture-credential-value"}]),
        env_names=("TEST_PROVIDER_TOKEN",),
    )
    with pytest.raises(AdapterError, match="credential"):
        await model.cluster([])


def test_cli_configuration_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="environment name"):
        adapter(tmp_path, "", env_names=("PATH",))
    with pytest.raises(ValueError, match="bounds"):
        adapter(tmp_path, "", timeout=float("nan"))
    with pytest.raises(ValueError, match="effort"):
        adapter(tmp_path, "", effort="invalid")
    with pytest.raises(ValueError, match="native executable"):
        CodeAgentModel("nonexistent-codeagent", tmp_path)
    monkeypatch.setenv("SKILL_EVOLUTION_MODEL_BACKEND", "codeagent")
    monkeypatch.setenv("SKILL_EVOLUTION_CODEAGENT_CMD", sys.executable)
    monkeypatch.setenv("SKILL_EVOLUTION_CODEAGENT_CWD", str(tmp_path))
    monkeypatch.setenv("SKILL_EVOLUTION_MODEL_ENABLED", "1")
    monkeypatch.setenv("SKILL_EVOLUTION_CODEAGENT_ENABLED", "1")
    assert isinstance(model_from_environment(), CodeAgentModel)
    assert codeagent_from_environment().enabled is True
    monkeypatch.setenv("SKILL_EVOLUTION_MODEL_BACKEND", "unknown")
    with pytest.raises(ValueError, match="backend"):
        model_from_environment()
