"""Real Skill preparation preserves snapshots with fake model IO and local Git only."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from test_prepare_service import _job
from test_prepare_service import _run_values as legacy_run_values
from test_prepare_service import prepare_state as prepare_state

from app.db.base import Database
from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeQueryRepository
from app.db.crud.skill_evolution import SkillEvolutionRepository
from app.db.models.domain import probe_reports
from app.service.evolution.prepare import (
    EvolutionPrepareService,
    RepositoryProbeReader,
)
from app.service.evolution.skill_core import SkillEvolutionCore
from app.third_party_api.git.workspace import GitWorkspaceManager
from app.third_party_api.skill_model import AdapterError, StructuredSkillModel


def _run_values(repo_url: str, commit: str) -> dict[str, object]:
    values = legacy_run_values(repo_url, commit)
    values["eval_config"] = {"skillEvolution": "real"}
    return values


class Model:
    """Deterministic model double; no network or credentials."""

    calls = 0

    async def cluster(self, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls += 1
        return [
            {
                "cluster_id": "one",
                "attribute_type": reports[0]["attribute_type"],
                "probe_ids": [r["probe_id"] for r in reports],
                "summary": "repair",
            }
        ]

    async def verify(
        self, cluster: dict[str, Any], reports: list[dict[str, Any]], files: dict[str, str]
    ) -> dict[str, Any]:
        return {"confidence": 0.9, "severity": 0.5, "reason": "observed"}

    async def patch(
        self, cluster: dict[str, Any], reports: list[dict[str, Any]], files: dict[str, str]
    ) -> dict[str, str | None]:
        raise AssertionError("prepare must not generate a patch")

    async def attribute(self, feedback: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
        raise AssertionError("prepare must not attribute evaluation feedback")


def service_for(
    database: Database, repository: EvolutionRepository, model: Model, tmp_path: Path
) -> EvolutionPrepareService:
    return EvolutionPrepareService(
        repository,
        RepositoryProbeReader(ProbeQueryRepository(database)),
        GitWorkspaceManager(
            repos_root=tmp_path / "skill-repos", workspaces_root=tmp_path / "skill-workspaces"
        ),
        skill_core=SkillEvolutionCore(model),
        skill_states=SkillEvolutionRepository(database),
    )


async def test_skill_prepare_persists_plan_and_does_not_requery_on_retry(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    database, repository, _, repo_url, commit = prepare_state
    values = _run_values(repo_url, commit)
    values["eval_config"] = {"skillEvolution": "real"}
    await repository.create_run(values)
    model = Model()
    service = service_for(database, repository, model, tmp_path)
    result = await service.handle(_job("run-m5-09-prepare"))
    assert result["progressUnit"] == 4
    state = await SkillEvolutionRepository(database).get("run-m5-09-prepare")
    assert state is not None
    assert state.input_snapshot["base_commit"] == commit
    assert len(state.input_snapshot["reports"]) == 2
    assert len(state.checkpoint["plan"]["clusters"]) == 2
    assert len(state.checkpoint["cluster_map"]) == 2
    assert model.calls == 2
    async with database.transaction() as session:
        await session.execute(update(probe_reports).values(probe_description="changed upstream"))
    repeated = await service.handle(_job("run-m5-09-prepare"))
    assert repeated["reused"] is True
    assert model.calls == 2
    assert await SkillEvolutionRepository(database).get("run-m5-09-prepare") == state


async def test_secret_in_source_report_is_rejected_before_snapshot_write(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    database, repository, _, repo_url, commit = prepare_state
    values = _run_values(repo_url, commit)
    values["eval_config"] = {"skillEvolution": "real"}
    await repository.create_run(values)
    async with database.transaction() as session:
        await session.execute(update(probe_reports).values(probe_analysis="ghp_" + "x" * 30))
    model = Model()
    service = service_for(database, repository, model, tmp_path)
    with pytest.raises(ValueError, match="Potential secret"):
        await service.handle(_job("run-m5-09-prepare"))
    assert await SkillEvolutionRepository(database).get("run-m5-09-prepare") is None
    assert model.calls == 0


class PatchModel(Model):
    """One low-confidence group plus a no-op proposal for the selected group."""

    async def verify(
        self, cluster: dict[str, Any], reports: list[dict[str, Any]], files: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "confidence": 0.1 if reports[0]["attribute_type"] == "model" else 0.9,
            "severity": 1.0 if reports[0]["attribute_type"] == "model" else 0.0,
            "reason": "observed",
        }

    async def patch(
        self, cluster: dict[str, Any], reports: list[dict[str, Any]], files: dict[str, str]
    ) -> dict[str, str | None]:
        return {}


async def test_advance_skips_ineligible_and_rejects_noop_without_live_probe_reads(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    from app.service.evolution.advance import EvolutionAdvanceService
    from app.service.evolution.git_candidate import EvolutionCandidateService

    database, repository, _, repo_url, commit = prepare_state
    run_id = "run-m5-09-prepare"
    values = _run_values(repo_url, commit)
    values["eval_config"] = {"skillEvolution": "real"}
    await repository.create_run(values)
    model = PatchModel()
    await service_for(database, repository, model, tmp_path).handle(_job(run_id))
    states = SkillEvolutionRepository(database)
    core = SkillEvolutionCore(model)
    manager = GitWorkspaceManager(
        repos_root=tmp_path / "skill-repos",
        workspaces_root=tmp_path / "skill-workspaces",
    )
    candidates = EvolutionCandidateService(
        repository, manager, skill_core=core, skill_states=states
    )

    class NoLiveProbes:
        async def list_rows_by_ids(self, probe_ids: Any) -> Any:
            raise AssertionError("must use frozen source reports")

    service = EvolutionAdvanceService(repository, candidates, NoLiveProbes(), skill_states=states)
    result = await service.handle(_job(run_id))
    assert result["status"] == "rejected"
    rounds = await repository.list_rounds(run_id)
    assert all(item.status in {"skipped", "rejected"} for item in rounds)
    assert all(item.eval_status == "skipped" for item in rounds)
    rejected = next(item for item in rounds if item.status == "rejected")
    assert rejected.eval_status == "skipped"
    assert rejected.candidate_ref is None
    assert rejected.eval_error == "noop_patch"


async def test_interrupted_prepare_pins_original_commit_after_branch_moves(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    from test_prepare_service import _git

    database, repository, _, repo_url, commit = prepare_state
    values = _run_values(repo_url, commit)
    values["target_version"] = "main"
    # Probe selection retains the semantic version in the job payload.
    await repository.create_run(values)

    class Interrupted(Model):
        fail = True

        async def cluster(self, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if self.fail:
                self.fail = False
                raise RuntimeError("simulated model outage after snapshot")
            return await super().cluster(reports)

    model = Interrupted()
    service = service_for(database, repository, model, tmp_path)
    with pytest.raises(RuntimeError, match="outage"):
        await service.handle(_job("run-m5-09-prepare"))
    seed = tmp_path / "seed"
    (seed / "skills/deep-research/SKILL.md").write_text("# Upstream changed\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "upstream changed")
    _git(seed, "push", "origin", "HEAD:refs/heads/main")
    await service.handle(_job("run-m5-09-prepare"))
    run = await repository.get_run("run-m5-09-prepare")
    assert run is not None
    assert run.base_commit == commit
    state = await SkillEvolutionRepository(database).get(run.id)
    assert state is not None
    assert state.input_snapshot["base_commit"] == commit
    assert state.input_snapshot["files"]["SKILL.md"] == "# Deep Research\n"


async def test_existing_real_run_cannot_resume_with_placeholder_model(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    database, repository, _, repo_url, commit = prepare_state
    run_id = "run-m5-09-prepare"
    values = _run_values(repo_url, commit)
    values["eval_config"] = {"skillEvolution": "real"}
    await repository.create_run(values)
    await service_for(database, repository, Model(), tmp_path).handle(_job(run_id))
    disabled = EvolutionPrepareService(
        repository,
        RepositoryProbeReader(ProbeQueryRepository(database)),
        GitWorkspaceManager(
            repos_root=tmp_path / "unused", workspaces_root=tmp_path / "unused-work"
        ),
        skill_states=SkillEvolutionRepository(database),
    )
    with pytest.raises(ValueError, match="refusing placeholder fallback"):
        await disabled.handle(_job(run_id))


@pytest.mark.parametrize("recovers", [True, False])
async def test_cluster_repair_commits_only_a_complete_valid_plan(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
    recovers: bool,
) -> None:
    class RepairModel(Model, StructuredSkillModel):
        verified = 0

        async def cluster(self, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return await StructuredSkillModel.cluster(self, reports)

        async def _generate(self, operation: str, data: dict[str, Any]) -> Any:
            assert operation == "cluster"
            self.calls += 1
            reports = data["reports"]
            ids = [r["probe_id"] for r in reports]
            if self.calls == 1 or not recovers:
                ids.append(ids[0])
            else:
                # The second attribute group starts without previous feedback.
                assert self.calls != 2 or "validation_feedback" in data
            return [
                {
                    "cluster_id": "one",
                    "attribute_type": reports[0]["attribute_type"],
                    "probe_ids": ids,
                    "summary": "Synthetic issue",
                }
            ]

        async def verify(
            self,
            cluster: dict[str, Any],
            reports: list[dict[str, Any]],
            files: dict[str, str],
        ) -> dict[str, Any]:
            self.verified += 1
            return await super().verify(cluster, reports, files)

    database, repository, _, repo_url, commit = prepare_state
    run_id = "run-m5-09-prepare"
    await repository.create_run(_run_values(repo_url, commit))
    model = RepairModel()
    service = service_for(database, repository, model, tmp_path)
    if recovers:
        assert (await service.handle(_job(run_id)))["progressUnit"] == 4
    else:
        with pytest.raises(AdapterError, match=r"after 3 attempts.*duplicate_membership"):
            await service.handle(_job(run_id))
    state = await SkillEvolutionRepository(database).get(run_id)
    assert state is not None and len(state.input_snapshot["reports"]) == 2
    assert ("plan" in state.checkpoint) is recovers
    assert len(await repository.list_clusters(run_id)) == (2 if recovers else 0)
    assert len(await repository.list_rounds(run_id)) == (2 if recovers else 0)
    assert model.calls == 3
    assert model.verified == (2 if recovers else 0)
    run = await repository.get_run(run_id)
    assert run is not None and run.progress_unit == (4 if recovers else 0)


async def test_verify_failure_preserves_visible_partition(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    from sqlalchemy import insert, select

    from app.db.models.domain import bad_case_analysis, bad_cases
    from app.service.evolution.stages import EvolutionStageService, EvolutionStepClusters

    database, repo, _, url, commit = prepare_state
    rid = "run-m5-09-prepare"
    await repo.create_run(_run_values(url, commit))
    states = SkillEvolutionRepository(database)
    query = ProbeQueryRepository(database)
    stages = EvolutionStageService(repo, query, skill_states=states)
    async with database.transaction() as session:
        for table, changes in (
            (probe_reports, {"probe_id": "third", "attribute_type": "prompt"}),
            (bad_cases, {"bad_case_id": "case-3"}),
            (bad_case_analysis, {"probe_id": "third", "bad_case_id": "case-3"}),
        ):
            row = dict((await session.execute(select(table).limit(1))).mappings().one())
            row.pop("id", None)
            await session.execute(insert(table).values({**row, **changes}))

    class FailSecond(Model):
        async def verify(
            self, cluster: dict[str, Any], reports: list[dict[str, Any]], files: dict[str, str]
        ) -> dict[str, Any]:
            if cluster["attribute_type"] == "model":
                live = await stages.step(rid, "clusters")
                assert isinstance(live, EvolutionStepClusters)
                assert [c.review_status for c in live.payload.clusters] == [
                    "verified",
                    "verifying",
                    "pending",
                ]
                raise AdapterError("CodeAgent timed out")
            return await super().verify(cluster, reports, files)

    service = EvolutionPrepareService(
        repo,
        RepositoryProbeReader(query),
        GitWorkspaceManager(repos_root=tmp_path / "repos", workspaces_root=tmp_path / "ws"),
        skill_core=SkillEvolutionCore(FailSecond()),
        skill_states=states,
    )
    with pytest.raises(AdapterError, match="CodeAgent timed out"):
        await service.handle(_job(rid))
    await repo.mark_failed(rid, stage="prepare", message="CodeAgent timed out")
    page = await stages.step(rid, "clusters")
    assert isinstance(page, EvolutionStepClusters)
    assert page.clickable
    assert [(c.type, c.review_status, c.review_error) for c in page.payload.clusters] == [
        ("error", "verified", None),
        ("model", "failed", "CodeAgent timed out"),
        ("prompt", "pending", None),
    ]
    assert all(c.probe_count == len(c.probes) == 1 for c in page.payload.clusters)
    assert not await repo.list_rounds(rid)


async def test_resume_reuses_partition_ids_and_verified_reviews(
    prepare_state: tuple[Database, EvolutionRepository, EvolutionPrepareService, str, str],
    tmp_path: Path,
) -> None:
    import asyncio

    class InterruptOnce(Model):
        seen: list[str]

        async def verify(
            self, cluster: dict[str, Any], reports: list[dict[str, Any]], files: dict[str, str]
        ) -> dict[str, Any]:
            self.seen.append(cluster["cluster_id"])
            if len(self.seen) == 2:
                raise asyncio.CancelledError()
            return await super().verify(cluster, reports, files)

    model = InterruptOnce()
    model.seen = []
    db, repo, _, url, commit = prepare_state
    rid = "run-m5-09-prepare"
    await repo.create_run(_run_values(url, commit))
    service = service_for(db, repo, model, tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await service.handle(_job(rid))
    before = await repo.list_clusters(rid)
    await service.handle(_job(rid))
    after = await repo.list_clusters(rid)
    assert [c.probe_cluster_id for c in before] == [c.probe_cluster_id for c in after]
    assert model.calls == 2
    assert model.seen == ["error:one", "model:one", "model:one"]
