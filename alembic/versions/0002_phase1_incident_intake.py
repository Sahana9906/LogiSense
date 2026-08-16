"""phase 1: investigation runs + incident intake

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

investigation_run_status = sa.Enum(
    "pending", "running", "completed", "failed", name="investigation_run_status"
)
investigation_stage = sa.Enum(
    "incident_intake", "ready_for_hypothesis", name="investigation_stage"
)
incident_severity = sa.Enum("low", "medium", "high", "critical", name="incident_severity")


def upgrade() -> None:
    # NOTE: do not pre-create these enum types. op.create_table() creates
    # Postgres ENUM types itself for any Enum-typed column it contains --
    # pre-creating them here as well causes a duplicate CREATE TYPE and
    # fails with DuplicateObject even under checkfirst=True, because the
    # table-creation code path does not check first. Only add_column-style
    # migrations (see 0003) need the explicit-create pattern.
    op.create_table(
        "investigation_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("status", investigation_run_status, nullable=False),
        sa.Column("current_stage", investigation_stage, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
    )
    op.create_index(
        "ix_investigation_runs_incident_started", "investigation_runs", ["incident_id", "started_at"]
    )
    op.create_index(
        "ix_investigation_runs_status_stage", "investigation_runs", ["status", "current_stage"]
    )

    op.create_table(
        "incident_intake",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("investigation_runs.run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("incident_type", sa.String(128), nullable=False),
        sa.Column("severity", incident_severity, nullable=False),
        sa.Column("severity_rationale", sa.Text(), nullable=False),
        sa.Column("normalized_summary", sa.Text(), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_incident_intake_type_severity", "incident_intake", ["incident_type", "severity"]
    )


def downgrade() -> None:
    op.drop_table("incident_intake")
    op.drop_table("investigation_runs")
    incident_severity.drop(op.get_bind(), checkfirst=True)
    investigation_stage.drop(op.get_bind(), checkfirst=True)
    investigation_run_status.drop(op.get_bind(), checkfirst=True)
