"""Alembic upgrade creates the service-owned catalog sync state table."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import inspect, text

from app.config import DatabaseDialect, Settings
from app.db.factory import create_database
from app.db.migrate import upgrade_database

EXPECTED_COLUMNS = {
    "id",
    "last_bad_case_id",
    "synced_count",
    "pending_count",
    "status",
    "attempts",
    "last_error",
    "last_run_at",
    "last_success_at",
    "updated_at",
}


@pytest.mark.asyncio
async def test_upgrade_creates_catalog_sync_table(tmp_path: Path) -> None:
    """Head migration exposes the single-row cursor table with its primary key."""
    settings = Settings(
        db_dialect=DatabaseDialect.SQLITE,
        data_dir=tmp_path,
        jwt_secret=SecretStr("catalog-sync-migration-secret"),
    )
    database = create_database(settings)
    await database.start()
    try:
        await upgrade_database(database)

        async with database.engine.connect() as connection:
            exists, columns, primary_key = await connection.run_sync(
                lambda sync_connection: (
                    inspect(sync_connection).has_table("bad_case_catalog_sync"),
                    {
                        column["name"]
                        for column in inspect(sync_connection).get_columns(
                            "bad_case_catalog_sync",
                        )
                    },
                    inspect(sync_connection).get_pk_constraint("bad_case_catalog_sync")[
                        "constrained_columns"
                    ],
                ),
            )
        async with database.session() as session:
            revision: str = str(
                (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one(),
            )

        assert exists is True
        assert columns == EXPECTED_COLUMNS
        assert primary_key == ["id"]
        assert revision == "0013"
    finally:
        await database.dispose()
