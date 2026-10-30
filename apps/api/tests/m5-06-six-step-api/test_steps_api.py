"""Authenticated six-step step API contract: snapshots, readiness, and errors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import update

from app.config import DatabaseDialect, Settings
from app.db.base import Database
from app.db.models.evolution import evolution_runs, probe_clusters
from app.main import create_app
from tests.six_step_seed import (
    RUN_ID,
    load_fixture,
    load_snapshot,
    seed_external_corpus,
    seed_fixture,
    seed_run,
)

ADMIN_TEMP_PASSWORD = "six-step-admin-temp-password"
ADMIN_PASSWORD = "six-step-admin-password"

STEP_KEYS: tuple[str, ...] = (
    "input",
    "clusters",
    "attribution",
    "severity",
    "iteration",
    "final",
)


@pytest_asyncio.fixture
async def steps_api(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Provide an authenticated admin client over a migrated application."""
    app = create_app(
        Settings(
            db_dialect=DatabaseDialect.SQLITE,
            data_dir=tmp_path,
            jwt_secret=SecretStr("six-step-api-signing-secret-sufficient-length"),
            admin_username="admin",
            admin_password=SecretStr(ADMIN_TEMP_PASSWORD),
        ),
    )
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    try:
        async with app.router.lifespan_context(app):
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": ADMIN_TEMP_PASSWORD},
            )
            assert login.status_code == 200
            changed = await client.post(
                "/api/auth/change-password",
                headers={"Authorization": f"Bearer {login.json()['accessToken']}"},
                json={"oldPassword": ADMIN_TEMP_PASSWORD, "newPassword": ADMIN_PASSWORD},
            )
            assert changed.status_code == 204
            relogin = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            assert relogin.status_code == 200
            client.headers["Authorization"] = f"Bearer {relogin.json()['accessToken']}"
            yield client
    finally:
        await client.aclose()


def _app_of(client: httpx.AsyncClient) -> Any:
    """Return the composed test application behind an ASGI transport."""
    return cast(Any, client._transport).app


async def _database(client: httpx.AsyncClient) -> Database:
    """Return the application database through the composed app state."""
    return cast(Database, _app_of(client).state.database)


def _step_url(run_id: str, key: str) -> str:
    """Build one step endpoint path."""
    return f"/api/evolution/runs/{run_id}/steps/{key}"


async def test_step_endpoints_require_authentication(
    steps_api: httpx.AsyncClient,
) -> None:
    """Step payloads are only readable for authenticated users."""
    steps_api.headers.pop("Authorization", None)

    for key in STEP_KEYS:
        response = await steps_api.get(_step_url(RUN_ID, key))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("key", STEP_KEYS)
async def test_step_snapshots_match_the_design_examples(
    steps_api: httpx.AsyncClient,
    key: str,
) -> None:
    """Every fixture group reproduces its stored 05 §3.6.1 snapshot over HTTP."""
    data = load_fixture(key)
    await seed_fixture(await _database(steps_api), data)

    response = await steps_api.get(_step_url(data["run"]["id"], key))

    assert response.status_code == 200
    assert response.json() == load_snapshot(key)


@pytest.mark.parametrize("key", STEP_KEYS)
async def test_unknown_run_returns_run_not_found(
    steps_api: httpx.AsyncClient,
    key: str,
) -> None:
    """Unknown run ids 404 with the shared RUN_NOT_FOUND code on every step."""
    response = await steps_api.get(_step_url("evo-missing", key))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.parametrize("key", STEP_KEYS[1:])
async def test_preparing_run_returns_step_not_ready(
    steps_api: httpx.AsyncClient,
    key: str,
) -> None:
    """Unreached steps of a preparing run stay 404 STEP_NOT_READY."""
    database = await _database(steps_api)
    await seed_external_corpus(database)
    await seed_run(database, progress_unit=0, status="preparing")

    response = await steps_api.get(_step_url(RUN_ID, key))

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "STEP_NOT_READY"
    assert body["error"]["details"] == {"key": key}


