"""phase 1: enrich incident_intake with impact, priority, recommended_next_step

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

incident_impact = sa.Enum("minimal", "moderate", "significant", "severe", name="incident_impact")
investigation_priority = sa.Enum("low", "medium", "high", "critical", name="investigation_priority")


def upgrade() -> None:
    bind = op.get_bind()
    incident_impact.create(bind, checkfirst=True)
    investigation_priority.create(bind, checkfirst=True)

    op.alter_column(
        "incident_intake", "severity_rationale", new_column_name="rationale"
    )
    op.add_column(
        "incident_intake",
        sa.Column("impact", incident_impact, nullable=False, server_default="moderate"),
    )
    op.add_column(
        "incident_intake",
        sa.Column("priority", investigation_priority, nullable=False, server_default="medium"),
    )
    op.add_column(
        "incident_intake",
        sa.Column("recommended_next_step", sa.Text(), nullable=False, server_default="Proceed to hypothesis generation."),
    )
    # server_default values above only exist to satisfy NOT NULL on any
    # pre-existing rows; new inserts always supply explicit values.
    op.alter_column("incident_intake", "impact", server_default=None)
    op.alter_column("incident_intake", "priority", server_default=None)
    op.alter_column("incident_intake", "recommended_next_step", server_default=None)


def downgrade() -> None:
    op.drop_column("incident_intake", "recommended_next_step")
    op.drop_column("incident_intake", "priority")
    op.drop_column("incident_intake", "impact")
    op.alter_column(
        "incident_intake", "rationale", new_column_name="severity_rationale"
    )
    investigation_priority.drop(op.get_bind(), checkfirst=True)
    incident_impact.drop(op.get_bind(), checkfirst=True)
