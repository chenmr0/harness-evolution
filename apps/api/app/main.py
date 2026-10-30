"""FastAPI application composition root."""

import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.acquisition import router as acquisition_router
from app.api.auth import router as auth_router
from app.api.catalogs import router as catalogs_router
from app.api.events import router as events_router
from app.api.evolution_launch import preview_router, probe_router
from app.api.evolution_runs import router as evolution_runs_router
from app.api.evolution_targets import router as evolution_targets_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.problems import router as problems_router
from app.api.sessions import router as sessions_router
from app.api.users import router as users_router
from app.config import Settings
from app.db.base import Database
from app.db.crud.bad_case_catalog_sync import BadCaseCatalogSyncRepository
from app.db.crud.bad_case_sync import BadCaseRootSyncRepository
from app.db.crud.bad_cases import BadCaseRepository
from app.db.crud.evolution import EvolutionRepository
from app.db.crud.evolution_targets import EvolutionTargetRepository
from app.db.crud.jobs import JobRepository
from app.db.crud.probe_query import ProbeQueryRepository
from app.db.crud.problems import ProblemRepository
from app.db.crud.run_events import SqlRunEventStore
from app.db.crud.sessions import SessionRepository
from app.db.crud.skill_evolution import SkillEvolutionRepository
from app.db.factory import create_database
from app.db.migrate import upgrade_database
from app.db.models.jobs import JobRunRecord
from app.errors import register_exception_handlers
from app.middleware.auth import AuthMiddleware
from app.middleware.rate_limit import LoginRateLimiter, LoginRateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.service.acquisition.bad_case_catalog_sync import (
    KIND as BAD_CASE_CATALOG_SYNC_KIND,
)
from app.service.acquisition.bad_case_catalog_sync import BadCaseCatalogSyncService
from app.service.acquisition.bad_case_catalog_sync_coordinator import (
    BadCaseCatalogSyncCoordinator,
)
from app.service.acquisition.bad_case_sync import BadCaseRootSyncService
from app.service.acquisition.bad_case_sync_coordinator import (
    KIND as BAD_CASE_ROOT_SYNC_KIND,
)
from app.service.acquisition.bad_case_sync_coordinator import (
    BadCaseRootSyncCoordinator,
)
from app.service.acquisition.sync import SyncCoordinator
from app.service.acquisition.upstream_pull import UpstreamPullService
from app.service.bad_cases.service import BadCaseService
from app.service.catalogs import CatalogService
from app.service.events import EventBus
from app.service.evolution.advance import EvolutionAdvanceService, EvolutionJobLifecycle
from app.service.evolution.apply_final import (
    EvolutionApplyService,
    EvolutionFinalizeService,
)
from app.service.evolution.blocked_sweeper import EvolutionBlockedSweeper
from app.service.evolution.engine.contracts import EngineDeps, EvalGatewayConfig
from app.service.evolution.engine.factory import EngineFactory
from app.service.evolution.engine.http_eval import HttpEvalGateway
from app.service.evolution.eval_poller import (
    EvolutionEvalPoller,
    EvolutionEvalSubmitter,
)
from app.service.evolution.events import (
    EvolutionRunEventService,
    EvolutionRunTopicAuthorizer,
)
from app.service.evolution.git_candidate import EvolutionCandidateService
from app.service.evolution.launch import EvolutionLaunchService
from app.service.evolution.prepare import (
    EvolutionPrepareService,
    RepositoryProbeReader,
    WorkspacePreparer,
)
from app.service.evolution.probe_query import ProbeQueryService
from app.service.evolution.retry import EvolutionRetryService
from app.service.evolution.runs import EvolutionRunService
from app.service.evolution.skill_contracts import strategy_from_environment
from app.service.evolution.skill_core import SkillEvolutionCore
from app.service.evolution.skill_feedback import SkillFeedbackService
from app.service.evolution.stages import EvolutionStageService
from app.service.evolution.targets import EvolutionTargetService
from app.service.identity.service import IdentityService
from app.service.jobs import JobRunner
from app.service.problems.service import ProblemService
from app.service.sessions.service import SessionService
from app.third_party_api.bad_case_query.http import HttpBadCaseCatalogSource
from app.third_party_api.bad_case_query.models import BadCaseCatalogSource
from app.third_party_api.git.remote import GitRemote, SubprocessGitRemote
from app.third_party_api.git.workspace import GitWorkspaceManager
from app.third_party_api.session_query.http import HttpSessionQuerySource
from app.third_party_api.session_query.models import SessionQuerySource
from app.third_party_api.skill_model import model_from_environment
from app.third_party_api.trajectory.http import HttpTrajectorySource
from app.third_party_api.trajectory.source import TrajectorySource

