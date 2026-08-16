"""canonical supply-chain schema (dataset-agnostic)

Revision ID: 0001
Revises:
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("city", sa.String(128)),
        sa.Column("state", sa.String(128)),
        sa.Column("country", sa.String(128)),
        sa.Column("region", sa.String(128)),
        sa.UniqueConstraint("city", "state", "country", "region", name="uq_locations_identity"),
    )
    op.create_index("ix_locations_country_region", "locations", ["country", "region"])

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_product_id", sa.String(64), unique=True),
        sa.Column("name", sa.String(255)),
        sa.Column("category", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_supplier_id", sa.String(64), unique=True),
        sa.Column("name", sa.String(255)),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_customer_id", sa.String(64), unique=True),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("order_date", sa.Date()),
        sa.Column("order_value", sa.Numeric(14, 2)),
        sa.Column("quantity", sa.Integer()),
        sa.Column("status", sa.String(128)),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id")),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id")),
        sa.CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_orders_quantity_nonneg"),
    )
    op.create_index("ix_orders_order_date", "orders", ["order_date"])

    op.create_table(
        "shipments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False, unique=True),
        sa.Column("origin_location_id", sa.Integer(), sa.ForeignKey("locations.id")),
        sa.Column("destination_location_id", sa.Integer(), sa.ForeignKey("locations.id")),
        sa.Column("shipping_mode", sa.String(128)),
        sa.Column("freight_cost", sa.Numeric(14, 2)),
        sa.Column("expected_delivery_date", sa.Date()),
        sa.Column("actual_delivery_date", sa.Date()),
        sa.Column("delay_days", sa.Integer()),
    )
    op.create_index("ix_shipments_delay_days", "shipments", ["delay_days"])
    op.create_index(
        "ix_shipments_expected_actual", "shipments", ["expected_delivery_date", "actual_delivery_date"]
    )

    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("shipment_id", sa.Integer(), sa.ForeignKey("shipments.id"), nullable=False, unique=True),
        sa.Column("incident_type", sa.String(128), nullable=False),
        sa.Column("deviation_value", sa.Numeric(14, 2)),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_incidents_type", "incidents", ["incident_type"])


def downgrade() -> None:
    op.drop_table("incidents")
    op.drop_table("shipments")
    op.drop_table("orders")
    op.drop_table("customers")
    op.drop_table("suppliers")
    op.drop_table("products")
    op.drop_table("locations")
