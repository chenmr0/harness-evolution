"""Admission/restart trust-boundary integration, without model or evaluation network calls."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.config import DatabaseDialect, Settings
from app.errors import AppError
from app.main import create_app
from app.service.evolution.engine.contracts import FakeEvalGateway
from app.service.evolution.engine.http_eval import HttpEvalGateway
from app.service.evolution.git_candidate import EvolutionCandidateService
from app.service.evolution.launch import EvolutionLaunchService
from app.service.evolution.prepare import EvolutionPrepareService
from app.service.evolution.probe_query import ProbeQueryRequest


@pytest.mark.parametrize(
    "enabled,target,mode,client_marker,expected",
    [
        (True, "skill", "full", None, "real"),
        (True, "skill", "full", "fake", "real"),
        (False, "skill", "full", "real", None),
        (True, "agent", "full", "real", None),
        (True, "skill", "cluster_only", "real", None),
    ],
)
async def test_admission_marker_is_server_owned(
    enabled: bool,
    target: str,
    mode: str,
    client_marker: str | None,
    expected: str | None,
) -> None:
    """Only enabled full Skill admission may persist the real-mode marker."""
    snapshots: list[dict[str, Any]] = []

    async def create(values: dict[str, Any]) -> Any:
        snapshots.append(dict(values))
        return SimpleNamespace(id="admitted-run", **values)

    targets = SimpleNamespace(
        find=AsyncMock(
            return_value=SimpleNamespace(
                id="target",
                default_ref="main",
                repo_url="https://example.invalid/repo.git",
                sub_dir="skills/example",
                enabled=True,
            )
        )
    )
    probes = SimpleNamespace(list_hit_rows=AsyncMock(return_value=[object()]))
    jobs = SimpleNamespace(submit=AsyncMock(return_value=SimpleNamespace(id="job")))
    service = EvolutionLaunchService(
        cast(Any, targets),
        cast(Any, probes),
        cast(Any, SimpleNamespace(create_run=create)),
        cast(Any, jobs),
        real_skill_enabled=enabled,
    )
    supplied = {"templateId": "test-template", "skillEvolution": client_marker}
    body = ProbeQueryRequest.model_validate(
        {
            "targetType": target,
            "targetName": "example",
            "mode": mode,
            "eval": supplied,
        }
    )
    accepted = await service.request_run(body, expected_mode=cast(Any, mode), created_by=None)
    assert accepted.run_id == "admitted-run"
    assert snapshots[0]["eval_config"].get("skillEvolution") == expected
    assert snapshots[0]["eval_config"]["templateId"] == "test-template"
    assert supplied["skillEvolution"] == client_marker
    jobs.submit.assert_awaited_once()


def _app(tmp_path: Path) -> Any:
    return create_app(
        Settings(
            db_dialect=DatabaseDialect.SQLITE,
            data_dir=tmp_path,
            jwt_secret=SecretStr("mock-admission-signing-material"),
        )
    )


def test_disabled_restart_rejects_real_gateway_but_keeps_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SKILL_EVOLUTION_MODEL_ENABLED", raising=False)
    monkeypatch.delenv("SKILL_EVOLUTION_EVAL_ENABLED", raising=False)
    app = _app(tmp_path)
    resolve = app.state.evolution_eval_poller._gateway_for_run
    real = SimpleNamespace(id="real-run", engine_key="full", eval_config={"skillEvolution": "real"})
    with pytest.raises(ValueError, match="disabled"):
        resolve(real)
    legacy = SimpleNamespace(id="legacy-run", engine_key="full", eval_config={})
    assert isinstance(resolve(legacy), FakeEvalGateway)


def test_enabled_restart_does_not_upgrade_legacy_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILL_EVOLUTION_MODEL_ENABLED", "1")
    monkeypatch.setenv("SKILL_EVOLUTION_EVAL_ENABLED", "1")
    monkeypatch.setenv("SKILL_EVOLUTION_EVAL_URL", "https://eval.invalid/api/eval/v1")
    monkeypatch.setenv("SKILL_EVOLUTION_EVAL_TEMPLATE_ID", "server-template")
    # Construction must not access credentials or issue network requests.
    monkeypatch.setattr("app.main.model_from_environment", lambda: object())
    app = _app(tmp_path)
    resolve = app.state.evolution_eval_poller._gateway_for_run
    legacy = SimpleNamespace(id="legacy-run", engine_key="full", eval_config={})
    real = SimpleNamespace(id="real-run", engine_key="full", eval_config={"skillEvolution": "real"})
    assert isinstance(resolve(legacy), FakeEvalGateway)
    assert isinstance(resolve(real), HttpEvalGateway)


@pytest.mark.parametrize("model,eval_", [("1", "0"), ("0", "1")])
def test_partial_enablement_fails_closed_at_composition(
    model: str,
    eval_: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILL_EVOLUTION_MODEL_ENABLED", model)
    monkeypatch.setenv("SKILL_EVOLUTION_EVAL_ENABLED", eval_)
    with pytest.raises(ValueError, match="both be enabled"):
        _app(tmp_path)


async def test_real_prepare_and_candidate_cannot_fallback_when_model_disabled() -> None:
    run = SimpleNamespace(
        id="run", target_type="skill", mode="full", eval_config={"skillEvolution": "real"}
    )
    runs = SimpleNamespace(get_run=AsyncMock(return_value=run))
    workspace = SimpleNamespace(prepare=AsyncMock())
    probes = SimpleNamespace(list_matching=AsyncMock())
    prepare = EvolutionPrepareService(cast(Any, runs), cast(Any, probes), cast(Any, workspace))
    with pytest.raises(ValueError, match="refusing placeholder fallback"):
        await prepare.handle(cast(Any, SimpleNamespace(ref_id="run", payload={})))
    probes.list_matching.assert_not_awaited()
    workspace.prepare.assert_not_awaited()
    candidates = EvolutionCandidateService(cast(Any, runs), cast(Any, workspace), events=None)
    with pytest.raises(ValueError, match="refusing placeholder fallback"):
        await candidates.publish(
            cast(Any, run), cast(Any, SimpleNamespace(status="pending")), cast(Any, object())
        )


async def test_unmarked_legacy_prepare_stays_legacy_when_model_is_enabled() -> None:
    run = SimpleNamespace(
        id="run",
        target_type="skill",
        target_name="example",
        target_version="main",
        mode="full",
        eval_config={},
        status="preparing",
        progress_unit=0,
        models=[],
        window_from=None,
        window_to=None,
    )
    runs = SimpleNamespace(
        get_run=AsyncMock(return_value=run), list_clusters=AsyncMock(return_value=[])
    )
    probes = SimpleNamespace(list_matching=AsyncMock(return_value=[]))
    service = EvolutionPrepareService(
        cast(Any, runs),
        cast(Any, probes),
        cast(Any, object()),
        skill_core=object(),
        skill_states=SimpleNamespace(get=AsyncMock(return_value=None)),
    )
    with pytest.raises(AppError, match="no analyzed probe"):
        await service.handle(cast(Any, SimpleNamespace(ref_id="run", payload={})))
    probes.list_matching.assert_awaited_once()
