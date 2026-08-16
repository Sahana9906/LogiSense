"""phase 1: persist contextual metrics snapshot on investigation_runs

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "investigation_runs",
        sa.Column("contextual_metrics", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investigation_runs", "contextual_metrics")
