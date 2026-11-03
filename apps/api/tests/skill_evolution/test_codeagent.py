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


def tool_client() -> str:
    return r"""import json,sys,subprocess
from pathlib import Path
prompt=sys.stdin.buffer.read()
record=json.loads(Path('input.json').read_text(encoding='utf-8'))
assert Path(record['prompt_file']).read_bytes()==prompt
assert sys.argv[sys.argv.index('--tools')+1] == ''
mcp=json.loads(Path(sys.argv[sys.argv.index('--mcp-config')+1]).read_text())['mcpServers']['skill']
server=subprocess.Popen([mcp['command'],*mcp['args']],
    stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
def send(method,params):
    server.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params})+'\n')
    server.stdin.flush()
    return json.loads(server.stdout.readline())['result']
tools=send('tools/list',{})['tools']
assert len(tools)==1
tool=tools[0]['name']
assert 'mcp__skill__'+tool in sys.argv
assert json.loads(Path(record['response_schema_file']).read_text())==tools[0]['inputSchema']
def submit(args):
    return send('tools/call',{'name':tool,'arguments':args})
"""


@pytest.mark.parametrize("operation", ["verify", "patch", "draft_patch", "attribute"])
async def test_cli_shared_operations(tmp_path: Path, operation: str) -> None:
    value: dict[str, Any]
    if operation == "verify":
        value = {
            "confidence": 0.9,
            "severity": 0.8,
            "reason": "evidence",
            "repair_plan": {"objective": "review", "changes": [], "validation_checks": ["inspect"]},
        }
    elif operation == "attribute":
        value = {"status": "insufficient_evidence", "reason": "No cases", "issues": []}
    else:
        value = {"edits": [{"action": "replace", "file": 1, "old": "old", "new": "better"}]}
    body = tool_client() + f"assert not submit({value!r})['isError']\n" + response(None)
    model = adapter(tmp_path, body)
    if operation == "attribute":
        assert await model.attribute({"evidence": []}, {}) == value
    else:
        result = await getattr(model, operation)({}, [], {"SKILL.md": "# old"})
        if operation == "verify":
            assert result["repair_plan"]["objective"] == "review"
        else:
            assert result == {"SKILL.md": "# better"}


@pytest.mark.parametrize("recover", [True, False])
async def test_cluster_tool_enforces_three_submissions(tmp_path: Path, recover: bool) -> None:
    body = r"""import json,sys,subprocess
from pathlib import Path
prompt=sys.stdin.buffer.read().decode()
assert 'probe-long-id' not in prompt
assert sys.argv[sys.argv.index('--tools')+1] == ''
assert '--restricted' in sys.argv and '--strict-mcp-config' in sys.argv
mcp=json.loads(Path(sys.argv[sys.argv.index('--mcp-config')+1]).read_text())['mcpServers']['skill']
server=subprocess.Popen([mcp['command'],*mcp['args']],
    stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
def send(method,params):
    server.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params})+'\n')
    server.stdin.flush()
    return json.loads(server.stdout.readline())['result']
tools=send('tools/list',{})['tools']
assert [t['name'] for t in tools]==['submit_clusters']
def submit(members):
    groups=[{'reason':'Sample','members':members}]
    return send('tools/call',{'name':'submit_clusters','arguments':{'groups':groups}})
for i in range(2):
    r=submit([1,1])
    assert r['isError'] and 'duplicate=[1]' in str(r) and 'missing=[2]' in str(r)
last=submit([1,2] if RECOVER else [1,1])
assert last['isError'] != RECOVER
if not RECOVER:
    assert 'limit reached' in str(submit([1,2]))
server.stdin.close()
server.wait()
print(json.dumps({'type':'result','subtype':'success'}))
""".replace("RECOVER", str(recover))
    model = adapter(tmp_path, body)
    reports = [{"probe_id": f"probe-long-id-{i}", "attribute_type": "prompt"} for i in (1, 2)]
    if recover:
        result = await model.cluster(reports)
        assert result[0]["probe_ids"] == [r["probe_id"] for r in reports]
    else:
        with pytest.raises(AdapterError, match="after 3 attempts"):
            await model.cluster(reports)
    assert len(list(model.run_root.glob("*/submission-*.json"))) == 3


async def test_cli_rejects_sensitive_input_before_writing_prompt(tmp_path: Path) -> None:
    model = adapter(tmp_path, response([]))
    with pytest.raises(ValueError, match="Potential secret"):
        await model.cluster([{"probe_description": "ghp_" + "x" * 30}])
    assert not model.run_root.exists()


async def test_cli_rejects_oversized_prompt_before_writing_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.third_party_api.codeagent.model_prompt", lambda *_: "x" * 8_000_001)
    model = adapter(tmp_path, response([]))
    with pytest.raises(AdapterError, match="input exceeds size limit"):
        await model.cluster([])
    assert not model.run_root.exists()


@pytest.mark.parametrize(
    "body,message",
    [
        ("import sys; sys.stdin.read(); print('not-json')", "invalid JSON"),
        (response({}).replace('"is_error": false', '"is_error": true'), "complete successfully"),
        (response({}).replace('"structured_output"', '"missing"'), "tool not called"),
        ("import sys; sys.stdin.read(); sys.exit(2)", "exited unsuccessfully"),
        (response({}) + response({}), "complete successfully"),
    ],
)
async def test_cli_rejects_failed_or_malformed_results(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(AdapterError, match=message):
        await adapter(tmp_path, body).verify({}, [], {})


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
        tool_client() + "submit(dict(status='insufficient_evidence',"
        "reason='fixture-credential-value',issues=[]))\n" + response(None),
        env_names=("TEST_PROVIDER_TOKEN",),
    )
    with pytest.raises(AdapterError, match="credential"):
        await model.attribute({"evidence": []}, {})


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
