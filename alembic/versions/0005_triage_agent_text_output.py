"""triage agent: store full triage assessment text; impact/priority now optional

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incident_intake",
        sa.Column(
            "triage_assessment",
            sa.Text(),
            nullable=False,
            server_default="(not available -- generated before this column existed)",
        ),
    )
    op.alter_column("incident_intake", "triage_assessment", server_default=None)
    op.alter_column("incident_intake", "impact", nullable=True)
    op.alter_column("incident_intake", "priority", nullable=True)


def downgrade() -> None:
    op.alter_column("incident_intake", "priority", nullable=False)
    op.alter_column("incident_intake", "impact", nullable=False)
    op.drop_column("incident_intake", "triage_assessment")
