"""Canonical supply-chain schema + Phase 1 (incident intake & triage) models.

These models are intentionally dataset-agnostic. Nothing here assumes a
specific source dataset (Olist, DataCo, Walmart, ...). Any dataset gets
mapped into this canonical shape by a separate ingestion/normalization
layer (not implemented here). Optional business fields stay optional so
the schema does not force datasets to have data they don't contain.
"""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from logisense.db import Base


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    """Make SQLAlchemy persist/compare enum .value (e.g. "pending"),
    not the Python member .name (e.g. "PENDING"). Without this,
    SQLAlchemy's default Enum type sends member names to the database,
    which does not match the lowercase values the Postgres ENUM types
    (and this app's Gemini/API contracts) actually use."""
    return [member.value for member in enum_cls]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class InvestigationRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class InvestigationStage(str, Enum):
    INCIDENT_INTAKE = "incident_intake"
    READY_FOR_HYPOTHESIS = "ready_for_hypothesis"


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentImpact(str, Enum):
    MINIMAL = "minimal"
    MODERATE = "moderate"
    SIGNIFICANT = "significant"
    SEVERE = "severe"


class InvestigationPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Canonical supply-chain schema
# ---------------------------------------------------------------------------

class Location(Base):
    """Generic reusable location dimension (used for origin/destination/
    supplier/customer locations). All fields optional: a dataset may only
    provide a country, or only a city, etc."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("city", "state", "country", "region", name="uq_locations_identity"),
        Index("ix_locations_country_region", "country", "region"),
    )


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_product_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Supplier(Base):
    """Supplier / seller. Optional dataset concept -- a dataset without a
    supplier/seller notion simply never populates this table and Order.
    supplier_id stays NULL."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_supplier_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(255))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    location: Mapped[Location | None] = relationship()


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    location: Mapped[Location | None] = relationship()


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    order_date: Mapped[date | None] = mapped_column(Date)
    order_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    quantity: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(128))

    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))

    customer: Mapped[Customer | None] = relationship()
    product: Mapped[Product | None] = relationship()
    supplier: Mapped[Supplier | None] = relationship()

    __table_args__ = (
        CheckConstraint("quantity IS NULL OR quantity >= 0"),
        Index("ix_orders_order_date", "order_date"),
    )


class Shipment(Base):
    """Shipment/delivery record for an order. Delay/deviation fields are
    derived (not raw dataset columns) so they work the same way regardless
    of which source columns a dataset originally used."""

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, unique=True)

    origin_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    destination_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))

    shipping_mode: Mapped[str | None] = mapped_column(String(128))
    freight_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    expected_delivery_date: Mapped[date | None] = mapped_column(Date)
    actual_delivery_date: Mapped[date | None] = mapped_column(Date)
    delay_days: Mapped[int | None] = mapped_column(Integer)

    order: Mapped[Order] = relationship()
    origin: Mapped[Location | None] = relationship(foreign_keys=[origin_location_id])
    destination: Mapped[Location | None] = relationship(foreign_keys=[destination_location_id])

    __table_args__ = (
        Index("ix_shipments_delay_days", "delay_days"),
        Index("ix_shipments_expected_actual", "expected_delivery_date", "actual_delivery_date"),
    )


class Incident(Base):
    """A data-derived operational incident. incident_type/rationale here
    are purely factual/data-derived (e.g. 'delivery_delay', 'delay_days=12
    exceeds expected delivery date'). Interpretive severity classification
    happens in Phase 1 (IncidentIntake), not here."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), nullable=False, unique=True)
    incident_type: Mapped[str] = mapped_column(String(128), nullable=False)
    deviation_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    shipment: Mapped[Shipment] = relationship()

    __table_args__ = (Index("ix_incidents_type", "incident_type"),)


# ---------------------------------------------------------------------------
# Phase 1: investigation runs + incident intake
# ---------------------------------------------------------------------------

class InvestigationRun(Base):
    __tablename__ = "investigation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    status: Mapped[InvestigationRunStatus] = mapped_column(
        SAEnum(InvestigationRunStatus, name="investigation_run_status", values_callable=_enum_values),
        nullable=False,
    )
    current_stage: Mapped[InvestigationStage] = mapped_column(
        SAEnum(InvestigationStage, name="investigation_stage", values_callable=_enum_values),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    contextual_metrics: Mapped[dict | None] = mapped_column(JSON)
    """Snapshot of ContextualMetricsRepository.build_profile(...).to_dict()
    at the moment this run was triaged -- i.e. exactly what Gemini saw.
    This is deterministic, SQL-derived data (not the LLM's response), so
    storing it does not violate the "no raw LLM JSON blob" rule; it lets
    the frontend and any later processing reuse the same metrics without
    recomputing them, and gives a historical record of what informed a
    given severity call even if live metrics later drift."""

    incident: Mapped[Incident] = relationship()

    __table_args__ = (
        Index("ix_investigation_runs_incident_started", "incident_id", "started_at"),
        Index("ix_investigation_runs_status_stage", "status", "current_stage"),
    )


class IncidentIntake(Base):
    """Phase 1 output -- the Triage Agent's assessment.

    `triage_assessment` holds the complete, verbatim, human-readable text
    Gemini returned (INCIDENT TYPE / SEVERITY / WHAT HAPPENED / WHY THIS
    SEVERITY / POTENTIAL BUSINESS IMPACT / IMPORTANT SIGNALS /
    INVESTIGATION PRIORITIES / INITIAL ASSESSMENT) -- this is the single
    source of truth for everything the UI needs to display, never
    reconstructed or paraphrased.

    incident_type/severity are additionally parsed out into typed columns
    purely so existing typed consumers (API filters, the dashboard's
    severity badge) keep working; rationale/recommended_next_step/
    normalized_summary mirror the closest-matching sections for the same
    reason. impact/priority are no longer produced by this prompt (it
    yields a business-impact narrative and an investigation checklist
    instead of discrete enum levels), so both are nullable and left NULL.

    Never stored as a raw JSON blob, and never handed off via a file --
    PostgreSQL is the sole Phase 1 -> Phase 2 handoff mechanism."""

    __tablename__ = "incident_intake"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("investigation_runs.run_id"), nullable=False, unique=True
    )
    incident_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[IncidentSeverity] = mapped_column(
        SAEnum(IncidentSeverity, name="incident_severity", values_callable=_enum_values), nullable=False
    )
    impact: Mapped[IncidentImpact | None] = mapped_column(
        SAEnum(IncidentImpact, name="incident_impact", values_callable=_enum_values), nullable=True
    )
    priority: Mapped[InvestigationPriority | None] = mapped_column(
        SAEnum(InvestigationPriority, name="investigation_priority", values_callable=_enum_values),
        nullable=True,
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_next_step: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_summary: Mapped[str] = mapped_column(Text, nullable=False)
    triage_assessment: Mapped[str] = mapped_column(Text, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[InvestigationRun] = relationship()

    __table_args__ = (Index("ix_incident_intake_type_severity", "incident_type", "severity"),)
