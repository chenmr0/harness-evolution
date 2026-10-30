"""Database lifecycle, migration, and cross-dialect repository contracts."""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import QueuePool

from alembic import command
from app.config import DatabaseDialect, Settings
from app.db.base import Database
from app.db.crud.bad_case_catalog_sync import (
    BadCaseCatalogSyncRepository,
    CatalogCaseRow,
)
from app.db.crud.bad_cases import BadCaseRepository
from app.db.crud.evolution import EvolutionRepository
from app.db.crud.evolution_targets import (
    EvolutionTargetConflict,
    EvolutionTargetRepository,
    ModelUsageRecord,
)
from app.db.crud.problems import ProblemRepository
from app.db.crud.sessions import SessionRepository
from app.db.crud.sync import SyncLockConflict, SyncLockRepository
from app.db.database import SqlAlchemyDatabase
from app.db.factory import create_database
from app.db.migrate import upgrade_database
from app.db.models.domain import (
    agent_events,
    agents,
    bad_case_analysis,
    bad_case_catalog_sync,
    bad_cases,
    evolution_targets,
    probe_reports,
    session_families,
    source_records,
    trajectory_payloads,
    user_turns,
)
from app.db.models.evolution import (
    ClusterWrite,
    evolution_finals,
    evolution_rounds,
    evolution_runs,
    probe_cluster_probes,
    probe_clusters,
)
from app.db.models.jobs import job_runs
from app.db.models.users import users
from app.db.mysql import MySqlDatabase
from app.db.sqlite import SqliteDatabase
from app.service.acquisition.replay import replay_pending_payloads
from app.service.bad_cases.service import BadCaseService
from app.service.problems.service import ProblemService


class FailingHealthDatabase(SqlAlchemyDatabase):
    """Database double whose health check raises a synthetic driver error."""

    def __init__(self, error: str) -> None:
        """Keep the sensitive driver text available only to the raised exception."""
        super().__init__(dialect="mysql", url="mysql+asyncmy://redacted", echo=False)
        self._error = error

    @property
    def engine(self) -> AsyncEngine:
        """Raise the synthetic driver failure during the health probe."""
        raise RuntimeError(self._error)


def sqlite_settings(tmp_path: Path) -> Settings:
    """Build isolated SQLite settings for one test."""
    return Settings(
        db_dialect=DatabaseDialect.SQLITE,
        data_dir=tmp_path,
        db_pool_size=2,
        db_max_overflow=1,
    )


def alembic_config(database: Database) -> Config:
    """Build an Alembic config for explicit revision-path migration tests."""
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database.url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


def normalized_server_default(column: Mapping[str, object]) -> str | None:
    """Normalize dialect-specific quoting around a reflected server default."""
    default = column["default"]
    if default is None:
        return None
    return str(default).strip().strip("'\"").lower()


def assert_true_server_default(column: Mapping[str, object]) -> None:
    """Assert one reflected column has a truthy boolean server default."""
    default = normalized_server_default(column)
    assert default is not None
    assert default in {"1", "true"}


async def reflected_event_schema(
    connection: AsyncConnection,
) -> tuple[
    set[str],
    set[str],
    set[tuple[str, ...]],
    set[tuple[tuple[str, ...], str, str | None]],
]:
    """Inspect the shared event projection through a real dialect connection."""
    columns, indexes, uniques, foreign_keys = await connection.run_sync(
        lambda sync_connection: (
            {
                column["name"]
                for column in inspect(sync_connection).get_columns("evolution_run_events")
            },
            {
                index["name"]
                for index in inspect(sync_connection).get_indexes("evolution_run_events")
                if index["name"] is not None
            },
            {
                tuple(constraint["column_names"])
                for constraint in inspect(sync_connection).get_unique_constraints(
                    "evolution_run_events",
                )
            },
            {
                (
                    tuple(str(column) for column in foreign_key["constrained_columns"]),
                    str(foreign_key["referred_table"]),
                    cast(
                        str | None,
                        foreign_key.get("options", {}).get("ondelete"),
                    ),
                )
                for foreign_key in inspect(sync_connection).get_foreign_keys(
                    "evolution_run_events",
                )
            },
        ),
    )
    return columns, indexes, uniques, foreign_keys


class EvolutionTableSchema(TypedDict):
    """Reflected evolution table details used by the MySQL CI lane."""

    columns: dict[str, Mapping[str, object]]
    indexes: set[str]
    foreign_keys: set[tuple[tuple[str, ...], str]]
    foreign_key_rules: set[tuple[tuple[str, ...], str, str | None]]


async def reflected_evolution_schema(
    connection: AsyncConnection,
) -> dict[str, EvolutionTableSchema]:
    """Inspect the five service-owned evolution tables through the real dialect."""
    return cast(
        dict[str, EvolutionTableSchema],
        await connection.run_sync(
            lambda sync_connection: {
                table: {
                    "columns": {
                        str(column["name"]): column
                        for column in inspect(sync_connection).get_columns(table)
                    },
                    "indexes": {
                        str(index["name"])
                        for index in inspect(sync_connection).get_indexes(table)
                        if index["name"] is not None
                    },
                    "foreign_keys": {
                        (
                            tuple(str(column) for column in foreign_key["constrained_columns"]),
                            str(foreign_key["referred_table"]),
                        )
                        for foreign_key in inspect(sync_connection).get_foreign_keys(table)
                    },
                    "foreign_key_rules": {
                        (
                            tuple(str(column) for column in foreign_key["constrained_columns"]),
                            str(foreign_key["referred_table"]),
                            cast(
                                str | None,
                                foreign_key.get("options", {}).get("ondelete"),
                            ),
                        )
                        for foreign_key in inspect(sync_connection).get_foreign_keys(table)
                    },
                }
                for table in (
                    "evolution_runs",
                    "probe_clusters",
                    "probe_cluster_probes",
                    "evolution_rounds",
                    "evolution_finals",
                )
            },
        ),
    )


def assert_evolution_schema(schema: dict[str, EvolutionTableSchema]) -> None:
    """Assert the cross-dialect evolution columns, indexes, and foreign keys."""
    assert "progress_unit" in schema["evolution_runs"]["columns"]
    assert {
        "ix_evolution_runs_created_at",
        "ix_evolution_runs_status_next_retry",
        "ix_evolution_runs_target_config",
    } <= schema["evolution_runs"]["indexes"]
    assert (
        ("target_config_id",),
        "evolution_targets",
    ) in schema["evolution_runs"]["foreign_keys"]
    assert schema["evolution_runs"]["columns"]["target_config_id"]["nullable"] is True
    assert (
        ("target_config_id",),
        "evolution_targets",
        "SET NULL",
    ) in schema["evolution_runs"]["foreign_key_rules"]
    assert schema["probe_clusters"]["columns"]["impact"]["nullable"] is True
    assert {
        "ix_probe_clusters_run",
        "ix_probe_clusters_run_rank",
    } <= schema["probe_clusters"]["indexes"]
    assert (
        ("run_id",),
        "evolution_runs",
    ) in schema["probe_clusters"]["foreign_keys"]
    assert (
        ("probe_id",),
        "probe_reports",
    ) in schema["probe_cluster_probes"]["foreign_keys"]
    assert {
        "ix_evolution_rounds_cluster",
        "ix_evolution_rounds_run_status",
        "ix_evolution_rounds_eval_poll",
    } <= schema["evolution_rounds"]["indexes"]
    assert (
        ("probe_cluster_id",),
        "probe_clusters",
    ) in schema["evolution_rounds"]["foreign_keys"]
    assert (
        ("run_id",),
        "evolution_runs",
    ) in schema["evolution_finals"]["foreign_keys"]


