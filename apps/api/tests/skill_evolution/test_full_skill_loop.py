"""Full Skill integration over real local Git and SQLite with fake providers."""

# ruff: noqa: S603, S607
from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import insert, update

from app.config import DatabaseDialect, Settings
from app.db.base import Database
from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeQueryRepository
from app.db.crud.skill_evolution import SkillEvolutionRepository
from app.db.factory import create_database
from app.db.migrate import upgrade_database
from app.db.models.domain import bad_case_analysis, bad_cases, probe_reports
from app.db.models.evolution import (
    EvolutionRunRecord,
)
from app.db.models.jobs import JobRunRecord
from app.service.evolution.advance import EvolutionAdvanceService
from app.service.evolution.apply_final import EvolutionApplyService, EvolutionFinalizeService
from app.service.evolution.engine.contracts import EvalGatewayConfig
from app.service.evolution.engine.http_eval import HttpEvalGateway
from app.service.evolution.eval_poller import EvolutionEvalPoller, EvolutionEvalSubmitter
from app.service.evolution.git_candidate import (
    EvolutionCandidateService,
)
from app.service.evolution.launch import EvolutionLaunchService
from app.service.evolution.prepare import EvolutionPrepareService, RepositoryProbeReader
from app.service.evolution.probe_query import ProbeQueryRequest
from app.service.evolution.skill_contracts import SkillStrategy
from app.service.evolution.skill_core import SkillEvolutionCore
from app.service.evolution.skill_feedback import SkillFeedbackService
from app.service.evolution.stages import EvolutionStageService
from app.service.evolution.state import total_units
from app.third_party_api.evaluation.models import CaseScore
from app.third_party_api.git.workspace import (
    GitWorkspaceManager,
)
from app.utils.time import now_shanghai

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def _git(cwd: Path, *args: str) -> str:
    """Run one deterministic local git command."""
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _remote(tmp_path: Path) -> tuple[Path, str]:
    """Create one tagged local remote with a target subdirectory."""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "config", "user.name", "Test User")
    _git(seed, "config", "user.email", "test@example.invalid")
    target = seed / "skills" / "deep-research"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Deep Research\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "tag", "v2.0.0")
    _git(seed, "push", "origin", "HEAD:refs/heads/main", "--tags")
    return bare, _git(seed, "rev-parse", "HEAD")


