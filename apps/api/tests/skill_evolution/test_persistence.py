"""Skill snapshots survive source changes and checkpoints survive interrupted work."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, inspect

from app.config import DatabaseDialect, Settings
from app.db.base import Database
from app.db.crud.evolution import EvolutionRepository
from app.db.crud.skill_evolution import SkillCheckpointConflict, SkillEvolutionRepository
from app.db.factory import create_database
from app.db.migrate import upgrade_database
from app.db.models.evolution import evolution_runs


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """Migrate an isolated SQLite database, never a configured live database."""
    db = create_database(Settings(db_dialect=DatabaseDialect.SQLITE, data_dir=tmp_path))
    await db.start()
    await upgrade_database(db)
    try:
        yield db
    finally:
        await db.dispose()


async def make_run(database: Database, run_id: str = "skill-run", kind: str = "skill") -> None:
    """Create the minimal existing run without requiring mutable source report rows."""
    await EvolutionRepository(database).create_run(
        {
            "id": run_id,
            "title": "Skill",
            "mode": "full",
            "engine_key": "full",
            "eval_config": {},
            "target_config_id": None,
            "target_type": kind,
            "target_name": "sample",
            "target_version": "v1",
            "repo_url": "local",
            "sub_dir": "sample",
            "created_by": None,
        }
    )


async def test_snapshot_is_immutable_and_retry_preserves_checkpoint(database: Database) -> None:
    await make_run(database)
    repository = SkillEvolutionRepository(database)
    snapshot = {
        "reports": [{"probe_id": "deleted-upstream", "description": "original"}],
        "files": {"SKILL.md": "original"},
        "base_commit": "a" * 40,
    }
    initial = await repository.initialize("skill-run", snapshot)
    snapshot["files"]["SKILL.md"] = "changed"  # type: ignore[index]
    assert initial.input_snapshot["files"]["SKILL.md"] == "original"
    updated = await repository.save_checkpoint(
        "skill-run",
        {"plan": {"aliases": {"p2": "p1"}}},
        expected_revision=0,
    )
    assert updated.revision == 1
    retried = await repository.initialize("skill-run", initial.input_snapshot)
    assert retried.checkpoint == updated.checkpoint
    with pytest.raises(ValueError, match="immutable"):
        await repository.initialize("skill-run", snapshot)
    loaded = await SkillEvolutionRepository(database).get("skill-run")
    assert loaded is not None
    loaded.input_snapshot["files"]["SKILL.md"] = "mutated returned copy"
    reloaded = await repository.get("skill-run")
    assert reloaded is not None
    assert reloaded.input_snapshot["files"]["SKILL.md"] == "original"


async def test_checkpoint_cas_and_invalid_payload_preserve_last_completed_stage(
    database: Database,
) -> None:
    await make_run(database)
    repository = SkillEvolutionRepository(database)
    await repository.initialize("skill-run", {"reports": []})
    state = await repository.save_checkpoint(
        "skill-run", {"plan": {"clusters": []}}, expected_revision=0
    )
    with pytest.raises(SkillCheckpointConflict):
        await repository.save_checkpoint("skill-run", {"lost": True}, expected_revision=0)
    with pytest.raises(ValueError):
        await repository.save_checkpoint("skill-run", {"score": float("nan")}, expected_revision=1)
    with pytest.raises(TypeError):
        await repository.save_checkpoint("skill-run", {"secret": object()}, expected_revision=1)
    loaded = await repository.get("skill-run")
    assert loaded is not None
    assert loaded.checkpoint == state.checkpoint
    assert loaded.revision == 1
    with pytest.raises(SkillCheckpointConflict):
        await repository.save_checkpoint("missing", {}, expected_revision=0)
    assert await repository.get("missing") is None


async def test_only_skill_runs_and_delete_cascade(database: Database) -> None:
    repository = SkillEvolutionRepository(database)
    with pytest.raises(ValueError, match="existing Skill"):
        await repository.initialize("missing", {})
    await make_run(database, "agent-run", "agent")
    with pytest.raises(ValueError, match="existing Skill"):
        await repository.initialize("agent-run", {})
    await make_run(database)
    await repository.initialize("skill-run", {})
    async with database.transaction() as session:
        await session.execute(delete(evolution_runs).where(evolution_runs.c.id == "skill-run"))
    assert await repository.get("skill-run") is None


async def test_additive_schema_and_mysql_ddl(database: Database) -> None:
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    from app.db.models.skill_evolution import skill_evolution_state

    async with database.engine.connect() as connection:
        names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("skill_evolution_state"),
        )
    assert "probe_reports" in names
    assert "evolution_rounds" in names
    assert {column["name"] for column in columns} == {
        "run_id",
        "input_snapshot",
        "checkpoint",
        "revision",
        "created_at",
        "updated_at",
    }
    ddl = str(CreateTable(skill_evolution_state).compile(dialect=mysql.dialect()))
    assert "JSON NOT NULL" in ddl
    assert "ON DELETE CASCADE" in ddl


async def test_upgrade_does_not_backfill_or_rewrite_historical_runs(tmp_path: Path) -> None:
    import asyncio

    from sqlalchemy import func, select

    from alembic import command
    from app.db.migrate import _alembic_config
    from app.db.models.skill_evolution import skill_evolution_state

    database = create_database(Settings(db_dialect=DatabaseDialect.SQLITE, data_dir=tmp_path))
    await database.start()
    try:
        config = _alembic_config(database)
        config.attributes["bootstrap_external_tables"] = True
        await asyncio.to_thread(command.upgrade, config, "0012")
        await make_run(database, "historical-run")
        before = await EvolutionRepository(database).get_run("historical-run")
        await upgrade_database(database)
        after = await EvolutionRepository(database).get_run("historical-run")
        assert after == before
        async with database.session() as session:
            assert (
                await session.execute(
                    select(func.count()).select_from(skill_evolution_state),
                )
            ).scalar_one() == 0
    finally:
        await database.dispose()


async def test_invalid_patch_rejection_advances_once_without_evaluation(database: Database) -> None:
    from sqlalchemy import insert, update

    from app.db.models.evolution import evolution_rounds, probe_clusters
    from app.service.evolution.state import StageConflict
    from app.utils.time import now_shanghai

    await make_run(database)
    now = now_shanghai()
    async with database.transaction() as session:
        await session.execute(update(evolution_runs).values(progress_unit=4, status="running"))
        await session.execute(
            insert(probe_clusters).values(
                probe_cluster_id="cluster",
                run_id="skill-run",
                seq=1,
                probe_cluster_description="repair",
                probe_cluster_type="model",
                created_at=now,
            )
        )
        await session.execute(
            insert(evolution_rounds).values(
                id="round",
                run_id="skill-run",
                probe_cluster_id="cluster",
                seq=1,
                status="patching",
            )
        )
    repository = EvolutionRepository(database)
    with pytest.raises(ValueError, match="safe reason"):
        await repository.reject_invalid_patch(
            "round", reason="raw provider message", finished_at=now
        )
    rejected = await repository.reject_invalid_patch(
        "round", reason="invalid_patch", finished_at=now
    )
    assert rejected.status == "rejected"
    assert rejected.eval_status == "skipped"
    assert rejected.eval_external_id is None
    assert rejected.accepted is False
    assert rejected.eval_error == "invalid_patch"
    with pytest.raises(StageConflict):
        await repository.reject_invalid_patch("round", reason="invalid_patch", finished_at=now)
    run = await repository.get_run("skill-run")
    assert run is not None and run.progress_unit == 5


@pytest.mark.parametrize("disposition", ["selected", "low_confidence"])
async def test_feedback_append_is_atomic_and_does_not_write_external_probes(
    database: Database,
    disposition: str,
) -> None:
    from sqlalchemy import func, insert, select, update

    from app.db.models.domain import probe_reports
    from app.db.models.evolution import evolution_rounds, probe_cluster_probes, probe_clusters
    from app.utils.time import now_shanghai

    await make_run(database)
    now = now_shanghai()
    async with database.transaction() as session:
        await session.execute(update(evolution_runs).values(progress_unit=5, status="running"))
        await session.execute(
            insert(probe_clusters).values(
                probe_cluster_id="original",
                run_id="skill-run",
                seq=1,
                probe_cluster_description="original issue",
                probe_cluster_type="model",
                created_at=now,
            )
        )
        await session.execute(
            insert(evolution_rounds).values(
                id="original-round",
                run_id="skill-run",
                probe_cluster_id="original",
                seq=1,
                status="accepted",
            )
        )
    repository = SkillEvolutionRepository(database)
    await repository.initialize("skill-run", {"reports": [{"probe_id": "original"}]})
    cluster = {
        "cluster_id": "r2:feedback",
        "attribute_type": "error",
        "disposition": disposition,
        "probe_cluster_description": "new observed failure",
        "source_probe_ids": ["feedback:1"],
        "critic": {"confidence": 0.9},
        "priority": 0.7,
    }
    plan = {"clusters": [cluster], "canonical_reports": [{"probe_id": "feedback:1"}], "aliases": {}}
    checkpoint = {
        "cluster_map": {"original": {}},
        "feedback_rounds": [{"end_seq": 2}],
        "source_reports": [{"probe_id": "feedback:1", "evidence": "case-a"}],
    }
    with pytest.raises(SkillCheckpointConflict):
        await repository.append_feedback_plan("skill-run", plan, checkpoint, expected_revision=9)
    assert len(await EvolutionRepository(database).list_clusters("skill-run")) == 1
    state = await repository.append_feedback_plan(
        "skill-run", plan, checkpoint, expected_revision=0
    )
    assert state.revision == 1
    assert len(state.checkpoint["cluster_map"]) == 2
    assert state.checkpoint["source_reports"][0]["evidence"] == "case-a"
    assert state.input_snapshot == {"reports": [{"probe_id": "original"}]}
    run = await EvolutionRepository(database).get_run("skill-run")
    assert run is not None and run.progress_unit == 5 + (disposition != "selected")
    assert run.stats["clusters"] == 2
    rounds = await EvolutionRepository(database).list_rounds("skill-run")
    assert [r.seq for r in rounds] == [1, 2]
    assert rounds[-1].status == ("pending" if disposition == "selected" else "skipped")
    assert rounds[-1].eval_status == ("pending" if disposition == "selected" else "skipped")
    async with database.session() as session:
        for table in (probe_reports, probe_cluster_probes):
            assert (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one() == 0
    if disposition == "selected":
        with pytest.raises(SkillCheckpointConflict, match="completed"):
            await repository.append_feedback_plan(
                "skill-run", plan, checkpoint, expected_revision=1
            )
    else:
        # A complete skipped wave extends total and completed units exactly once.
        next_state = await repository.append_feedback_plan(
            "skill-run",
            plan,
            state.checkpoint,
            expected_revision=1,
        )
        assert next_state.revision == 2
        run = await EvolutionRepository(database).get_run("skill-run")
        assert run is not None and run.progress_unit == 7 and run.stats["clusters"] == 3
        with pytest.raises(SkillCheckpointConflict):
            await repository.append_feedback_plan(
                "skill-run",
                plan,
                state.checkpoint,
                expected_revision=1,
            )
        assert len(await EvolutionRepository(database).list_clusters("skill-run")) == 3
        assert await EvolutionRepository(database).get_run("skill-run") == run


@pytest.mark.parametrize(
    "plan,checkpoint",
    [
        ({}, {}),
        ({"clusters": []}, {}),
        ({"clusters": [{}]}, {"cluster_map": []}),
    ],
)
async def test_feedback_append_rejects_malformed_plan(
    database: Database,
    plan: dict[str, object],
    checkpoint: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        await SkillEvolutionRepository(database).append_feedback_plan(
            "missing",
            plan,
            checkpoint,
            expected_revision=0,
        )


async def test_feedback_append_requires_active_skill_parent(database: Database) -> None:
    with pytest.raises(SkillCheckpointConflict, match="active full Skill"):
        await SkillEvolutionRepository(database).append_feedback_plan(
            "missing",
            {"clusters": [{}]},
            {},
            expected_revision=0,
        )


async def test_accept_atomically_updates_best_files_and_revision(database: Database) -> None:
    from sqlalchemy import insert, update

    from app.db.models.evolution import evolution_rounds, probe_clusters
    from app.service.evolution.state import StageConflict
    from app.utils.time import now_shanghai

    await make_run(database)
    now = now_shanghai()
    async with database.transaction() as session:
        await session.execute(update(evolution_runs).values(progress_unit=4, status="running"))
        await session.execute(
            insert(probe_clusters).values(
                probe_cluster_id="accept-cluster",
                run_id="skill-run",
                seq=1,
                probe_cluster_description="repair",
                probe_cluster_type="model",
                created_at=now,
            )
        )
        await session.execute(
            insert(evolution_rounds).values(
                id="accept-round",
                run_id="skill-run",
                probe_cluster_id="accept-cluster",
                seq=1,
                status="deciding",
                patch={"files": {"SKILL.md": "accepted", "new.txt": "keep me"}},
            )
        )
    states = SkillEvolutionRepository(database)
    await states.initialize("skill-run", {"files": {"SKILL.md": "initial"}})
    before = await states.save_checkpoint(
        "skill-run",
        {
            "best_files": {"SKILL.md": "initial"},
            "evaluations": {"pair": {"result": "retain"}},
        },
        expected_revision=0,
    )
    repository = EvolutionRepository(database)
    accepted = await repository.accept_round(
        "accept-round",
        final_commit="a" * 40,
        final_message="accepted",
        finished_at=now,
    )
    state = await SkillEvolutionRepository(database).get("skill-run")
    assert state is not None
    assert state.checkpoint["best_files"] == accepted.patch["files"]  # type: ignore[index]
    assert state.checkpoint["evaluations"] == before.checkpoint["evaluations"]
    assert state.revision == before.revision + 1
    assert state.input_snapshot["files"]["SKILL.md"] == "initial"
    with pytest.raises(SkillCheckpointConflict):
        await states.save_checkpoint(
            "skill-run", before.checkpoint, expected_revision=before.revision
        )
    with pytest.raises(StageConflict):
        await repository.accept_round(
            "accept-round",
            final_commit="a" * 40,
            final_message="accepted",
            finished_at=now,
        )
    assert await states.get("skill-run") == state


async def test_initial_skipped_accounting_is_absolute_and_verified(database: Database) -> None:
    from sqlalchemy import insert, update

    from app.db.models.evolution import evolution_rounds, probe_clusters
    from app.utils.time import now_shanghai

    await make_run(database)
    async with database.transaction() as session:
        await session.execute(update(evolution_runs).values(progress_unit=4))
        await session.execute(
            insert(probe_clusters).values(
                probe_cluster_id="skip",
                run_id="skill-run",
                seq=1,
                probe_cluster_description="skip",
                probe_cluster_type="model",
                created_at=now_shanghai(),
            )
        )
        await session.execute(
            insert(evolution_rounds).values(
                id="skip-round",
                run_id="skill-run",
                probe_cluster_id="skip",
                seq=1,
                status="skipped",
            )
        )
    repository = EvolutionRepository(database)
    kwargs = dict(
        status="running",
        progress_unit=4,
        repo_path=None,
        base_ref=None,
        base_commit=None,
        branch_name=None,
        workspace_path=None,
        stats={"clusters": 1},
    )
    with pytest.raises(ValueError, match="differs"):
        await repository.mark_prepared("skill-run", **kwargs)  # type: ignore[arg-type]
    first = await repository.mark_prepared("skill-run", skipped_rounds=1, **kwargs)  # type: ignore[arg-type]
    second = await repository.mark_prepared("skill-run", skipped_rounds=1, **kwargs)  # type: ignore[arg-type]
    assert first.progress_unit == second.progress_unit == 5
