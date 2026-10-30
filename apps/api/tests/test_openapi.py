"""OpenAPI snapshot and public schema contracts."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from app.config import DatabaseDialect, Settings
from app.main import create_app
from app.service.evolution.runs import EvolutionRunBlockedStage
from app.service.evolution.stages import RoundStatus

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "packages" / "contracts" / "openapi.json"
API_ROOT = ROOT / "apps" / "api"


def canonicalize(value: Any) -> Any:
    """Return JSON-like data with object keys in a deterministic order."""
    if isinstance(value, dict):
        return {key: canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    return value


def test_openapi_exposes_implemented_identity_and_error_contracts() -> None:
    """Only implemented health, identity, user, and ticket endpoints are exported."""
    schema = create_app(Settings(db_dialect=DatabaseDialect.SQLITE)).openapi()
    components = schema["components"]["schemas"]

    assert set(schema["paths"]) == {
        "/api/health",
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/auth/logout",
        "/api/auth/change-password",
        "/api/auth/me",
        "/api/users",
        "/api/events/ticket",
        "/api/events",
        "/api/jobs/{jobId}",
        "/api/sessions",
        "/api/sessions/{sessionId}",
        "/api/sessions/{sessionId}/tree",
        "/api/sessions/{sessionId}/bad-cases",
        "/api/sessions/{sessionId}/probes",
        "/api/bad-cases/{badCaseId}",
        "/api/bad-cases/{badCaseId}/analysis",
        "/api/probes/{probeId}",
        "/api/acquisition/status",
        "/api/acquisition/sync",
        "/api/acquisition/pull",
        "/api/evolution/runs",
        "/api/evolution/runs/{run_id}",
        "/api/evolution/runs/{run_id}/events",
        "/api/evolution/runs/{run_id}/retry",
        "/api/evolution/runs/{run_id}/steps/input",
        "/api/evolution/runs/{run_id}/steps/clusters",
        "/api/evolution/runs/{run_id}/steps/attribution",
        "/api/evolution/runs/{run_id}/steps/severity",
        "/api/evolution/runs/{run_id}/steps/iteration",
        "/api/evolution/runs/{run_id}/steps/final",
        "/api/evolution/probes/query",
        "/api/evolution/cluster-previews",
        "/api/evolution/targets",
        "/api/evolution/targets/{targetId}",
        "/api/evolution/targets/{targetId}/versions",
        "/api/catalogs/targets",
        "/api/catalogs/models",
    }
    assert {
        "HealthResponse",
        "DatabaseHealthResponse",
        "ErrorCode",
        "ErrorResponse",
        "ErrorBody",
        "LoginRequest",
        "LoginResponse",
        "ChangePasswordRequest",
        "CreateUserRequest",
        "PublicUser",
        "UserListResponse",
        "TicketRequest",
        "EventTicketResponse",
        "JobResponse",
        "SessionStats",
        "SessionListItem",
        "SessionListResponse",
        "SessionEvent",
        "SessionAgent",
        "SessionBadCase",
        "SessionTree",
        "BadCase",
        "BadCaseListResponse",
        "BadCaseAnalysis",
        "ProbeReport",
        "ProbeListResponse",
        "AcquisitionStatusResponse",
        "SyncStatusError",
        "SyncTriggerResponse",
        "EvolutionRunCard",
        "EvolutionRunList",
        "EvolutionRunTarget",
        "EvolutionRunWindow",
        "EvolutionRunError",
        "EvolutionRunStats",
        "EvolutionRunAttributeStats",
        "EvolutionRunProgress",
        "EvolutionRunFinal",
        "EvolutionStepInput",
        "EvolutionStepClusters",
        "EvolutionStepAttribution",
        "EvolutionStepSeverity",
        "EvolutionStepIteration",
        "EvolutionStepFinal",
        "EvolutionTarget",
        "EvolutionTargetWrite",
        "EvolutionTargetList",
        "EvolutionTargetVersions",
        "CatalogTarget",
        "CatalogTargets",
        "CatalogModel",
        "CatalogModels",
    } <= set(components)
    error_body = components["ErrorBody"]
    assert error_body["properties"]["code"]["$ref"] == "#/components/schemas/ErrorCode"
    paths = schema["paths"]
    assert {"200", "401", "403", "422", "429"} <= set(paths["/api/auth/login"]["post"]["responses"])
    assert {"200", "401", "403"} <= set(paths["/api/auth/refresh"]["post"]["responses"])
    assert {"201", "401", "403", "409", "422"} <= set(paths["/api/users"]["post"]["responses"])
    assert {"200", "401", "403", "422"} <= set(paths["/api/events/ticket"]["post"]["responses"])
    login_401 = paths["/api/auth/login"]["post"]["responses"]["401"]
    assert login_401["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    bearer = schema["components"]["securitySchemes"]["BearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    protected_operations = [
        ("/api/auth/logout", "post"),
        ("/api/auth/change-password", "post"),
        ("/api/auth/me", "get"),
        ("/api/users", "get"),
        ("/api/users", "post"),
        ("/api/events/ticket", "post"),
        ("/api/jobs/{jobId}", "get"),
        ("/api/sessions", "get"),
        ("/api/sessions/{sessionId}", "get"),
        ("/api/sessions/{sessionId}/tree", "get"),
        ("/api/sessions/{sessionId}/bad-cases", "get"),
        ("/api/sessions/{sessionId}/probes", "get"),
        ("/api/bad-cases/{badCaseId}", "get"),
        ("/api/bad-cases/{badCaseId}/analysis", "get"),
        ("/api/probes/{probeId}", "get"),
        ("/api/acquisition/status", "get"),
        ("/api/acquisition/sync", "post"),
        ("/api/evolution/runs", "get"),
        ("/api/evolution/runs", "post"),
        ("/api/evolution/runs/{run_id}/steps/input", "get"),
        ("/api/evolution/runs/{run_id}/steps/clusters", "get"),
        ("/api/evolution/runs/{run_id}/steps/attribution", "get"),
        ("/api/evolution/runs/{run_id}/steps/severity", "get"),
        ("/api/evolution/runs/{run_id}/steps/iteration", "get"),
        ("/api/evolution/runs/{run_id}/steps/final", "get"),
        ("/api/evolution/probes/query", "post"),
        ("/api/evolution/cluster-previews", "post"),
        ("/api/evolution/targets", "get"),
        ("/api/evolution/targets", "post"),
        ("/api/evolution/targets/{targetId}", "put"),
        ("/api/evolution/targets/{targetId}", "delete"),
        ("/api/evolution/targets/{targetId}/versions", "get"),
        ("/api/catalogs/targets", "get"),
        ("/api/catalogs/models", "get"),
    ]
    for path, method in protected_operations:
        assert paths[path][method]["security"] == [{"BearerAuth": []}]
    for path, method in [
        ("/api/health", "get"),
        ("/api/auth/login", "post"),
        ("/api/auth/refresh", "post"),
    ]:
        assert "security" not in paths[path][method]

    assert {parameter["name"] for parameter in paths["/api/sessions"]["get"]["parameters"]} == {
        "q",
        "status",
        "sort",
        "limit",
    }
    session_task_kind = components["SessionAgent"]["properties"]["taskKind"]
    assert session_task_kind["anyOf"][0]["enum"] == [
        "in_process_teammate",
        "forked_skill",
    ]
    for path in ("/api/sessions/{sessionId}", "/api/sessions/{sessionId}/tree"):
        validation = paths[path]["get"]["responses"]["422"]
        assert validation["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
    target_collection = paths["/api/evolution/targets"]
    assert paths["/api/evolution/runs"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/EvolutionRunList"}
    step_not_ready = paths["/api/evolution/runs/{run_id}/steps/input"]["get"]["responses"]["404"]
    assert step_not_ready["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    run_card = components["EvolutionRunCard"]
    assert set(run_card["properties"]) == {
        "runId",
        "title",
        "mode",
        "target",
        "window",
        "models",
        "createdBy",
        "createdAt",
        "status",
        "error",
        "stats",
        "progress",
        "final",
    }
    assert components["EvolutionRunWindow"]["properties"]["from"]["anyOf"][0]["format"] == ("date")
    detail_schema = components["EvolutionRunDetail"]
    assert detail_schema["properties"]["evalStatus"]["enum"] == [
        "pending",
        "evaluating",
        "completed",
        "failed",
        "timeout",
    ]
    assert detail_schema["properties"]["blockedStage"]["anyOf"][0]["enum"] == [
        "patch",
        "push",
        "eval",
    ]
    assert components["EvolutionRunWorkspace"]["properties"]["pushStatus"]["enum"] == [
        "pending",
        "pushing",
        "succeeded",
        "failed",
        "skipped",
    ]
    assert components["StepRound"]["properties"]["status"]["enum"] == [
        "pending",
        "patching",
        "pushing",
        "evaluating",
        "deciding",
        "accepted",
        "rejected",
        "skipped",
        "failed",
    ]
    assert components["StepRoundPush"]["properties"]["status"]["enum"] == [
        "pending",
        "pushing",
        "succeeded",
        "skipped",
        "failed",
    ]
    assert components["StepRoundEval"]["properties"]["status"]["enum"] == [
        "pending",
        "evaluating",
        "completed",
        "failed",
        "timeout",
        "skipped",
    ]
    assert target_collection["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/EvolutionTargetList"}
    assert target_collection["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EvolutionTargetWrite"
    }
    assert target_collection["post"]["responses"]["201"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/EvolutionTarget"}
    target_item = paths["/api/evolution/targets/{targetId}"]
    assert target_item["put"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EvolutionTarget"
    }
    assert set(target_item["delete"]["responses"]) >= {
        "204",
        "401",
        "403",
        "404",
        "409",
        "422",
    }
    assert paths["/api/evolution/targets/{targetId}/versions"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {"$ref": "#/components/schemas/EvolutionTargetVersions"}
    assert paths["/api/catalogs/targets"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/CatalogTargets"}
    assert paths["/api/catalogs/models"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/CatalogModels"}
    case_parameters = paths["/api/bad-cases/{badCaseId}"]["get"]["parameters"]
    analysis_parameters = paths["/api/bad-cases/{badCaseId}/analysis"]["get"]["parameters"]
    assert {parameter["name"] for parameter in case_parameters} == {
        "badCaseId",
        "sessionId",
    }
    assert {parameter["name"] for parameter in analysis_parameters} == {
        "badCaseId",
        "sessionId",
    }
    for parameter in [*case_parameters, *analysis_parameters]:
        if parameter["name"] == "sessionId":
            assert parameter["schema"]["anyOf"][0]["minLength"] == 1
    analysis_schema = components["BadCaseAnalysis"]
    assert analysis_schema["discriminator"] == {
        "mapping": {
            "unanalyzed": "#/components/schemas/UnanalyzedBadCaseAnalysis",
            "analyzed_nonissue": "#/components/schemas/NonIssueBadCaseAnalysis",
            "analyzed_issue": "#/components/schemas/IssueBadCaseAnalysis",
        },
        "propertyName": "status",
    }
    assert [item["$ref"] for item in analysis_schema["oneOf"]] == [
        "#/components/schemas/UnanalyzedBadCaseAnalysis",
        "#/components/schemas/NonIssueBadCaseAnalysis",
        "#/components/schemas/IssueBadCaseAnalysis",
    ]
    issue_result = components["IssueAnalysisResult"]["properties"]
    assert set(issue_result) == {"summary"}
    non_issue_result = components["NonIssueAnalysisResult"]["properties"]
    assert set(non_issue_result) == {"reason", "reasoning"}
    assert "409" in paths["/api/bad-cases/{badCaseId}"]["get"]["responses"]
    assert paths["/api/bad-cases/{badCaseId}"]["get"]["responses"]["409"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}
    error_codes = components["ErrorCode"]["enum"]
    assert {
        "BAD_CASE_NOT_FOUND",
        "BAD_CASE_SESSION_MISMATCH",
        "PROBE_NOT_FOUND",
    } <= set(error_codes)
    assert "/api/sessions/{sessionId}/timeline" not in paths
    trigger_responses = paths["/api/acquisition/sync"]["post"]["responses"]
    assert {"200", "202", "401", "403"} <= set(trigger_responses)
    assert trigger_responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SyncTriggerResponse"
    }


def test_evolution_status_aliases_reject_non_design_states() -> None:
    """Runtime validation rejects stages and round states absent from the design."""
    blocked_stage = cast(
        TypeAdapter[EvolutionRunBlockedStage],
        TypeAdapter(cast(Any, EvolutionRunBlockedStage)),
    )
    round_status = cast(
        TypeAdapter[RoundStatus],
        TypeAdapter(cast(Any, RoundStatus)),
    )

    assert blocked_stage.validate_python("patch") == "patch"
    assert round_status.validate_python("evaluating") == "evaluating"

    with pytest.raises(ValidationError):
        blocked_stage.validate_python("prepare")
    with pytest.raises(ValidationError):
        round_status.validate_python("blocked")


def test_checked_in_openapi_snapshot_is_current() -> None:
    """FastAPI schema drift fails until the deterministic snapshot is regenerated."""
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = create_app(Settings(db_dialect=DatabaseDialect.SQLITE)).openapi()

    assert canonicalize(actual) == expected


def test_identity_time_and_secret_configuration_matches_design() -> None:
    """Deployment names and defaults preserve the authoritative auth contract."""
    settings = Settings(db_dialect=DatabaseDialect.SQLITE, _env_file=None)  # type: ignore[call-arg]

    assert settings.access_token_ttl_min == 30
    assert settings.refresh_token_ttl_days == 14
    assert settings.access_ttl_seconds == 1_800
    assert settings.refresh_ttl_seconds == 1_209_600
    assert settings.event_ticket_ttl_seconds == 60
    assert settings.sse_heartbeat_seconds == 15
    assert settings.sse_queue_capacity == 100
    assert settings.event_replay_limit == 500
    assert settings.sync_interval_seconds == 60
    assert settings.sync_lock_ttl_seconds == 1_800


def test_openapi_export_ignores_service_environment() -> None:
    """Canonical snapshots cannot drift because of local HE_* overrides."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("HE_")}
    environment.update(
        {
            "HE_VERSION": "environment-drift",
            "HE_PHASE": "live",
            "HE_DOCS_ENABLED": "true",
            "PATH": os.environ["PATH"],
        }
    )
    result = subprocess.run(
        [sys.executable, "scripts/export_openapi.py"],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["info"]["version"] == "0.1.0"
