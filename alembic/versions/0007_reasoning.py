"""phase 2: rank/rule-out reasoning + ruled-out hypotheses snapshot

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "investigation_hypotheses",
        sa.Column(
            "why_ranked_here",
            sa.Text(),
            nullable=False,
            server_default="(not available -- generated before this column existed)",
        ),
    )
    op.alter_column("investigation_hypotheses", "why_ranked_here", server_default=None)

    op.add_column(
        "investigation_runs",
        sa.Column("ruled_out_hypotheses", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investigation_runs", "ruled_out_hypotheses")
    op.drop_column("investigation_hypotheses", "why_ranked_here")