async def job_recency_index_directions(
    database: Database,
) -> list[tuple[str, bool]]:
    """Return ordered column directions for the kind/recency index."""
    index_name = "ix_job_runs_kind_created_at"
    async with database.session() as session:
        if database.dialect == "sqlite":
            sqlite_rows = (
                await session.execute(text(f"PRAGMA index_xinfo('{index_name}')"))
            ).mappings()
            return [
                (str(row["name"]), bool(row["desc"]))
                for row in sqlite_rows
                if row["name"] is not None and row["key"]
            ]
        mysql_rows = (
            await session.execute(
                text(
                    "SELECT column_name, collation "
                    "FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = 'job_runs' "
                    "AND index_name = :index_name "
                    "ORDER BY seq_in_index",
                ),
                {"index_name": index_name},
            )
        ).all()
        return [(str(row[0]), str(row[1]).upper() == "D") for row in mysql_rows]


@pytest.mark.parametrize(
    ("reflected", "expected"),
    [
        (1, "1"),
        ("'1'", "1"),
        ('"1"', "1"),
        ("true", "true"),
        (None, None),
    ],
)
def test_normalized_server_default_accepts_truthy_dialect_forms(
    reflected: object,
    expected: str | None,
) -> None:
    """SQLite and MySQL reflection may quote the same DEFAULT 1 constraint."""
    assert normalized_server_default({"default": reflected}) == expected


def test_settings_derive_database_urls(tmp_path: Path) -> None:
    """Each dialect has an explicit, credential-free default URL."""
    sqlite = Settings(db_dialect=DatabaseDialect.SQLITE, data_dir=tmp_path)
    mysql = Settings(db_dialect=DatabaseDialect.MYSQL)

    assert sqlite.effective_db_url == f"sqlite+aiosqlite:///{tmp_path / 'harness.db'}"
    assert mysql.effective_db_url.startswith("mysql+asyncmy://")
    assert "password" not in mysql.effective_db_url.lower()


def test_database_factory_selects_both_adapters(tmp_path: Path) -> None:
    """The factory is the only dialect selection point."""
    sqlite = create_database(sqlite_settings(tmp_path))
    mysql = create_database(Settings(db_dialect=DatabaseDialect.MYSQL))

    assert isinstance(sqlite, SqliteDatabase)
    assert isinstance(mysql, MySqlDatabase)


def test_job_index_declares_descending_recency() -> None:
    """The kind/recency index encodes newest-job ordering without table scans."""
    index = next(item for item in job_runs.indexes if item.name == "ix_job_runs_kind_created_at")

    assert [str(expression) for expression in index.expressions] == [
        "job_runs.kind",
        "job_runs.created_at DESC",
    ]


def test_sqlite_adapter_creates_the_default_data_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented ``./data`` default is usable before the first migration."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(db_dialect=DatabaseDialect.SQLITE)

    SqliteDatabase(settings)

    assert (tmp_path / "data").is_dir()


async def test_sqlite_health_and_transaction_rollback(tmp_path: Path) -> None:
    """A rolled-back transaction leaves no residue and health reports the dialect."""
    database = SqliteDatabase(sqlite_settings(tmp_path))
    await database.start()
    metadata = MetaData()
    probe = Table(
        "transaction_probes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(64), nullable=False),
    )
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        with pytest.raises(RuntimeError, match="rollback"):
            async with database.transaction() as session:
                await session.execute(insert(probe).values(id=1, name="discard"))
                raise RuntimeError("rollback")

        async with database.session() as session:
            rows = (await session.execute(select(probe))).all()
        health = await database.health()

        assert rows == []
        assert health.ok is True
        assert health.dialect == "sqlite"
        assert health.latency_ms >= 0
    finally:
        await database.dispose()


async def test_sqlite_connection_applies_design_pragmas(tmp_path: Path) -> None:
    """The SQLite adapter applies the concurrency and integrity contract."""
    database = SqliteDatabase(sqlite_settings(tmp_path))
    await database.start()
    try:
        async with database.session() as session:
            pragmas: dict[str, object] = {
                "journal_mode": (await session.execute(text("PRAGMA journal_mode"))).scalar_one(),
                "foreign_keys": (await session.execute(text("PRAGMA foreign_keys"))).scalar_one(),
                "busy_timeout": (await session.execute(text("PRAGMA busy_timeout"))).scalar_one(),
                "synchronous": (await session.execute(text("PRAGMA synchronous"))).scalar_one(),
            }
    finally:
        await database.dispose()

    assert str(pragmas["journal_mode"]).lower() == "wal"
    assert pragmas["foreign_keys"] == 1
    assert pragmas["busy_timeout"] == 5000
    assert pragmas["synchronous"] == 1


def test_fixture_migration_is_frozen_from_mutable_model_metadata() -> None:
    """Historical 0004 must not execute whichever tables the current model holds."""
    migration = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0004_domain_fixtures.py"
    ).read_text(encoding="utf-8")

    assert "DOMAIN_METADATA" not in migration
    assert "op.create_table(" in migration


async def test_sqlite_health_failure_is_sanitized(tmp_path: Path) -> None:
    """Real connection failures expose only the stable unavailable summary."""
    database = SqliteDatabase(
        Settings(
            db_dialect=DatabaseDialect.SQLITE,
            db_url=f"sqlite+aiosqlite:///{tmp_path / 'missing' / 'db.sqlite'}",
        )
    )
    await database.start()
    try:
        health = await database.health()
    finally:
        await database.dispose()

    assert health.ok is False
    assert health.dialect == "sqlite"
    assert health.error == "OperationalError: database is unavailable"


