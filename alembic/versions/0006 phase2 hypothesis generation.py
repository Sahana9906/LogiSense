"""phase 2: hypothesis generation

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

hypothesis_confidence = sa.Enum("low", "medium", "high", name="hypothesis_confidence")


def upgrade() -> None:
    # NOTE: do not pre-create hypothesis_confidence here. op.create_table()
    # creates Postgres ENUM types itself for any Enum-typed column it
    # contains -- pre-creating it too causes a duplicate CREATE TYPE and
    # fails with DuplicateObject even under checkfirst=True (same class of
    # bug fixed in migration 0002; missed here originally).
    op.execute("ALTER TYPE investigation_stage ADD VALUE IF NOT EXISTS 'hypothesis_generated'")

    op.create_table(
        "investigation_hypotheses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("investigation_runs.run_id"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("supporting_signals", sa.Text(), nullable=False),
        sa.Column("confidence", hypothesis_confidence, nullable=False),
        sa.Column("what_would_confirm", sa.Text(), nullable=False),
        sa.Column("what_would_refute", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "rank", name="uq_hypothesis_run_rank"),
    )
    op.create_index("ix_investigation_hypotheses_run", "investigation_hypotheses", ["run_id"])


def downgrade() -> None:
    op.drop_table("investigation_hypotheses")
    hypothesis_confidence.drop(op.get_bind(), checkfirst=True)
    # Postgres cannot drop a single enum value; downgrading the stage enum
    # itself is intentionally left as a no-op (existing 'hypothesis_generated'
    # rows would need to be migrated to another stage first if this matters).