@pytest_asyncio.fixture
async def prepare_state(
    tmp_path: Path,
) -> AsyncIterator[tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str]]:
    """Provide migrated SQLite state, two probes, and the real prepare service."""
    database = create_database(
        Settings(
            db_dialect=DatabaseDialect.SQLITE,
            data_dir=tmp_path / "db",
            jwt_secret=SecretStr("m5-09-prepare-test-secret"),
        ),
    )
    await database.start()
    await upgrade_database(database)
    bare, base_commit = _remote(tmp_path)
    async with database.transaction() as session:
        for index, attribute_type in enumerate(("model", "error", "prompt"), start=1):
            probe_id = f"probe-{index}"
            bad_case_id = f"case-{index}"
            await session.execute(
                insert(probe_reports).values(
                    probe_id=probe_id,
                    session_id=f"session-{index}",
                    probe_type="skill",
                    probe_object="deep-research",
                    object_version="v2.0.0",
                    attribute_type=attribute_type,
                    probe_description=f"{attribute_type} issue",
                    probe_analysis=f"{attribute_type} analysis",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            )
            await session.execute(
                insert(bad_cases).values(
                    bad_case_id=bad_case_id,
                    bad_case_session_id=f"session-{index}",
                    bad_case_type="behavior",
                    object_name="deep-research",
                    object_type="skill",
                    object_version="v2.0.0",
                    bad_case_agent_id=None,
                    title="observed issue",
                    bad_case_description="observed issue",
                    meta_data={},
                    source="fixture",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            )
            await session.execute(
                insert(bad_case_analysis).values(
                    bad_case_id=bad_case_id,
                    analysis_status="analyzed_issue",
                    probe_id=probe_id,
                    analysis_result={},
                    updated_at=NOW,
                ),
            )
    repository = EvolutionRepository(database)
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos",
        workspaces_root=tmp_path / "workspaces",
    )
    service = EvolutionPrepareService(
        repository,
        RepositoryProbeReader(ProbeQueryRepository(database)),
        manager,
    )
    try:
        yield database, repository, service, str(bare), base_commit
    finally:
        await database.dispose()


def _run_values(repo_url: str, base_commit: str, *, mode: str = "full") -> dict[str, object]:
    """Return one full run snapshot ready for prepare."""
    return {
        "id": "run-m5-09-prepare",
        "title": "deep-research evolution",
        "mode": mode,
        "engine_key": mode,
        "eval_config": {},
        "target_config_id": None,
        "target_type": "skill",
        "target_name": "deep-research",
        "target_version": "v2.0.0",
        "repo_url": repo_url,
        "sub_dir": "skills/deep-research",
        "base_ref": "v2.0.0",
        "base_commit": base_commit,
        "window_from": None,
        "window_to": None,
        "models": [],
        "stats": {},
        "created_by": None,
        "created_at": NOW,
    }


def _job(run_id: str) -> JobRunRecord:
    """Build one evolution prepare job in launch payload shape."""
    return JobRunRecord(
        id="job-prepare",
        kind="evolution_prepare",
        ref_id=run_id,
        status="running",
        progress=0.0,
        executor="local",
        external_job_id=None,
        attempt=0,
        payload={
            "runId": run_id,
            "query": {
                "targetType": "skill",
                "targetName": "deep-research",
                "targetVersion": "v2.0.0",
                "from": None,
                "to": None,
                "models": [],
            },
        },
        result=None,
        error=None,
        created_by=None,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        updated_at=NOW,
    )


class SkillModel:
    """Deterministic source protocol model; real services still validate all output."""

    async def cluster(self, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "cluster_id": "one",
                "attribute_type": reports[0]["attribute_type"],
                "probe_ids": [r["probe_id"] for r in reports],
                "summary": "Issue",
            }
        ]

    async def verify(self, cluster: dict[str, Any], *args: Any) -> dict[str, Any]:
        return {
            "confidence": 0.1 if cluster["attribute_type"] == "model" else 0.9,
            "severity": 0.9,
            "reason": "Verified",
        }

    async def patch(
        self, cluster: dict[str, Any], reports: list[dict[str, Any]], files: dict[str, str]
    ) -> dict[str, str | None]:
        text = "REGRESSION" if cluster["attribute_type"] == "prompt" else "IMPROVEMENT"
        return {"SKILL.md": files["SKILL.md"] + text + "\n", "references/new.md": "Details\n"}

    async def attribute(self, feedback: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
        assert all(e["name"].startswith("validation/") for e in feedback["evaluations"])
        evidence = next(e for e in feedback["evidence"] if e["ref"] == feedback["current_ref"])
        return {
            "status": "actionable",
            "reason": "One more fix",
            "issues": [
                {
                    "attribute_type": "error",
                    "probe_description": "Remaining issue",
                    "probe_analysis": "Grounded in accepted evaluation",
                    "evidence_ids": [evidence["id"]],
                }
            ],
        }


class LocalEvaluation:
    """Fake provider scores real published Git snapshots, including a fresh final retest pair."""

    integration_identity = "local-fixture-v1"

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.submissions: dict[str, str] = {}
        self.templates: list[str] = []

    def validate_submission(self, ref: str, template_id: str, idempotency_key: str) -> None:
        assert ref and template_id and idempotency_key

    async def submit(self, ref: str, template: str, key: str) -> str:
        self.submissions[key] = ref
        self.templates.append(template)
        return key

    async def get_status(self, eval_id: str) -> str:
        return "completed"

    async def get_score(self, eval_id: str) -> CaseScore:
        content = _git(
            self.bare, "show", f"{self.submissions[eval_id]}:skills/deep-research/SKILL.md"
        )
        score = 10.0 + content.count("IMPROVEMENT") * 10 - content.count("REGRESSION") * 30
        return CaseScore(
            (("case1", score),),
            metric_key="hard",
            report={
                "metrics": [
                    {
                        "key": "hard",
                        "value": score,
                        "tags": {"metric_type": "case", "case_name": "case1"},
                    }
                ],
                "bad_cases": [{"case_id": "case1", "reason": "More work possible"}],
                "bad_cases_complete": True,
            },
        )


class Jobs:
    def __init__(self) -> None:
        self.items: list[tuple[str, Mapping[str, Any]]] = []

    async def submit(
        self, *, kind: str, payload: Mapping[str, Any], ref_id: str | None, created_by: str | None
    ) -> Any:
        self.items.append((kind, payload))
        return SimpleNamespace(id=f"job-{len(self.items)}")


async def test_full_skill_launch_feedback_restart_and_final(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    database, runs, _, repo_url, _ = prepare_state
    probes = ProbeQueryRepository(database)
    jobs = Jobs()

    class Targets:
        async def find(self, *args: Any) -> Any:
            return SimpleNamespace(
                id=None,
                enabled=True,
                default_ref="v2.0.0",
                repo_url=repo_url,
                sub_dir="skills/deep-research",
            )

    launch = EvolutionLaunchService(
        Targets(), probes, runs, cast(Any, jobs), real_skill_enabled=True
    )
    accepted = await launch.request_run(
        ProbeQueryRequest.model_validate(
            {
                "targetType": "skill",
                "targetName": "deep-research",
                "targetVersion": "v2.0.0",
                "mode": "full",
                "eval": {"mode": "http", "templateId": "validation", "skillEvolution": "real"},
            }
        ),
        expected_mode="full",
        created_by=None,
    )
    run_id = accepted.run_id
    job = replace(_job(run_id), payload=dict(jobs.items[0][1]))
    states = SkillEvolutionRepository(database)
    core = SkillEvolutionCore(SkillModel(), SkillStrategy(max_rounds=2))
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "repos", workspaces_root=tmp_path / "workspaces", push_enabled=True
    )
    prepare = EvolutionPrepareService(
        runs, RepositoryProbeReader(probes), manager, skill_core=core, skill_states=states
    )
    await prepare.handle(job)
    await prepare.handle(job)  # restart/replay cannot regenerate the plan
    assert len(await runs.list_rounds(run_id)) == 3
    assert await states.get(run_id) is not None
    async with database.transaction() as session:
        await session.execute(
            update(probe_reports).values(
                probe_description="MUTATED UPSTREAM DESCRIPTION",
                probe_analysis="MUTATED UPSTREAM ANALYSIS",
            )
        )

    class NoLiveProbes:
        async def list_rows_by_ids(self, *args: Any) -> Any:
            raise AssertionError("must use frozen Skill provenance instead of upstream reads")

    trace_probes = cast(Any, NoLiveProbes())
    clock = now_shanghai()
    provider = LocalEvaluation(Path(repo_url))

    def gateway(run: EvolutionRunRecord) -> HttpEvalGateway:
        return HttpEvalGateway(
            EvalGatewayConfig.from_mapping({"templateId": "validation"}),
            run_id=run.id,
            states=SkillEvolutionRepository(database),
            client=cast(Any, provider),
        )

    def now() -> datetime:
        return clock

    def services() -> tuple[
        EvolutionAdvanceService, EvolutionApplyService, EvolutionFinalizeService
    ]:
        fresh_states = SkillEvolutionRepository(database)
        candidates = EvolutionCandidateService(
            runs,
            manager,
            push_interval_seconds=0,
            skill_core=core,
            skill_states=fresh_states,
            now=now,
        )
        submitter = EvolutionEvalSubmitter(
            runs, gateway, events=None, now=now, poll_interval=timedelta(seconds=1)
        )
        return (
            EvolutionAdvanceService(
                runs, candidates, trace_probes, submitter, skill_states=fresh_states
            ),
            EvolutionApplyService(
                runs, candidates, trace_probes, now=now, skill_states=fresh_states
            ),
            EvolutionFinalizeService(
                runs,
                manager,
                gateway_for_run=gateway,
                skill_states=fresh_states,
                feedback=SkillFeedbackService(core, fresh_states),
                now=now,
                poll_interval=timedelta(seconds=1),
            ),
        )

    for _ in range(12):
        current = await runs.get_run(run_id)
        assert current is not None
        if current.status == "completed":
            break
        assert current.status == "running", current.error_message
        advance, apply, finalize = services()  # simulate a fresh worker at every boundary
        rounds = await runs.list_rounds(run_id)
        active = next(
            (r for r in rounds if r.status not in {"accepted", "rejected", "skipped"}), None
        )
        if active is not None:
            await advance.handle(replace(job, kind="evolution_advance"))
            clock += timedelta(seconds=2)
            poller = EvolutionEvalPoller(
                runs, gateway, jobs, events=None, now=now, poll_interval=timedelta(seconds=1)
            )
            await poller.run_once()
            active = await runs.get_round(active.id)
            assert active is not None and active.eval_status == "completed"
            apply_job = replace(
                job, kind="evolution_apply", payload={"evolutionRoundId": active.id}
            )
            await apply.handle(apply_job)
            await services()[1].handle(apply_job)  # replay committed decision
        else:
            await finalize.handle(replace(job, kind="evolution_finalize"))
            clock += timedelta(seconds=2)
    current = await runs.get_run(run_id)
    assert current is not None and current.status == "completed"
    clusters = await runs.list_clusters(run_id)
    assert current.progress_unit == total_units(len(clusters))
    assert current.stats["sessions"] == 3
    assert current.stats["probes"] == 3
    assert current.stats["byAttributeType"] == {"model": 1, "prompt": 1, "error": 1, "nonIssue": 0}
    # Completion and retries must keep the public final step readable.
    await runs.complete_run(run_id, finished_at=now())
    replayed = await runs.get_run(run_id)
    assert replayed is not None and replayed.progress_unit == current.progress_unit
    stage = await EvolutionStageService(runs, probes, skill_states=states).step(run_id, "final")
    assert stage is not None and stage.status == "done"
    rounds = await runs.list_rounds(run_id)
    assert [r.status for r in rounds] == ["accepted", "rejected", "skipped", "accepted"]
    state = await states.get(run_id)
    assert state is not None and state.checkpoint["stop_reason"] == "max_rounds"
    assert len(state.checkpoint["feedback_rounds"]) == 2
    assert "REGRESSION" not in state.checkpoint["best_files"]["SKILL.md"]
    assert state.checkpoint["best_files"]["SKILL.md"].count("IMPROVEMENT") == 2
    final = await runs.get_final(run_id)
    assert final is not None and final.best_score == 30.0 and final.init_score == 10.0
    assert len(provider.submissions) == 8  # three candidate pairs plus fresh final retest pair

    assert provider.templates.count("validation") == 8
    published = _git(Path(repo_url), "show", f"{current.branch_name}:skills/deep-research/SKILL.md")
    assert published.count("IMPROVEMENT") == 2 and "REGRESSION" not in published
    original = _git(Path(repo_url), "show", "v2.0.0:skills/deep-research/SKILL.md")
    assert original == "# Deep Research"

    assert "Grounded in accepted evaluation" in (rounds[-1].commit_message or "")
    assert "feedback-r2-0" in (rounds[-1].commit_message or "")
    assert "MUTATED UPSTREAM" not in (rounds[0].commit_message or "")