async def test_health_failure_logs_stable_diagnostics_without_driver_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Logs and public health both exclude sensitive driver details."""
    driver_error = (
        "mysql+asyncmy://ops:super-secret@db.internal:3306/private"
        "?charset=utf8mb4 failed at /var/lib/mysql/private"
    )
    database = FailingHealthDatabase(driver_error)

    with caplog.at_level(logging.ERROR, logger="app.db.database"):
        health = await database.health()

    assert health.ok is False
    assert health.error == "RuntimeError: database is unavailable"
    assert "super-secret" not in health.error
    assert "db.internal" not in health.error
    assert "/var/lib/mysql/private" not in health.error
    assert driver_error not in caplog.text
    assert "super-secret" not in caplog.text
    assert "db.internal" not in caplog.text
    assert "/var/lib/mysql/private" not in caplog.text
    record = next(record for record in caplog.records if record.name == "app.db.database")
    assert record.exc_info is None
    assert getattr(record, "error_type", None) == "RuntimeError"


async def test_mysql_engine_uses_the_designed_pool_configuration() -> None:
    """MySQL configuration is applied without requiring a local server."""
    settings = Settings(
        db_dialect=DatabaseDialect.MYSQL,
        db_pool_size=3,
        db_max_overflow=7,
    )
    database = MySqlDatabase(settings)
    await database.start()
    try:
        assert database.engine.dialect.name == "mysql"
        pool = database.engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.size() == 3
        assert pool._max_overflow == 7
        assert pool._pre_ping is True
        assert pool._recycle == 1800
    finally:
        await database.dispose()


async def test_empty_sqlite_database_upgrades_to_head_idempotently(tmp_path: Path) -> None:
    """The baseline migration reaches head and may be rerun safely."""
    settings = sqlite_settings(tmp_path)
    database = create_database(settings)

    await database.start()
    try:
        await upgrade_database(database)
        await upgrade_database(database)

        async with database.session() as session:
            result = await session.execute(text("SELECT version_num FROM alembic_version"))

        assert result.scalar_one() == "0013"
        async with database.engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("users")
            )

        assert {column["name"] for column in columns} == {
            "id",
            "username",
            "password_hash",
            "display_name",
            "role",
            "is_active",
            "must_change_password",
            "created_at",
            "updated_at",
            "last_login_at",
            "password_changed_at",
        }
        by_name = {column["name"]: column for column in columns}
        assert normalized_server_default(by_name["created_at"]) is None
        assert normalized_server_default(by_name["updated_at"]) is None
        assert_true_server_default(by_name["must_change_password"])
        async with database.engine.connect() as connection:
            job_columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("job_runs")
            )
            job_indexes, job_foreign_keys = await connection.run_sync(
                lambda sync_connection: (
                    inspect(sync_connection).get_indexes("job_runs"),
                    inspect(sync_connection).get_foreign_keys("job_runs"),
                )
            )

        assert {column["name"] for column in job_columns} == {
            "id",
            "kind",
            "ref_id",
            "status",
            "progress",
            "executor",
            "external_job_id",
            "attempt",
            "payload",
            "result",
            "error",
            "created_by",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        }
        assert {
            "ix_job_runs_status",
            "ix_job_runs_kind_created_at",
            "ix_job_runs_ref_id",
        }.issubset({index["name"] for index in job_indexes if index["name"] is not None})
        assert {
            foreign_key["name"]
            for foreign_key in job_foreign_keys
            if foreign_key["name"] is not None
        } == {"fk_job_runs_created_by_users"}
        assert await job_recency_index_directions(database) == [
            ("kind", False),
            ("created_at", True),
        ]
        async with database.engine.connect() as connection:
            sync_columns, sync_indexes, sync_foreign_keys = await connection.run_sync(
                lambda sync_connection: (
                    inspect(sync_connection).get_columns("sync_lock"),
                    inspect(sync_connection).get_indexes("sync_lock"),
                    inspect(sync_connection).get_foreign_keys("sync_lock"),
                )
            )
        assert {column["name"] for column in sync_columns} == {
            "name",
            "job_id",
            "owner",
            "acquired_at",
            "expires_at",
        }
        assert {"ix_sync_lock_expires_at"}.issubset(
            {index["name"] for index in sync_indexes if index["name"] is not None}
        )
        assert {
            foreign_key["name"]
            for foreign_key in sync_foreign_keys
            if foreign_key["name"] is not None
        } == {"fk_sync_lock_job_runs"}
        async with database.engine.connect() as connection:
            (
                event_columns,
                event_indexes,
                event_uniques,
                event_foreign_keys,
            ) = await reflected_event_schema(connection)
        assert event_columns == {
            "id",
            "run_id",
            "seq",
            "event_type",
            "level",
            "stage",
            "step_index",
            "cluster_id",
            "evolution_round_id",
            "job_id",
            "message",
            "payload",
            "error_type",
            "error_message",
            "error_detail",
            "created_at",
        }
        assert {name for name in event_indexes if name.startswith("ix_evolution_run_events_")} == {
            "ix_evolution_run_events_run_created",
            "ix_evolution_run_events_event_type",
            "ix_evolution_run_events_level",
            "ix_evolution_run_events_cluster_id",
        }
        assert event_uniques == {("run_id", "seq")}
        assert (
            ("run_id",),
            "evolution_runs",
            "CASCADE",
        ) in event_foreign_keys
        async with database.engine.connect() as connection:
            evolution_schema = await reflected_evolution_schema(connection)
        assert_evolution_schema(evolution_schema)
        async with database.engine.connect() as connection:
            table_names, payload_columns = await connection.run_sync(
                lambda sync_connection: (
                    set(inspect(sync_connection).get_table_names()),
                    {
                        column["name"]
                        for column in inspect(sync_connection).get_columns(
                            "trajectory_payloads",
                        )
                    },
                )
            )
        assert {
            "trajectory_payloads",
            "source_records",
            "session_families",
            "agents",
            "agent_events",
            "user_turns",
            "bad_cases",
            "bad_case_analysis",
            "probe_reports",
            "evolution_targets",
            "evolution_runs",
            "probe_clusters",
            "probe_cluster_probes",
            "evolution_rounds",
            "evolution_finals",
            "sync_lock",
            "bad_case_root_sync",
        } <= table_names
        assert {
            "root_session_id",
            "agent_id",
            "payload",
            "content_hash",
            "line_count",
        } <= payload_columns
        async with database.engine.connect() as connection:
            bad_case_columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("bad_cases")
            )
        bad_case_by_name = {column["name"]: column for column in bad_case_columns}
        assert bad_case_by_name["title"]["nullable"] is False
        assert bad_case_by_name["bad_case_description"]["nullable"] is False
        assert "reason" not in bad_case_by_name
    finally:
        await database.dispose()


async def test_bad_case_title_migration_backfills_legacy_rows(tmp_path: Path) -> None:
    """Revision 0010 renames the description column and backfills short titles."""
    database = create_database(sqlite_settings(tmp_path))
    await database.start()
    try:
        await asyncio.to_thread(command.upgrade, alembic_config(database), "0009")
        created_at = datetime(2026, 10, 28, 9, tzinfo=UTC)
        short_description = "旧问题描述：首句。第二句。"  # noqa: RUF001
        long_description = "长" * 300
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO bad_cases ("
                    "bad_case_id, bad_case_session_id, bad_case_type, object_name, "
                    "object_type, object_version, bad_case_agent_id, reason, meta_data, "
                    "source, created_at, updated_at"
                    ") VALUES ("
                    ":case_id, :session_id, 'self', 'Explore', 'agent', NULL, NULL, "
                    ":reason, NULL, 'fixtures', :created_at, :created_at"
                    ")",
                ),
                [
                    {
                        "case_id": "legacy-short",
                        "session_id": "10000000-0000-4000-8000-000000000001",
                        "reason": short_description,
                        "created_at": created_at,
                    },
                    {
                        "case_id": "legacy-long",
                        "session_id": "10000000-0000-4000-8000-000000000001",
                        "reason": long_description,
                        "created_at": created_at,
                    },
                ],
            )

        await upgrade_database(database)

        async with database.engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("bad_cases")
            )
        by_name = {column["name"]: column for column in columns}
        assert by_name["title"]["nullable"] is False
        assert by_name["bad_case_description"]["nullable"] is False
        assert "reason" not in by_name
        async with database.session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT bad_case_id, title, bad_case_description "
                            "FROM bad_cases ORDER BY bad_case_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            revision = str(
                (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one(),
            )
        assert revision == "0013"
        assert [dict(row) for row in rows] == [
            {
                "bad_case_id": "legacy-long",
                "title": long_description[:255],
                "bad_case_description": long_description,
            },
            {
                "bad_case_id": "legacy-short",
                "title": short_description,
                "bad_case_description": short_description,
            },
        ]
    finally:
        await database.dispose()


async def test_bad_case_title_migration_downgrade_restores_column_names(
    tmp_path: Path,
) -> None:
    """Downgrade drops only the short title and preserves the description body."""
    database = create_database(sqlite_settings(tmp_path))
    await database.start()
    try:
        await upgrade_database(database)
        created_at = datetime(2026, 10, 28, 9, tzinfo=UTC)
        async with database.transaction() as session:
            await session.execute(
                bad_cases.insert().values(
                    bad_case_id="downgrade-case",
                    bad_case_session_id="10000000-0000-4000-8000-000000000001",
                    bad_case_type="self",
                    object_name="Explore",
                    object_type="agent",
                    object_version=None,
                    bad_case_agent_id=None,
                    title="降级前的短标题",
                    bad_case_description="降级后仍保留的描述正文",
                    meta_data=None,
                    source="fixtures",
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )

        await asyncio.to_thread(command.downgrade, alembic_config(database), "0009")

        async with database.engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("bad_cases")
            )
        by_name = {column["name"]: column for column in columns}
        assert "title" not in by_name
        assert "bad_case_description" not in by_name
        assert by_name["reason"]["nullable"] is False
        async with database.session() as session:
            reason = str(
                (
                    await session.execute(
                        text("SELECT reason FROM bad_cases WHERE bad_case_id = 'downgrade-case'"),
                    )
                ).scalar_one(),
            )
            revision = str(
                (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one(),
            )
        assert revision == "0009"
        assert reason == "降级后仍保留的描述正文"
    finally:
        await database.dispose()


class RepositoryContract:
    """Same repository behavior executed against each configured dialect."""

    dialect = ""

    @pytest_asyncio.fixture
    async def database(self, request: pytest.FixtureRequest) -> AsyncIterator[Database]:
        """Provide the dialect selected by the concrete test class."""
        if self.dialect == "sqlite":
            tmp_path = request.getfixturevalue("tmp_path")
            database: Database = SqliteDatabase(sqlite_settings(tmp_path))
        else:
            url = os.environ.get("HE_DB_URL", "")
            if not url.startswith("mysql+asyncmy://"):
                if os.environ.get("HE_REQUIRE_MYSQL") == "1":
                    pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
                pytest.skip("MySQL integration requires HE_DB_URL")
            database = MySqlDatabase(Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url))
        await database.start()
        try:
            yield database
        finally:
            await database.dispose()

    async def test_repository_round_trip_and_rollback(
        self,
        database: Database,
    ) -> None:
        """SQLAlchemy expressions behave identically on both dialects."""
        metadata = MetaData()
        probe = Table(
            "repository_contract_probes",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(64), nullable=False),
        )
        async with database.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)

        async with database.transaction() as session:
            await session.execute(insert(probe).values(id=1, name="commit"))
        with pytest.raises(RuntimeError, match="discard"):
            async with database.transaction() as session:
                await session.execute(insert(probe).values(id=2, name="discard"))
                raise RuntimeError("discard")

        async with database.session() as session:
            rows = (await session.execute(select(probe).order_by(probe.c.id))).all()
        assert [(row.id, row.name) for row in rows] == [(1, "commit")]
        assert (await database.health()).ok is True

        async with database.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)


class TestSqliteRepository(RepositoryContract):
    """Run the repository contract against SQLite."""

    dialect = "sqlite"


@pytest.mark.mysql
class TestMySqlRepository(RepositoryContract):
    """Run the same repository contract against a MySQL service."""

    dialect = "mysql"

    async def test_mysql_session_uses_shanghai_time_zone(
        self,
        database: Database,
    ) -> None:
        """Every pooled MySQL connection starts with the Shanghai session zone."""
        async with database.session() as session:
            time_zone: object = (
                await session.execute(text("SELECT @@session.time_zone"))
            ).scalar_one()

        assert time_zone == "+08:00"


@pytest.mark.mysql
async def test_empty_mysql_database_upgrades_to_head_idempotently() -> None:
    """The empty baseline also converges on a real MySQL 8 server."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("MySQL integration requires HE_DB_URL")
    settings = Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url)
    database = MySqlDatabase(settings)
    await database.start()
    try:
        await upgrade_database(database)
        await upgrade_database(database)
        async with database.session() as session:
            result = await session.execute(text("SELECT version_num FROM alembic_version"))
        assert result.scalar_one() == "0013"
        async with database.engine.connect() as connection:
            mysql_columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("users")
            )

        assert {column["name"] for column in mysql_columns} == {
            "id",
            "username",
            "password_hash",
            "display_name",
            "role",
            "is_active",
            "must_change_password",
            "created_at",
            "updated_at",
            "last_login_at",
            "password_changed_at",
        }
        by_name = {column["name"]: column for column in mysql_columns}
        assert normalized_server_default(by_name["created_at"]) is None
        assert normalized_server_default(by_name["updated_at"]) is None
        assert_true_server_default(by_name["must_change_password"])
        async with database.engine.connect() as connection:
            job_columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("job_runs")
            )
            job_indexes, job_foreign_keys = await connection.run_sync(
                lambda sync_connection: (
                    inspect(sync_connection).get_indexes("job_runs"),
                    inspect(sync_connection).get_foreign_keys("job_runs"),
                )
            )

        assert {column["name"] for column in job_columns} == {
            "id",
            "kind",
            "ref_id",
            "status",
            "progress",
            "executor",
            "external_job_id",
            "attempt",
            "payload",
            "result",
            "error",
            "created_by",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        }
        assert {
            "ix_job_runs_status",
            "ix_job_runs_kind_created_at",
            "ix_job_runs_ref_id",
        }.issubset({index["name"] for index in job_indexes if index["name"] is not None})
        assert {
            foreign_key["name"]
            for foreign_key in job_foreign_keys
            if foreign_key["name"] is not None
        } == {"fk_job_runs_created_by_users"}
        assert await job_recency_index_directions(database) == [
            ("kind", False),
            ("created_at", True),
        ]
        async with database.engine.connect() as connection:
            sync_columns, sync_indexes, sync_foreign_keys = await connection.run_sync(
                lambda sync_connection: (
                    inspect(sync_connection).get_columns("sync_lock"),
                    inspect(sync_connection).get_indexes("sync_lock"),
                    inspect(sync_connection).get_foreign_keys("sync_lock"),
                )
            )
        assert {column["name"] for column in sync_columns} == {
            "name",
            "job_id",
            "owner",
            "acquired_at",
            "expires_at",
        }
        assert {"ix_sync_lock_expires_at"}.issubset(
            {index["name"] for index in sync_indexes if index["name"] is not None}
        )
        assert {
            foreign_key["name"]
            for foreign_key in sync_foreign_keys
            if foreign_key["name"] is not None
        } == {"fk_sync_lock_job_runs"}
        async with database.engine.connect() as connection:
            (
                event_columns,
                event_indexes,
                event_uniques,
                event_foreign_keys,
            ) = await reflected_event_schema(connection)
        assert event_columns == {
            "id",
            "run_id",
            "seq",
            "event_type",
            "level",
            "stage",
            "step_index",
            "cluster_id",
            "evolution_round_id",
            "job_id",
            "message",
            "payload",
            "error_type",
            "error_message",
            "error_detail",
            "created_at",
        }
        assert {name for name in event_indexes if name.startswith("ix_evolution_run_events_")} == {
            "ix_evolution_run_events_run_created",
            "ix_evolution_run_events_event_type",
            "ix_evolution_run_events_level",
            "ix_evolution_run_events_cluster_id",
        }
        assert event_uniques == {("run_id", "seq")}
        assert (
            ("run_id",),
            "evolution_runs",
            "CASCADE",
        ) in event_foreign_keys
        async with database.engine.connect() as connection:
            evolution_schema = await reflected_evolution_schema(connection)
        assert_evolution_schema(evolution_schema)
        async with database.engine.connect() as connection:
            table_names, payload_columns = await connection.run_sync(
                lambda sync_connection: (
                    set(inspect(sync_connection).get_table_names()),
                    {
                        column["name"]
                        for column in inspect(sync_connection).get_columns(
                            "trajectory_payloads",
                        )
                    },
                )
            )
        assert {
            "trajectory_payloads",
            "source_records",
            "session_families",
            "agents",
            "agent_events",
            "user_turns",
            "bad_cases",
            "bad_case_analysis",
            "probe_reports",
            "evolution_targets",
            "evolution_runs",
            "probe_clusters",
            "probe_cluster_probes",
            "evolution_rounds",
            "evolution_finals",
            "sync_lock",
            "bad_case_root_sync",
        } <= table_names
        assert {"root_session_id", "agent_id", "content_hash", "line_count"} <= payload_columns
        async with database.engine.connect() as connection:
            bad_case_columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("bad_cases")
            )
        bad_case_by_name = {column["name"]: column for column in bad_case_columns}
        assert bad_case_by_name["title"]["nullable"] is False
        assert bad_case_by_name["bad_case_description"]["nullable"] is False
        assert "reason" not in bad_case_by_name
    finally:
        await database.dispose()


