"""Skill-only immutable input and incremental algorithm checkpoint storage."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Table

from app.db.models.evolution import EVOLUTION_METADATA

skill_evolution_state = Table(
    "skill_evolution_state",
    EVOLUTION_METADATA,
    Column(
        "run_id",
        String(36),
        ForeignKey("evolution_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("input_snapshot", JSON, nullable=False),
    Column("checkpoint", JSON, nullable=False),
    Column("revision", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True, slots=True)
class SkillEvolutionStateRecord:
    """Frozen input and latest committed algorithm facts, independent of source rows."""

    run_id: str
    input_snapshot: dict[str, Any]
    checkpoint: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime
