"""Add Skill input snapshots and incremental checkpoints without historical backfill.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add new storage; do not copy, rewrite, or remove existing evolution data."""
    op.create_table(
        "skill_evolution_state",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["evolution_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )


def downgrade() -> None:
    """Remove only this revision's new table when explicitly downgrading."""
    op.drop_table("skill_evolution_state")