@pytest.mark.mysql
async def test_mysql_replay_round_trip_is_idempotent() -> None:
    """The root-atomic replay repository works against the real MySQL lane."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("HE_DB_URL is not configured for MySQL")
    root_id = "55555555-5555-4555-8555-555555555555"
    child_id = "mysql-child-agent"
    database = create_database(
        Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url),
    )
    await database.start()
    try:
        await upgrade_database(database)
        async with database.transaction() as session:
            child_ids = cast(
                list[str],
                (
                    await session.execute(
                        text("SELECT id FROM agents WHERE root_session_id = :root"),
                        {"root": root_id},
                    )
                )
                .scalars()
                .all(),
            )
            if child_ids:
                await session.execute(
                    delete(agent_events).where(agent_events.c.agent_id.in_(child_ids)),
                )
            await session.execute(
                text("DELETE FROM agents WHERE root_session_id = :root"),
                {"root": root_id},
            )
            await session.execute(
                text("DELETE FROM user_turns WHERE root_session_id = :root"),
                {"root": root_id},
            )
            await session.execute(
                text("DELETE FROM session_families WHERE id = :root"),
                {"root": root_id},
            )
            await session.execute(
                text("DELETE FROM source_records WHERE root_session_id = :root"),
                {"root": root_id},
            )
            await session.execute(
                text("DELETE FROM trajectory_payloads WHERE root_session_id = :root"),
                {"root": root_id},
            )

            main_content = (
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": root_id,
                        "timestamp": "2026-10-20T01:00:00Z",
                        "message": {"content": "mysql replay prompt"},
                    },
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": root_id,
                        "timestamp": "2026-10-20T01:01:00Z",
                        "message": {
                            "id": "mysql-main-message",
                            "model": "mysql-model",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "mysql-tool",
                                    "name": "Agent",
                                    "input": {"subagent_type": "Explore"},
                                },
                            ],
                            "usage": {"input_tokens": 3, "output_tokens": 2},
                        },
                    },
                )
                + "\n"
            )
            child_content = (
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": root_id,
                        "timestamp": "2026-10-20T01:02:00Z",
                        "message": {
                            "id": "mysql-child-message",
                            "model": "mysql-model",
                            "content": [{"type": "text", "text": "child"}],
                            "usage": {"input_tokens": 2, "output_tokens": 1},
                        },
                    },
                )
                + "\n"
            )
            for agent_id, kind, content, meta in (
                (root_id, "main", main_content, None),
                (child_id, "subagent", child_content, {"toolUseId": "mysql-tool"}),
            ):
                await session.execute(
                    trajectory_payloads.insert().values(
                        root_session_id=root_id,
                        agent_id=agent_id,
                        agent_kind=kind,
                        payload=content,
                        agent_meta=meta,
                        content_hash=hashlib.sha256(content.encode()).hexdigest(),
                        line_count=content.count("\n"),
                        source_type="script",
                        updated_at=datetime(2026, 10, 20, 1, tzinfo=UTC),
                    ),
                )

        first = await replay_pending_payloads(database)
        second = await replay_pending_payloads(database)
        async with database.session() as session:
            main_source_before = (
                (
                    await session.execute(
                        select(source_records).where(source_records.c.agent_id == root_id),
                    )
                )
                .mappings()
                .one()
            )

        expanded_child = child_content + (
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": root_id,
                    "timestamp": "2026-10-20T01:03:00Z",
                    "message": {
                        "id": "mysql-child-message-2",
                        "model": "mysql-model",
                        "content": [{"type": "text", "text": "second child chunk"}],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
            )
            + "\n"
        )
        async with database.transaction() as session:
            await session.execute(
                trajectory_payloads.update()
                .where(
                    trajectory_payloads.c.root_session_id == root_id,
                    trajectory_payloads.c.agent_id == child_id,
                )
                .values(
                    payload=expanded_child,
                    content_hash=hashlib.sha256(expanded_child.encode()).hexdigest(),
                    line_count=expanded_child.count("\n"),
                ),
            )
        changed = await replay_pending_payloads(database)

        async with database.session() as session:
            counts = {
                "families": len(
                    (await session.execute(select(session_families.c.id))).all(),
                ),
                "agents": len((await session.execute(select(agents.c.id))).all()),
                "events": len((await session.execute(select(agent_events.c.id))).all()),
                "turns": len((await session.execute(select(user_turns.c.id))).all()),
                "sources": len(
                    (await session.execute(select(source_records.c.id))).all(),
                ),
            }
            child_source = (
                (
                    await session.execute(
                        select(source_records).where(source_records.c.agent_id == child_id),
                    )
                )
                .mappings()
                .one()
            )
            main_source_after = (
                (
                    await session.execute(
                        select(source_records).where(source_records.c.agent_id == root_id),
                    )
                )
                .mappings()
                .one()
            )
        assert first.imported == 2
        assert second.imported == 0
        assert second.skipped == 2
        assert changed.imported == 1
        assert child_source["line_count"] == 2
        assert main_source_after["processed_at"] == main_source_before["processed_at"]
        assert main_source_after["last_sync_run_id"] == main_source_before["last_sync_run_id"]
        assert counts["families"] == 1
        assert counts["agents"] == 2
        assert counts["events"] == 1
        assert counts["turns"] == 1
        assert counts["sources"] == 2
    finally:
        await database.dispose()


@pytest.mark.mysql
async def test_mysql_sync_lock_is_single_flight_and_reacquirable() -> None:
    """MySQL unique locking serializes sync job creation and permits reacquire."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("MySQL integration requires HE_DB_URL")
    database = MySqlDatabase(Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url))
    await database.start()
    created_ids: list[str] = []
    try:
        await upgrade_database(database)
        repository = SyncLockRepository(database)
        first = await repository.create_job(
            owner="mysql-contract",
            source="manual",
            created_by=None,
            ttl_seconds=60,
        )
        created_ids.append(first.job.id)
        with pytest.raises(SyncLockConflict):
            await repository.create_job(
                owner="mysql-contract",
                source="scheduler",
                created_by=None,
                ttl_seconds=60,
            )
        await repository.release(first.job.id)
        second = await repository.create_job(
            owner="mysql-contract",
            source="scheduler",
            created_by=None,
            ttl_seconds=60,
        )
        created_ids.append(second.job.id)
        assert second.job.id != first.job.id
        await repository.release(second.job.id)
    finally:
        if created_ids:
            async with database.transaction() as session:
                await session.execute(delete(job_runs).where(job_runs.c.id.in_(created_ids)))
        await database.dispose()