logger = logging.getLogger(__name__)


def create_default_app() -> FastAPI:
    """Build the environment-configured application for uvicorn's factory mode."""
    return create_app(Settings())


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    trajectory_source: TrajectorySource | None = None,
    session_query_source: SessionQuerySource | None = None,
    bad_case_catalog_source: BadCaseCatalogSource | None = None,
    git_remote: GitRemote | None = None,
    git_workspaces: WorkspacePreparer | None = None,
) -> FastAPI:
    """Compose middleware, exception handling, and routes."""
    resolved_settings = settings or Settings()
    resolved_database = database or create_database(resolved_settings)
    identity = IdentityService(resolved_settings, resolved_database)
    event_bus = EventBus(queue_capacity=resolved_settings.sse_queue_capacity)
    run_event_store = SqlRunEventStore(resolved_database)
    evolution_repository = EvolutionRepository(resolved_database)
    evolution_events = EvolutionRunEventService(
        run_event_store,
        evolution_repository,
        event_bus,
    )
    job_repository = JobRepository(resolved_database)
    jobs = JobRunner(job_repository, event_bus, evolution_events)
    upstream_client: httpx.AsyncClient | None = None
    resolved_source = trajectory_source
    if resolved_source is None and resolved_settings.upstream_configured:
        token = resolved_settings.upstream_token
        if token is None:
            raise RuntimeError("upstream configuration lost its token")
        upstream_client = httpx.AsyncClient(
            base_url=resolved_settings.upstream_base_url or "",
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            timeout=resolved_settings.upstream_timeout_seconds,
            follow_redirects=False,
        )
        resolved_source = HttpTrajectorySource(
            upstream_client,
            supported_major=resolved_settings.upstream_api_major,
            max_retries=resolved_settings.upstream_retry_attempts,
            backoff_seconds=resolved_settings.upstream_retry_backoff_seconds,
            max_pages=resolved_settings.upstream_max_pages,
        )
    pull_service = UpstreamPullService(resolved_database, jobs, resolved_source)
    jobs.register_handler("pull", pull_service.handle)
    session_query_client: httpx.AsyncClient | None = None
    resolved_session_query_source = session_query_source
    if resolved_session_query_source is None and resolved_settings.session_query_configured:
        query_token = resolved_settings.session_query_token
        if query_token is None:
            raise RuntimeError("session query configuration lost its token")
        session_query_client = httpx.AsyncClient(
            base_url=resolved_settings.session_query_base_url or "",
            headers={"Authorization": f"Bearer {query_token.get_secret_value()}"},
            timeout=resolved_settings.session_query_timeout_seconds,
            follow_redirects=False,
        )
        resolved_session_query_source = HttpSessionQuerySource(
            session_query_client,
            max_retries=resolved_settings.session_query_retry_attempts,
            backoff_seconds=resolved_settings.session_query_retry_backoff_seconds,
        )
    bad_case_sync_repository = BadCaseRootSyncRepository(resolved_database)
    bad_case_sync_service = BadCaseRootSyncService.from_settings(
        resolved_database,
        bad_case_sync_repository,
        resolved_session_query_source,
        resolved_settings,
    )
    bad_case_sync = BadCaseRootSyncCoordinator(
        jobs,
        bad_case_sync_repository,
        resolved_settings,
    )
    jobs.register_handler(BAD_CASE_ROOT_SYNC_KIND, bad_case_sync_service.handle)
    resolved_catalog_source = bad_case_catalog_source
    if resolved_catalog_source is None and resolved_settings.bad_case_query_configured:
        catalog_token = resolved_settings.bad_case_query_token
        if catalog_token is None:
            raise RuntimeError("bad-case query configuration lost its token")
        catalog_client = httpx.AsyncClient(
            base_url=resolved_settings.bad_case_query_base_url or "",
            headers={"Authorization": f"Bearer {catalog_token.get_secret_value()}"},
            timeout=resolved_settings.bad_case_query_timeout_seconds,
            follow_redirects=False,
        )
        resolved_catalog_source = HttpBadCaseCatalogSource(
            catalog_client,
            max_retries=resolved_settings.bad_case_query_retry_attempts,
            backoff_seconds=resolved_settings.bad_case_query_retry_backoff_seconds,
        )
    catalog_repository = BadCaseCatalogSyncRepository(resolved_database)
    catalog_service = BadCaseCatalogSyncService.from_settings(
        catalog_repository,
        resolved_catalog_source,
        resolved_settings,
    )
    catalog_sync = BadCaseCatalogSyncCoordinator(
        jobs,
        catalog_repository,
        resolved_settings,
        catalog_service,
    )
    jobs.register_handler(BAD_CASE_CATALOG_SYNC_KIND, catalog_service.handle)
    sync = SyncCoordinator(
        resolved_database,
        jobs,
        job_repository,
        resolved_settings,
    )
    jobs.set_terminal_hook(sync.release_terminal_job)
    sessions = SessionService(SessionRepository(resolved_database))
    problems = ProblemService(
        ProblemRepository(resolved_database),
        BadCaseService(BadCaseRepository(resolved_database)),
    )
    target_repository = EvolutionTargetRepository(resolved_database)
    resolved_git_remote = git_remote or SubprocessGitRemote()
    evolution_targets = EvolutionTargetService(target_repository, resolved_git_remote)
    probe_query_repository = ProbeQueryRepository(resolved_database)
    evolution_runs = EvolutionRunService(evolution_repository, run_event_store)
    skill_states = SkillEvolutionRepository(resolved_database)
    evolution_stages = EvolutionStageService(
        evolution_repository, probe_query_repository, skill_states=skill_states
    )
    model_enabled = os.environ.get("SKILL_EVOLUTION_MODEL_ENABLED") == "1"
    eval_enabled = os.environ.get("SKILL_EVOLUTION_EVAL_ENABLED") == "1"
    if model_enabled != eval_enabled:
        raise ValueError("Skill model and evaluation must both be enabled")
    evolution_launch = EvolutionLaunchService(
        target_repository,
        probe_query_repository,
        evolution_repository,
        jobs,
        evolution_events,
        real_skill_enabled=model_enabled,
    )
    probe_query = ProbeQueryService(probe_query_repository, target_repository)
    default_workspace_manager = GitWorkspaceManager(
        repos_root=resolved_settings.data_dir / "repos",
        workspaces_root=resolved_settings.data_dir / "workspaces",
        push_enabled=resolved_settings.git_push,
    )
    workspace_manager = git_workspaces or default_workspace_manager
    candidate_workspace_manager = cast(GitWorkspaceManager, workspace_manager)
    lifecycle_workspace_manager = cast(Any, workspace_manager)
    skill_core = (
        SkillEvolutionCore(model_from_environment(), strategy_from_environment())
        if model_enabled
        else None
    )
    eval_server_config = {
        "enabled": eval_enabled,
        "baseUrl": os.environ.get("SKILL_EVOLUTION_EVAL_URL", ""),
        "tokenEnv": os.environ.get("SKILL_EVOLUTION_EVAL_TOKEN_ENV", "EVALUATION_TOKEN"),
        "allowHttp": os.environ.get("SKILL_EVOLUTION_EVAL_ALLOW_HTTP") == "1",
        "timeoutSeconds": float(os.environ.get("SKILL_EVOLUTION_EVAL_TIMEOUT", "30")),
        "templateId": os.environ.get("SKILL_EVOLUTION_EVAL_TEMPLATE_ID", ""),
    }
    prepare_service = EvolutionPrepareService(
        evolution_repository,
        RepositoryProbeReader(probe_query_repository),
        workspace_manager,
        evolution_events,
        skill_core=skill_core,
        skill_states=skill_states,
    )
    evolution_candidates = EvolutionCandidateService(
        evolution_repository,
        candidate_workspace_manager,
        events=evolution_events,
        web_base_url=resolved_settings.web_base_url,
        skill_core=skill_core,
        skill_states=skill_states,
    )

    def gateway_for_run(run: Any) -> Any:
        """Resolve a gateway through the run's registered engine and frozen config."""
        if run.eval_config.get("skillEvolution") == "real":
            if not model_enabled:
                raise ValueError("Real Skill evaluation is disabled; cannot resume this run")
            return HttpEvalGateway(
                EvalGatewayConfig.from_mapping(run.eval_config),
                run_id=run.id,
                states=skill_states,
                server_config=eval_server_config,
            )
        engine = EngineFactory.for_run(
            run,
            EngineDeps(
                reads=cast(Any, RepositoryProbeReader(probe_query_repository)),
                writes=evolution_repository,
                events=evolution_events,
            ),
        )
        return engine.gateway()

    evolution_eval_submitter = EvolutionEvalSubmitter(
        evolution_repository,
        gateway_for_run,
        events=evolution_events,
        poll_interval=timedelta(seconds=resolved_settings.eval_poll_seconds),
    )
    evolution_advance = EvolutionAdvanceService(
        evolution_repository,
        evolution_candidates,
        probe_query_repository,
        evolution_eval_submitter,
        skill_states=skill_states,
    )
    evolution_apply = EvolutionApplyService(
        evolution_repository,
        evolution_candidates,
        probe_query_repository,
        skill_states=skill_states,
    )
    evolution_finalize = EvolutionFinalizeService(
        evolution_repository,
        candidate_workspace_manager,
        events=evolution_events,
        gateway_for_run=gateway_for_run if model_enabled else None,
        skill_states=skill_states if model_enabled else None,
        feedback=SkillFeedbackService(skill_core, skill_states) if skill_core else None,
        poll_interval=timedelta(seconds=resolved_settings.eval_poll_seconds),
        timeout=timedelta(seconds=resolved_settings.eval_timeout_seconds),
    )
    evolution_eval_poller = EvolutionEvalPoller(
        evolution_repository,
        gateway_for_run,
        jobs,
        events=evolution_events,
        scan_interval=timedelta(seconds=resolved_settings.eval_scan_seconds),
        poll_interval=timedelta(seconds=resolved_settings.eval_poll_seconds),
        timeout=timedelta(seconds=resolved_settings.eval_timeout_seconds),
    )
    evolution_lifecycle = EvolutionJobLifecycle(
        evolution_repository,
        jobs,
        lifecycle_workspace_manager,
        before_terminal=sync.release_terminal_job,
        schedule_candidate_advance=getattr(
            workspace_manager,
            "supports_candidate_lifecycle",
            False,
        ),
    )
    jobs.set_terminal_hook(evolution_lifecycle.on_terminal)
    evolution_retry = EvolutionRetryService(
        evolution_repository,
        evolution_candidates,
        jobs,
        apply=evolution_apply,
        finalize=evolution_finalize,
        advance=evolution_advance,
        eval_submitter=evolution_eval_submitter,
        probes=probe_query_repository,
        events=evolution_events,
        skill_states=skill_states,
    )
    evolution_blocked_sweeper = EvolutionBlockedSweeper(
        evolution_repository,
        jobs,
        scan_interval_seconds=resolved_settings.blocked_sweep_seconds,
    )

    async def prepare_handler(job: JobRunRecord) -> Mapping[str, Any]:
        """Run the real prepare workflow for one admitted run."""
        return await prepare_service.handle(job)

    async def advance_handler(job: JobRunRecord) -> Mapping[str, Any]:
        """Publish the first pending round through the real candidate boundary."""
        return await evolution_advance.handle(job)

    async def apply_handler(job: JobRunRecord) -> Mapping[str, Any]:
        """Apply one completed placeholder evaluation."""
        return await evolution_apply.handle(job)

    async def finalize_handler(job: JobRunRecord) -> Mapping[str, Any]:
        """Persist and publish the placeholder terminal evaluation."""
        return await evolution_finalize.handle(job)

    jobs.register_handler("evolution_prepare", prepare_handler)
    jobs.register_handler("evolution_advance", advance_handler)
    jobs.register_handler("evolution_apply", apply_handler)
    jobs.register_handler("evolution_finalize", finalize_handler)
    jobs.register_handler("evolution_retry", evolution_retry.handle)
    catalogs = CatalogService(target_repository, resolved_git_remote)

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        """Start storage and migrations without turning outages into crashes."""
        phase = "startup"
        try:
            await resolved_database.start()
            phase = "migration"
            await upgrade_database(
                resolved_database,
                bootstrap_external_tables=False,
            )
            phase = "identity-seed"
            await identity.seed_admin_if_empty()
            phase = "job-recovery"
            await jobs.recover()
            phase = "sync-scheduler"
            await sync.start_scheduler()
            phase = "bad-case-sync-scheduler"
            await bad_case_sync.start()
            phase = "bad-case-catalog-sync-scheduler"
            await catalog_sync.start()
            phase = "job-dispatch"
            await jobs.start_queued()
            phase = "eval-poller"
            evolution_eval_poller.start()
            phase = "blocked-sweeper"
            evolution_blocked_sweeper.start()
        except Exception as exc:
            logger.error(
                "database initialization failed",
                extra={
                    "dialect": resolved_settings.db_dialect.value,
                    "phase": phase,
                    "error_type": type(exc).__name__,
                },
            )
            lifespan_app.state.database_start_error = "startup or migration failed"
        try:
            yield
        finally:
            try:
                await evolution_blocked_sweeper.stop()
            finally:
                try:
                    await evolution_eval_poller.stop()
                finally:
                    try:
                        await sync.stop_scheduler()
                    finally:
                        try:
                            await bad_case_sync.stop()
                        finally:
                            try:
                                await catalog_sync.stop()
                            finally:
                                try:
                                    await jobs.close()
                                finally:
                                    try:
                                        if resolved_source is not None:
                                            await resolved_source.close()
                                    finally:
                                        try:
                                            if resolved_session_query_source is not None:
                                                await resolved_session_query_source.close()
                                        finally:
                                            try:
                                                if resolved_catalog_source is not None:
                                                    await resolved_catalog_source.close()
                                            finally:
                                                try:
                                                    await event_bus.close()
                                                finally:
                                                    await resolved_database.dispose()

    docs_url = "/api/docs" if resolved_settings.docs_enabled else None
    openapi_url = "/api/openapi.json" if resolved_settings.docs_enabled else None
    app = FastAPI(
        title="harness-evolution-api",
        version=resolved_settings.version,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.identity = identity
    app.state.event_bus = event_bus
    app.state.jobs = jobs
    app.state.sync = sync
    app.state.bad_case_sync = bad_case_sync
    app.state.bad_case_catalog_sync = catalog_sync
    app.state.upstream_pull = pull_service
    app.state.run_events = run_event_store
    app.state.evolution_events = evolution_events
    app.state.sessions = sessions
    app.state.problems = problems
    app.state.evolution_targets = evolution_targets
    app.state.evolution_runs = evolution_runs
    app.state.evolution_stages = evolution_stages
    app.state.evolution_candidates = evolution_candidates
    app.state.evolution_advance = evolution_advance
    app.state.evolution_apply = evolution_apply
    app.state.evolution_finalize = evolution_finalize
    app.state.evolution_retry = evolution_retry
    app.state.blocked_sweeper = evolution_blocked_sweeper
    app.state.evolution_eval_poller = evolution_eval_poller
    app.state.evolution_lifecycle = evolution_lifecycle
    app.state.git_workspaces = workspace_manager
    app.state.evolution_launch = evolution_launch
    app.state.probe_query = probe_query
    app.state.catalogs = catalogs
    app.state.run_event_replay = run_event_store
    app.state.run_topic_authorizer = EvolutionRunTopicAuthorizer(
        evolution_repository,
    )
    app.state.login_rate_limiter = LoginRateLimiter()
    app.state.database_start_error = None

    app.add_middleware(AuthMiddleware)
    app.add_middleware(LoginRateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(jobs_router)
    app.include_router(sessions_router)
    app.include_router(problems_router)
    app.include_router(evolution_runs_router)
    app.include_router(probe_router)
    app.include_router(preview_router)
    app.include_router(evolution_targets_router)
    app.include_router(catalogs_router)
    app.include_router(events_router)
    app.include_router(acquisition_router)
    return app
