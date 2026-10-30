"""Synchronous launch validation and asynchronous run admission."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeHitQuery, ProbeQueryRepository
from app.errors import AppError, ErrorCode
from app.service.evolution.engine.factory import EngineFactory
from app.service.evolution.events import EvolutionRunEventService
from app.service.evolution.probe_query import (
    ProbeQueryRequest,
    RunAccepted,
    normalized_query,
)
from app.service.jobs import JobRunner, sanitize_job_payload


class LaunchTargetRecord(Protocol):
    """Read-only enabled-target facts consumed from configuration storage."""

    @property
    def id(self) -> str:
        """Return the configured target id."""

    @property
    def object_type(self) -> str:
        """Return the configured object type."""

    @property
    def object_name(self) -> str:
        """Return the configured object name."""

    @property
    def default_ref(self) -> str:
        """Return the configured default reference."""

    @property
    def repo_url(self) -> str:
        """Return the configured git remote."""

    @property
    def sub_dir(self) -> str:
        """Return the configured repository sub directory."""

    @property
    def enabled(self) -> bool:
        """Return whether the target accepts launches."""


class LaunchTargetStore(Protocol):
    """Persistence operations consumed by the launch service."""

    async def find(self, object_type: str, object_name: str) -> LaunchTargetRecord | None:
        """Return one configured object pair."""
        ...


class EvolutionLaunchService:
    """Validate wizard conditions, snapshot the run, and enqueue preparation."""

    def __init__(
        self,
        targets: LaunchTargetStore,
        probe_repository: ProbeQueryRepository,
        runs: EvolutionRepository,
        jobs: JobRunner,
        events: EvolutionRunEventService | None = None,
        *,
        real_skill_enabled: bool = False,
    ) -> None:
        """Bind launch collaborators for both run modes."""
        self._targets = targets
        self._probe_repository = probe_repository
        self._runs = runs
        self._jobs = jobs
        self._events = events
        self._real_skill_enabled = real_skill_enabled

    async def request_run(
        self,
        body: ProbeQueryRequest,
        *,
        expected_mode: Literal["full", "cluster_only"],
        created_by: str | None,
    ) -> RunAccepted:
        """Validate one launch and admit its asynchronous preparation.

        Args:
            body: Validated wizard condition including the requested mode.
            expected_mode: Mode served by the calling endpoint.
            created_by: Authenticated principal that started the run.

        Returns:
            The accepted run identifier, job identifier, and preparing status.

        Raises:
            AppError: On mode mismatch, inverted windows, disabled targets, or
                when no analyzed probe matches the condition.
        """
        if body.mode != expected_mode:
            raise AppError(
                status_code=422,
                code=ErrorCode.VALIDATION_INVALID,
                message="launch mode does not match the endpoint",
            )
        engine_key = body.engine_key or body.mode
        if not EngineFactory.is_registered(engine_key):
            raise AppError(
                status_code=422,
                code=ErrorCode.ENGINE_INVALID,
                message="evolution engine is not registered",
                details={"engineKey": engine_key},
            )
        config = await self._targets.find(body.target_type, body.target_name)
        if config is None or not config.enabled:
            raise AppError(
                status_code=409,
                code=ErrorCode.TARGET_NOT_CONFIGURED,
                message="target is not configured or enabled",
            )
        query = normalized_query(body, default_version=config.default_ref)
        await self._require_hits(query)
        run = await self._runs.create_run(
            self._run_values(body, query, config, created_by=created_by),
        )
        if self._events is not None:
            await self._events.record(
                run.id,
                "run_created",
                level="info",
                stage="prepare",
                message="运行已创建",
                payload={
                    "mode": run.mode,
                    "targetType": run.target_type,
                    "targetName": run.target_name,
                    "targetVersion": run.target_version,
                },
            )
        job = await self._jobs.submit(
            kind="evolution_prepare",
            payload={"runId": run.id, "query": _query_payload(body, query.target_version)},
            ref_id=run.id,
            created_by=created_by,
        )
        return RunAccepted(run_id=run.id, job_id=job.id, status="preparing")

    async def _require_hits(self, query: ProbeHitQuery) -> None:
        """Fail the launch before any write when no analyzed probe matches.

        Args:
            query: Normalized wizard condition with resolved version.

        Raises:
            AppError: With `NO_PROBES_MATCHED` when the hit count is zero.
        """
        rows = await self._probe_repository.list_hit_rows(query)
        if not rows:
            raise AppError(
                status_code=409,
                code=ErrorCode.NO_PROBES_MATCHED,
                message="no analyzed probe matches the wizard condition",
            )

    def _run_values(
        self,
        body: ProbeQueryRequest,
        query: ProbeHitQuery,
        config: LaunchTargetRecord,
        *,
        created_by: str | None,
    ) -> Mapping[str, Any]:
        """Build the immutable run snapshot inserted before enqueueing.

        Args:
            body: Original wizard condition including engine selection.
            query: Normalized condition with the resolved version.
            config: Enabled target configuration backing the launch.
            created_by: Authenticated principal that started the run.

        Returns:
            Column values for one `preparing` run row.
        """
        eval_config = _snapshot_eval(body.eval)
        # This is a server-owned admission marker, never a client-selected mode.
        eval_config.pop("skillEvolution", None)
        if self._real_skill_enabled and body.mode == "full" and query.target_type == "skill":
            eval_config["skillEvolution"] = "real"
        return {
            "title": f"{query.target_name} {query.target_version} 自进化",
            "mode": body.mode,
            "engine_key": body.engine_key or body.mode,
            "eval_config": eval_config,
            "target_config_id": config.id,
            "target_type": query.target_type,
            "target_name": query.target_name,
            "target_version": query.target_version,
            "repo_url": config.repo_url,
            "sub_dir": config.sub_dir,
            "base_ref": config.default_ref,
            "window_from": query.window_from,
            "window_to": query.window_to,
            "models": list(query.models),
            "status": "preparing",
            "created_by": created_by,
        }


def _snapshot_eval(eval_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Redact credential fields from eval config before the run snapshot is durable.

    Args:
        eval_config: Credential-bearing eval selection from the wizard body.

    Returns:
        The snapshot dictionary with the same redaction applied to job payloads.
    """
    sanitized = sanitize_job_payload(dict(eval_config or {}))
    if not isinstance(sanitized, dict):
        return {}
    return {str(key): value for key, value in sanitized.items()}


def _query_payload(body: ProbeQueryRequest, resolved_version: str) -> dict[str, Any]:
    """Serialize the traceable launch condition stored with the job.

    Args:
        body: Original wizard condition.
        resolved_version: Version resolved from the request or configuration.

    Returns:
        JSON-safe condition payload without any credential fields.
    """
    return {
        "targetType": body.target_type,
        "targetName": body.target_name,
        "targetVersion": resolved_version,
        "from": body.window_from.isoformat() if body.window_from else None,
        "to": body.window_to.isoformat() if body.window_to else None,
        "models": list(body.models),
        "mode": body.mode,
        "engineKey": body.engine_key,
        "eval": body.eval,
    }