@pytest.mark.mysql
async def test_mysql_session_query_lists_and_reads_canonical_tree() -> None:
    """Session list and tree SQL execute against the real MySQL lane."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("HE_DB_URL is not configured for MySQL")
    root_id = "77777777-7777-4777-8777-777777777777"
    content = (
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": root_id,
                        "timestamp": "2026-10-24T08:00:00Z",
                        "cwd": "/workspace/mysql-queryable",
                        "gitBranch": "main",
                        "message": {"content": "MySQL queryable session"},
                    },
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": root_id,
                        "timestamp": "2026-10-24T08:01:00Z",
                        "message": {
                            "id": "mysql-query-message",
                            "model": "mysql-query-model",
                            "content": [{"type": "text", "text": "done"}],
                            "usage": {"input_tokens": 4, "output_tokens": 2},
                        },
                    },
                ),
            ],
        )
        + "\n"
    )
    database = create_database(
        Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url),
    )
    await database.start()
    try:
        await upgrade_database(database)
        async with database.transaction() as session:
            await session.execute(
                delete(trajectory_payloads).where(
                    trajectory_payloads.c.root_session_id == root_id,
                ),
            )
            await session.execute(
                trajectory_payloads.insert().values(
                    root_session_id=root_id,
                    agent_id=root_id,
                    agent_kind="main",
                    payload=content,
                    agent_meta=None,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    line_count=content.count("\n"),
                    source_type="script",
                    updated_at=datetime(2026, 10, 24, 8, tzinfo=UTC),
                ),
            )

        await replay_pending_payloads(database)
        repository = SessionRepository(database)
        records = await repository.list_sessions(
            query="queryable",
            status="all",
            sort="latest",
            limit=10,
        )
        matching = [record for record in records if record.id == root_id]
        tree_agents = await repository.list_agents(root_id)
        tree_snapshot = await repository.tree_snapshot(root_id)
        counts = await repository.bad_case_counts([root_id])

        assert len(matching) == 1
        assert matching[0].model == "mysql-query-model"
        assert matching[0].user_turns_count == 1
        assert matching[0].messages_count == 1
        assert tree_agents[0].kind == "main"
        assert tree_snapshot is not None
        assert tree_snapshot.session.id == root_id
        assert [agent.id for agent in tree_snapshot.agents] == [root_id]
        assert tree_snapshot.events == ()
        assert tree_snapshot.bad_cases == ()
        assert counts == {}

        case_id = "mysql-problem-api-case"
        probe_id = "mysql-problem-api-probe"
        created_at = datetime(2026, 10, 26, 9, tzinfo=UTC)
        async with database.transaction() as session:
            await session.execute(
                delete(bad_case_analysis).where(
                    bad_case_analysis.c.bad_case_id == case_id,
                ),
            )
            await session.execute(
                delete(bad_cases).where(bad_cases.c.bad_case_id == case_id),
            )
            await session.execute(
                delete(probe_reports).where(probe_reports.c.probe_id == probe_id),
            )
            await session.execute(
                bad_cases.insert().values(
                    bad_case_id=case_id,
                    bad_case_session_id=root_id,
                    bad_case_type="self",
                    object_name="claude",
                    object_type="agent",
                    object_version="mysql-1",
                    bad_case_agent_id=None,
                    title="MySQL problem API",
                    bad_case_description="MySQL problem API",
                    meta_data={"dialect": "mysql"},
                    source="mysql-test",
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
            await session.execute(
                probe_reports.insert().values(
                    probe_id=probe_id,
                    session_id=root_id,
                    probe_type="agent",
                    probe_object="claude",
                    object_version="mysql-1",
                    attribute_type="model",
                    probe_description="MySQL probe",
                    probe_analysis="MySQL analysis",
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
            await session.execute(
                bad_case_analysis.insert().values(
                    bad_case_id=case_id,
                    analysis_status="analyzed_issue",
                    probe_id=probe_id,
                    analysis_result={"summary": "model issue"},
                    updated_at=created_at,
                ),
            )

        problems = ProblemService(
            ProblemRepository(database),
            BadCaseService(BadCaseRepository(database)),
        )
        listed = await problems.list_for_session(root_id)
        detailed = await problems.get_case(case_id, root_id)

        assert [item.bad_case_id for item in listed.items] == [case_id]
        assert detailed.anchor.agent_id == root_id
        assert detailed.analysis.display_state == "analyzed_issue_model"
    finally:
        try:
            if "case_id" in locals():
                async with database.transaction() as session:
                    await session.execute(
                        delete(bad_case_analysis).where(
                            bad_case_analysis.c.bad_case_id == case_id,
                        ),
                    )
                    await session.execute(
                        delete(bad_cases).where(bad_cases.c.bad_case_id == case_id),
                    )
                    await session.execute(
                        delete(probe_reports).where(
                            probe_reports.c.probe_id == probe_id,
                        ),
                    )
        finally:
            await database.dispose()


@pytest.mark.mysql
async def test_mysql_external_read_models_merge_all_attribution_states() -> None:
    """The read-only issue service shares its five-state semantics with MySQL."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("HE_DB_URL is not configured for MySQL")
    session_id = "88888888-8888-4888-8888-888888888888"
    case_id = "mysql-external-case"
    probe_id = "mysql-external-probe"
    created_at = datetime(2026, 10, 26, 9, tzinfo=UTC)
    database = create_database(
        Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url),
    )
    await database.start()
    try:
        await upgrade_database(database)
        async with database.transaction() as session:
            await session.execute(
                delete(bad_case_analysis).where(
                    bad_case_analysis.c.bad_case_id == case_id,
                ),
            )
            await session.execute(
                delete(bad_cases).where(bad_cases.c.bad_case_id == case_id),
            )
            await session.execute(
                delete(probe_reports).where(probe_reports.c.probe_id == probe_id),
            )
            await session.execute(
                probe_reports.insert().values(
                    probe_id=probe_id,
                    session_id=session_id,
                    probe_type="agent",
                    probe_object="Explore",
                    object_version="v1",
                    attribute_type="model",
                    probe_description="MySQL external probe",
                    probe_analysis="MySQL external analysis",
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
            await session.execute(
                bad_cases.insert().values(
                    bad_case_id=case_id,
                    bad_case_session_id=session_id,
                    bad_case_type="discovery",
                    object_name="Explore",
                    object_type="agent",
                    object_version="v1",
                    bad_case_agent_id=None,
                    title="MySQL external issue",
                    bad_case_description="MySQL external issue",
                    meta_data={"dialect": "mysql"},
                    source="mysql-test",
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
            await session.execute(
                bad_case_analysis.insert().values(
                    bad_case_id=case_id,
                    analysis_status="analyzed_issue",
                    probe_id=probe_id,
                    analysis_result={"summary": "model issue"},
                    updated_at=created_at,
                ),
            )

        service = BadCaseService(BadCaseRepository(database))
        result = await service.get_case(case_id)
        probe_result = await service.get_probe(probe_id)

        assert result is not None
        assert result.display_state == "analyzed_issue_model"
        assert result.meta_data == {"dialect": "mysql"}
        assert probe_result is not None
        assert probe_result.from_bad_cases == (case_id,)
    finally:
        try:
            async with database.transaction() as session:
                await session.execute(
                    delete(bad_case_analysis).where(
                        bad_case_analysis.c.bad_case_id == case_id,
                    ),
                )
                await session.execute(
                    delete(bad_cases).where(bad_cases.c.bad_case_id == case_id),
                )
                await session.execute(
                    delete(probe_reports).where(probe_reports.c.probe_id == probe_id),
                )
        finally:
            await database.dispose()


@pytest.mark.mysql
async def test_mysql_evolution_repository_round_trip() -> None:
    """The evolution repository preserves cursor, cluster, round, and final state."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("MySQL integration requires HE_DB_URL")
    settings = Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url)
    database = MySqlDatabase(settings)
    await database.start()
    await upgrade_database(database)
    repository = EvolutionRepository(database)
    run_id = "mysql-m5-repository-run"
    target_id = "mysql-m5-repository-target"
    user_id = "mysql-m5-repository-user"
    probe_ids = ("mysql-m5-model", "mysql-m5-error")
    now = datetime(2026, 10, 27, 2, 3, 4, tzinfo=UTC)

    async def cleanup() -> None:
        async with database.transaction() as session:
            cluster_ids: list[str] = list(
                (
                    await session.execute(
                        select(probe_clusters.c.probe_cluster_id).where(
                            probe_clusters.c.run_id == run_id,
                        ),
                    )
                ).scalars(),
            )
            if cluster_ids:
                await session.execute(
                    delete(probe_cluster_probes).where(
                        probe_cluster_probes.c.probe_cluster_id.in_(cluster_ids),
                    ),
                )
            await session.execute(
                delete(evolution_finals).where(evolution_finals.c.run_id == run_id),
            )
            await session.execute(
                delete(evolution_rounds).where(evolution_rounds.c.run_id == run_id),
            )
            await session.execute(
                delete(probe_clusters).where(probe_clusters.c.run_id == run_id),
            )
            await session.execute(
                delete(evolution_runs).where(evolution_runs.c.id == run_id),
            )
            await session.execute(
                delete(probe_reports).where(probe_reports.c.probe_id.in_(probe_ids)),
            )
            await session.execute(
                delete(evolution_targets).where(
                    evolution_targets.c.id == target_id,
                ),
            )
            await session.execute(
                delete(users).where(users.c.id == user_id),
            )

    try:
        await cleanup()
        async with database.transaction() as session:
            await session.execute(
                insert(users).values(
                    id=user_id,
                    username="mysql-m5-run-creator",
                    password_hash="not-used",  # noqa: S106
                    display_name="MySQL Run Creator",
                    role="admin",
                    is_active=True,
                    must_change_password=False,
                    created_at=now,
                    updated_at=now,
                ),
            )
            await session.execute(
                insert(evolution_targets).values(
                    id=target_id,
                    object_type="skill",
                    object_name="mysql-evolution",
                    repo_url="https://example.invalid/mysql-evolution.git",
                    sub_dir="skills/mysql-evolution",
                    default_ref="v1",
                    enabled=True,
                    created_by=None,
                    created_at=now,
                    updated_at=now,
                ),
            )
            for probe_id, attribute_type in zip(
                probe_ids,
                ("model", "error"),
                strict=True,
            ):
                await session.execute(
                    insert(probe_reports).values(
                        probe_id=probe_id,
                        session_id="mysql-m5-session",
                        probe_type="skill",
                        probe_object="mysql-evolution",
                        object_version="v1",
                        attribute_type=attribute_type,
                        probe_description="MySQL evolution probe",
                        probe_analysis="MySQL evolution analysis",
                        created_at=now,
                        updated_at=now,
                    ),
                )

        await repository.create_run(
            {
                "id": run_id,
                "title": "MySQL evolution run",
                "mode": "full",
                "engine_key": "full",
                "eval_config": {"gateway": "mock"},
                "target_config_id": target_id,
                "target_type": "skill",
                "target_name": "mysql-evolution",
                "target_version": "v1",
                "repo_url": "https://example.invalid/mysql-evolution.git",
                "sub_dir": "skills/mysql-evolution",
                "status": "running",
                "created_by": user_id,
                "created_at": now,
            },
        )
        await repository.advance_unit(
            run_id,
            expected=0,
            target=1,
            cluster_count=1,
        )
        clusters = await repository.replace_clusters(
            run_id,
            (
                ClusterWrite(
                    type="model",
                    description="model cluster",
                    probes=(probe_ids[0],),
                    impact=5.0,
                ),
            ),
        )
        await repository.advance_unit(
            run_id,
            expected=1,
            target=2,
            cluster_count=1,
        )
        await repository.advance_unit(
            run_id,
            expected=2,
            target=3,
            cluster_count=1,
        )
        await repository.advance_unit(
            run_id,
            expected=3,
            target=4,
            cluster_count=1,
        )
        await repository.insert_rounds(
            (
                {
                    "id": "mysql-m5-round-1",
                    "run_id": run_id,
                    "probe_cluster_id": clusters[0].probe_cluster_id,
                    "seq": 1,
                    "status": "pending",
                },
            ),
        )
        await repository.advance_unit(
            run_id,
            expected=4,
            target=5,
            cluster_count=1,
        )
        final = await repository.write_final(
            {
                "run_id": run_id,
                "best_version": "v1-best",
                "verdict": True,
                "best_score": 88.0,
                "init_score": 80.0,
                "metrics": [{"name": "score", "value": 88.0}],
            },
        )
        run = await repository.get_run(run_id)

        assert run is not None
        assert run.progress_unit == 5
        assert [cluster.impact for cluster in clusters] == [5.0]
        assert [round_.seq for round_ in await repository.list_rounds(run_id)] == [1]
        assert final.best_version == "v1-best"
        summaries = await repository.list_summaries()
        summary = next(item for item in summaries if item.run.id == run_id)
        assert summary.created_by_name == "MySQL Run Creator"
        assert summary.cluster_count == 1
    finally:
        try:
            await cleanup()
        finally:
            await database.dispose()


@pytest.mark.mysql
async def test_mysql_evolution_target_repository_contract() -> None:
    """Target CRUD, conflicts, active-run checks, and model aggregation use MySQL."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("MySQL integration requires HE_DB_URL")
    settings = Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url)
    database = MySqlDatabase(settings)
    await database.start()
    await upgrade_database(database)
    repository = EvolutionTargetRepository(database)
    now = datetime(2026, 10, 27, 2, 4, 5, tzinfo=UTC)
    target_ids = (
        "mysql-m5-target-disabled",
        "mysql-m5-target-agent",
        "mysql-m5-target-skill",
        "mysql-m5-target-terminal",
    )
    run_ids = tuple(
        f"mysql-m5-target-run-{status}"
        for status in (
            "preparing",
            "running",
            "blocked",
            "completed",
        )
    )
    session_ids = ("mysql-m5-catalog-s1", "mysql-m5-catalog-s2")
    source_ids = (990_001, 990_002)
    agent_ids = tuple(f"mysql-m5-catalog-a{index}" for index in range(1, 7))

    async def cleanup() -> None:
        async with database.transaction() as session:
            await session.execute(
                delete(evolution_runs).where(evolution_runs.c.id.in_(run_ids)),
            )
            await session.execute(
                delete(agents).where(agents.c.id.in_(agent_ids)),
            )
            await session.execute(
                delete(session_families).where(
                    session_families.c.id.in_(session_ids),
                ),
            )
            await session.execute(
                delete(source_records).where(
                    source_records.c.id.in_(source_ids),
                ),
            )
            await session.execute(
                delete(evolution_targets).where(
                    evolution_targets.c.id.in_(target_ids),
                ),
            )

    try:
        await cleanup()
        disabled = await repository.insert(
            {
                "object_type": "skill",
                "object_name": "z-disabled",
                "repo_url": "https://git.example.com/z-disabled.git",
                "sub_dir": "skills/z-disabled",
                "default_ref": "main",
                "enabled": False,
            },
        )
        agent_target = await repository.insert(
            {
                "object_type": "agent",
                "object_name": "b-agent",
                "repo_url": "ssh://git@git.example.com/b-agent.git",
                "sub_dir": "agents/b-agent",
                "default_ref": "main",
                "enabled": True,
            },
        )
        skill_target = await repository.insert(
            {
                "object_type": "skill",
                "object_name": "a-skill",
                "repo_url": "git@git.example.com:a-skill.git",
                "sub_dir": "skills/a-skill",
                "default_ref": "main",
                "enabled": True,
            },
        )
        terminal_target = await repository.insert(
            {
                "object_type": "skill",
                "object_name": "terminal",
                "repo_url": "https://git.example.com/terminal.git",
                "sub_dir": "skills/terminal",
                "default_ref": "main",
                "enabled": True,
            },
        )

        assert [target.id for target in await repository.list()] == [
            agent_target.id,
            skill_target.id,
            terminal_target.id,
            disabled.id,
        ]
        assert [target.id for target in await repository.list(enabled_only=True)] == [
            agent_target.id,
            skill_target.id,
            terminal_target.id,
        ]
        assert await repository.find("skill", "a-skill") == skill_target

        with pytest.raises(EvolutionTargetConflict):
            await repository.insert(
                {
                    "object_type": "skill",
                    "object_name": "a-skill",
                    "repo_url": "https://git.example.com/duplicate.git",
                    "sub_dir": "skills/duplicate",
                    "default_ref": "main",
                    "enabled": True,
                },
            )

        updated = await repository.update(
            skill_target.id,
            {
                "object_type": "skill",
                "object_name": "renamed-skill",
                "repo_url": "https://git.example.com/renamed.git",
                "sub_dir": "skills/renamed",
                "default_ref": "v2",
                "enabled": False,
            },
        )
        assert updated.object_name == "renamed-skill"
        assert updated.enabled is False
        with pytest.raises(LookupError):
            await repository.update(
                "mysql-m5-target-missing",
                {
                    "object_type": "skill",
                    "object_name": "missing",
                    "repo_url": "https://git.example.com/missing.git",
                    "sub_dir": "skills/missing",
                    "default_ref": "main",
                    "enabled": True,
                },
            )

        async with database.transaction() as session:
            for run_id, status in zip(
                run_ids,
                ("preparing", "running", "blocked", "completed"),
                strict=True,
            ):
                target = terminal_target if status == "completed" else skill_target
                await session.execute(
                    insert(evolution_runs).values(
                        id=run_id,
                        title="target contract run",
                        mode="full",
                        engine_key="full",
                        eval_config={},
                        target_config_id=target.id,
                        target_type="skill",
                        target_name=target.object_name,
                        target_version="main",
                        repo_url=target.repo_url,
                        sub_dir=target.sub_dir,
                        remote_name="origin",
                        push_status="pending",
                        models=[],
                        status=status,
                        retry_attempts=0,
                        eval_status="pending",
                        progress_unit=0,
                        stats={},
                        created_at=now,
                    ),
                )

        assert await repository.has_active_runs(skill_target.id) is True
        assert await repository.has_active_runs(terminal_target.id) is False
        assert await repository.delete(disabled.id) is True
        assert await repository.delete(disabled.id) is False

        async with database.transaction() as session:
            for source_id, session_id in zip(source_ids, session_ids, strict=True):
                await session.execute(
                    insert(source_records).values(
                        id=source_id,
                        root_session_id=session_id,
                        agent_id=f"{session_id}-main",
                        kind="main",
                        source_type="jsonl",
                        line_count=1,
                        received_at=now,
                    ),
                )
                await session.execute(
                    insert(session_families).values(
                        id=session_id,
                        short_title=session_id,
                        title=session_id,
                        search_text=session_id,
                        repo_name="repo",
                        project_path="/repo",
                        models=[],
                        started_at=now,
                        ended_at=now,
                        duration_seconds=0,
                        status="ok",
                        skill_names=[],
                        user_turns_count=0,
                        messages_count=1,
                        tools_count=0,
                        errors_count=0,
                        agents_count=0,
                        tokens_total=0,
                        source_type="jsonl",
                        main_record_id=source_id,
                        imported_at=now,
                        updated_at=now,
                    ),
                )
            for index, (session_id, model) in enumerate(
                [
                    (session_ids[0], "mysql-m5-model"),
                    (session_ids[0], "mysql-m5-model"),
                    (session_ids[1], "mysql-m5-model"),
                    (session_ids[0], "mysql-m5-model-flash"),
                    (session_ids[0], None),
                    (session_ids[1], "   "),
                ],
                start=1,
            ):
                await session.execute(
                    insert(agents).values(
                        id=agent_ids[index - 1],
                        root_session_id=session_id,
                        parent_agent_id=None,
                        kind="main" if index == 1 else "subagent",
                        model=model,
                        spawn_depth=0,
                        lines=1,
                        messages_count=1,
                        tokens_total=0,
                        tools_count=0,
                        errors_count=0,
                        sort_key=float(index),
                    ),
                )

        assert [
            record
            for record in await repository.list_models()
            if record.model.startswith("mysql-m5-model")
        ] == [
            ModelUsageRecord(model="mysql-m5-model", session_count=2, agent_count=3),
            ModelUsageRecord(model="mysql-m5-model-flash", session_count=1, agent_count=1),
        ]
    finally:
        try:
            await cleanup()
        finally:
            await database.dispose()


def _catalog_row(title: str, now: datetime) -> CatalogCaseRow:
    """Build one catalog row for the MySQL dialect upsert contract."""
    return CatalogCaseRow(
        bad_case_id=991001,
        bad_case_session_id="session-catalog-mysql",
        bad_case_type="self",
        object_name="deep-research",
        object_type="skill",
        object_version="v1",
        bad_case_agent_id=None,
        title=title,
        description="description",
        meta_data=None,
        source="test",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.mysql
async def test_mysql_catalog_sync_upserts_by_primary_key() -> None:
    """MySQL keeps one row per bad_case_id and updates the existing one."""
    url = os.environ.get("HE_DB_URL", "")
    if not url.startswith("mysql+asyncmy://"):
        if os.environ.get("HE_REQUIRE_MYSQL") == "1":
            pytest.fail("HE_DB_URL must point to MySQL when HE_REQUIRE_MYSQL=1")
        pytest.skip("MySQL integration requires HE_DB_URL")
    settings = Settings(db_dialect=DatabaseDialect.MYSQL, db_url=url)
    database = MySqlDatabase(settings)
    await database.start()
    try:
        await upgrade_database(database, bootstrap_external_tables=True)
        now = datetime.now(UTC)
        async with database.transaction() as session:
            await session.execute(delete(bad_cases).where(bad_cases.c.bad_case_id == "991001"))
            await session.execute(
                delete(bad_case_catalog_sync).where(bad_case_catalog_sync.c.id == "bad_cases")
            )
        repository = BadCaseCatalogSyncRepository(database)
        await repository.initialize(now)
        first = await repository.commit_page(
            [_catalog_row("old", now)],
            last_bad_case_id=1,
            pending_count=0,
            now=now,
        )
        second = await repository.commit_page(
            [_catalog_row("new", now)],
            last_bad_case_id=1,
            pending_count=0,
            now=now,
        )
        assert (first.inserted, first.updated) == (1, 0)
        assert (second.inserted, second.updated) == (0, 1)
        async with database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(bad_cases).where(bad_cases.c.bad_case_id == "991001")
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 1
        assert rows[0]["title"] == "new"
        state = await repository.get_state()
        assert state is not None
        assert state.last_bad_case_id == 1
        assert state.synced_count >= 1
    finally:
        try:
            async with database.transaction() as session:
                await session.execute(delete(bad_cases).where(bad_cases.c.bad_case_id == "991001"))
                await session.execute(
                    delete(bad_case_catalog_sync).where(bad_case_catalog_sync.c.id == "bad_cases")
                )
        finally:
            await database.dispose()