async def test_preparing_run_serves_the_input_preparation_state(
    steps_api: httpx.AsyncClient,
) -> None:
    """The input step answers preparing runs with the active preparation shell."""
    database = await _database(steps_api)
    await seed_external_corpus(database)
    await seed_run(database, progress_unit=0, status="preparing")

    response = await steps_api.get(_step_url(RUN_ID, "input"))

    assert response.status_code == 200
    body = response.json()
    assert body["key"] == "input"
    assert body["status"] == "active"
    assert body["clickable"] is True
    assert body["payload"]["hits"]["probes"] == 0
    assert body["payload"]["rows"] == []
    assert body["payload"]["condition"] == {
        "targetType": "skill",
        "targetName": "deep-research",
        "targetVersion": "v2.0.0",
        "from": "2026-08-20",
        "to": "2026-09-10",
        "models": ["glm-5.3"],
    }


async def test_cluster_only_run_locks_steps_after_clustering(
    steps_api: httpx.AsyncClient,
) -> None:
    """Cluster-only runs serve null-score clusters and 404 the later four steps."""
    database = await _database(steps_api)
    data = load_fixture("input")
    data = {
        **data,
        "run": {**data["run"], "mode": "cluster_only", "status": "completed"},
    }
    await seed_fixture(database, data)
    async with database.transaction() as session:
        await session.execute(
            update(probe_clusters)
            .where(probe_clusters.c.run_id == data["run"]["id"])
            .values(confidence=None, severity=None, rank=None),
        )

    clusters = await steps_api.get(_step_url(data["run"]["id"], "clusters"))
    assert clusters.status_code == 200
    cluster = clusters.json()["payload"]["clusters"][0]
    assert cluster["confidence"] is None
    assert cluster["severity"] is None

    for key in ("attribution", "severity", "iteration", "final"):
        response = await steps_api.get(_step_url(data["run"]["id"], key))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "STEP_NOT_READY"


async def test_cluster_only_running_run_locks_steps_after_clustering(
    steps_api: httpx.AsyncClient,
) -> None:
    """A still-running cluster-only run 404s the later four steps over HTTP."""
    database = await _database(steps_api)
    data = load_fixture("input")
    data = {**data, "run": {**data["run"], "mode": "cluster_only"}}
    await seed_fixture(database, data)

    clusters = await steps_api.get(_step_url(data["run"]["id"], "clusters"))
    assert clusters.status_code == 200
    assert clusters.json()["status"] == "done"

    for key in ("attribution", "severity", "iteration", "final"):
        response = await steps_api.get(_step_url(data["run"]["id"], key))
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "STEP_NOT_READY"
        assert body["error"]["details"] == {"key": key}


async def test_active_final_without_a_terminal_row_returns_null_verdict(
    steps_api: httpx.AsyncClient,
) -> None:
    """A running run at unit 4+M exposes the final step with null verdict fields."""
    database = await _database(steps_api)
    data = load_fixture("final-active")
    await seed_fixture(database, data)

    response = await steps_api.get(_step_url(data["run"]["id"], "final"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["detail"] is None
    for field in ("verdict", "bestVersion", "bestScore", "initScore"):
        assert body["payload"][field] is None
    assert body["payload"]["metrics"] == []
    assert [change["commit"] for change in body["payload"]["changes"]] == [
        "cd34ab12ef56cd34ab12ef56cd34ab12ef56cd34",
    ]
    assert body == load_snapshot("final-active")


async def test_blocked_run_reads_iteration_and_locks_final(
    steps_api: httpx.AsyncClient,
) -> None:
    """A blocked run keeps its active step readable without advancing the cursor."""
    database = await _database(steps_api)
    data = load_fixture("iteration")
    await seed_fixture(database, data)
    async with database.transaction() as session:
        await session.execute(
            update(evolution_runs)
            .where(evolution_runs.c.id == data["run"]["id"])
            .values(status="blocked", blocked_stage="eval"),
        )

    blocked = await steps_api.get(_step_url(data["run"]["id"], "iteration"))
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "active"
    assert blocked.json()["detail"] == "第 1/1 轮"

    final = await steps_api.get(_step_url(data["run"]["id"], "final"))
    assert final.status_code == 404
    assert final.json()["error"]["code"] == "STEP_NOT_READY"


async def test_repeated_reads_are_idempotent(
    steps_api: httpx.AsyncClient,
) -> None:
    """Repeating one GET returns a byte-identical body and writes nothing."""
    database = await _database(steps_api)
    data = load_fixture("final")
    await seed_fixture(database, data)

    first = await steps_api.get(_step_url(data["run"]["id"], "final"))
    second = await steps_api.get(_step_url(data["run"]["id"], "final"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.text == second.text


async def test_unknown_step_path_is_a_plain_route_miss(
    steps_api: httpx.AsyncClient,
) -> None:
    """Only the closed six-key vocabulary is addressable under /steps."""
    response = await steps_api.get(_step_url(RUN_ID, "unknown"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_real_skill_steps_use_frozen_and_feedback_provenance(
    steps_api: httpx.AsyncClient,
) -> None:
    """All six endpoints project snapshots even after upstream descriptions and links change."""
    from sqlalchemy import delete

    from app.db.crud.skill_evolution import SkillEvolutionRepository
    from app.db.models.domain import probe_reports
    from app.db.models.evolution import evolution_rounds, probe_cluster_probes

    database = await _database(steps_api)
    data = load_fixture("final")
    await seed_fixture(database, data)
    run_id = data["run"]["id"]
    original = {
        "probe_id": "pr-050b-03",
        "attribute_type": "error",
        "probe_description": "Frozen original issue",
        "probe_analysis": "Frozen analysis",
        "session_id": "original-session",
        "from_bad_cases": [{"bad_case_id": "frozen-case"}],
    }
    feedback: dict[str, Any] = {
        "probe_id": "feedback-probe-1",
        "attribute_type": "error",
        "probe_description": "Validation feedback issue",
        "probe_analysis": "Validation evidence",
        "session_id": None,
        "from_bad_cases": [],
    }
    states = SkillEvolutionRepository(database)
    state = await states.initialize(run_id, {"reports": [original], "files": {"SKILL.md": "old"}})
    await states.save_checkpoint(
        run_id,
        {
            "source_reports": [original, feedback],
            "cluster_map": {
                "C1": {"source_probe_ids": [original["probe_id"], feedback["probe_id"]]}
            },
        },
        expected_revision=state.revision,
    )
    async with database.transaction() as session:
        await session.execute(delete(probe_cluster_probes))
        await session.execute(update(probe_reports).values(probe_description="MUTATED UPSTREAM"))
        await session.execute(
            update(evolution_rounds).values(
                patch={
                    "file": "SKILL.md",
                    "changes": {"SKILL.md": "new"},
                    "edit_patch": {"unified_diff": "--- a/SKILL.md\n+++ b/SKILL.md\n-old\n+new\n"},
                }
            )
        )
    payloads = {}
    for key in STEP_KEYS:
        response = await steps_api.get(_step_url(run_id, key))
        assert response.status_code == 200, response.text
        assert "MUTATED UPSTREAM" not in response.text
        payloads[key] = response.json()["payload"]
    assert len(payloads["input"]["rows"]) == 1
    assert payloads["input"]["rows"][0]["description"] == "Frozen original issue"
    assert payloads["input"]["hits"]["badCases"] == 1
    cluster = payloads["clusters"]["clusters"][0]
    assert cluster["probeCount"] == 2
    assert cluster["probes"][1]["description"] == "Validation feedback issue"
    assert cluster["probes"][1]["sourceBadCaseIds"] == []
    assert len(payloads["attribution"]["items"]) == 2
    assert payloads["severity"]["rows"][0]["reportCount"] == 1
    assert payloads["iteration"]["rounds"][0]["probeCount"] == 2
    assert "+new" in payloads["iteration"]["rounds"][0]["patch"]["lines"]
    assert payloads["final"]["changes"][0]["reportCount"] == 1


async def test_skipped_skill_evaluation_preserves_existing_step_dto(
    steps_api: httpx.AsyncClient,
) -> None:
    """A safety-rejected patch never invents a completed evaluation or breaks its step."""
    from app.db.models.evolution import evolution_rounds

    database = await _database(steps_api)
    data = load_fixture("iteration")
    await seed_fixture(database, data)
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds).values(
                status="rejected",
                eval_status="skipped",
                patch=None,
                accepted=False,
            )
        )
    response = await steps_api.get(_step_url(data["run"]["id"], "iteration"))
    assert response.status_code == 200
    round_ = response.json()["payload"]["rounds"][0]
    assert round_["status"] == "rejected"
    assert round_["eval"]["status"] == "skipped"
