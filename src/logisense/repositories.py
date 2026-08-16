"""Data access layer.

- IncidentRepository: fetch canonical incident context (only what exists).
- ContextualMetricsRepository: build a rich, data-derived incident context
  profile (overall/supplier/regional/shipping-mode comparisons, business
  impact, trend, and anomaly signals) using aggregate SQL only. Nothing
  here is computed by the LLM.
- InvestigationRunRepository: the Postgres-backed Phase 1 state machine
  (runs + incident_intake), the sole Phase 1 -> Phase 2 handoff.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from logisense import models as m

# Trend detection windows/thresholds. Documented here since they are the
# only "thresholds" in this module -- they classify a comparison as a
# label (improving/stable/deteriorating), they never decide severity.
TREND_WINDOW_DAYS = 30
TREND_MIN_SAMPLE_SIZE = 5
TREND_RELATIVE_CHANGE_THRESHOLD = 0.10  # +/-10% relative change

# Descriptive-only thresholds for the "signals" block. These label how a
# group's late-rate ratio compares to the network baseline; they are not
# severity determinations (severity remains the LLM's job in Phase 1).
PERFORMANCE_ELEVATED_RATIO = 1.5
PERFORMANCE_BELOW_AVERAGE_RATIO = 0.67


class BaseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session


# ---------------------------------------------------------------------------
# Canonical context retrieval
# ---------------------------------------------------------------------------

class IncidentRepository(BaseRepository):
    def get_with_context(self, incident_id: int) -> m.Incident | None:
        return self.session.scalar(
            select(m.Incident)
            .options(
                joinedload(m.Incident.shipment).joinedload(m.Shipment.origin),
                joinedload(m.Incident.shipment).joinedload(m.Shipment.destination),
                joinedload(m.Incident.shipment)
                .joinedload(m.Shipment.order)
                .joinedload(m.Order.product),
                joinedload(m.Incident.shipment)
                .joinedload(m.Shipment.order)
                .joinedload(m.Order.customer)
                .joinedload(m.Customer.location),
                joinedload(m.Incident.shipment)
                .joinedload(m.Shipment.order)
                .joinedload(m.Order.supplier)
                .joinedload(m.Supplier.location),
            )
            .where(m.Incident.id == incident_id)
        )

    def list_recent(self, limit: int = 50) -> list[m.Incident]:
        """For a browsable incidents list in the frontend. Ordered by most
        recently detected first."""
        return list(
            self.session.scalars(
                select(m.Incident)
                .options(
                    joinedload(m.Incident.shipment)
                    .joinedload(m.Shipment.order)
                    .joinedload(m.Order.customer)
                    .joinedload(m.Customer.location),
                    joinedload(m.Incident.shipment)
                    .joinedload(m.Shipment.order)
                    .joinedload(m.Order.supplier),
                )
                .order_by(m.Incident.detected_at.desc())
                .limit(limit)
            )
        )


# ---------------------------------------------------------------------------
# Generic upsert helpers for the canonical schema. Dialect-portable (plain
# select-then-insert-or-update rather than a Postgres-only ON CONFLICT), so
# any dataset-specific ingestion loader can reuse these instead of writing
# its own upsert logic -- keeping loaders thin, dataset-only mapping code.
# ---------------------------------------------------------------------------

class DimensionRepository(BaseRepository):
    """Every upsert_* method accepts flush=True (default, safe for ad-hoc
    use) or flush=False (for high-throughput loaders that want to batch
    many upserts into one flush() call themselves -- see the Olist/DataCo
    loaders, which flush once per row instead of once per field)."""

    def upsert_location(
        self,
        city: str | None,
        state: str | None,
        country: str | None,
        region: str | None,
        flush: bool = True,
    ) -> m.Location:
        if not any([city, state, country, region]):
            raise ValueError("upsert_location requires at least one non-null field")
        existing = self.session.scalar(
            select(m.Location).filter_by(city=city, state=state, country=country, region=region)
        )
        if existing:
            return existing
        location = m.Location(city=city, state=state, country=country, region=region)
        self.session.add(location)
        if flush:
            self.session.flush()
        return location

    def upsert_product(
        self, source_product_id: str, name: str | None, category: str | None, flush: bool = True
    ) -> m.Product:
        existing = self.session.scalar(
            select(m.Product).where(m.Product.source_product_id == source_product_id)
        )
        if existing:
            existing.name = name
            existing.category = category
            if flush:
                self.session.flush()
            return existing
        product = m.Product(source_product_id=source_product_id, name=name, category=category)
        self.session.add(product)
        if flush:
            self.session.flush()
        return product

    def upsert_supplier(
        self, source_supplier_id: str, name: str | None, location_id: int | None, flush: bool = True
    ) -> m.Supplier:
        existing = self.session.scalar(
            select(m.Supplier).where(m.Supplier.source_supplier_id == source_supplier_id)
        )
        if existing:
            existing.name = name
            existing.location_id = location_id
            if flush:
                self.session.flush()
            return existing
        supplier = m.Supplier(source_supplier_id=source_supplier_id, name=name, location_id=location_id)
        self.session.add(supplier)
        if flush:
            self.session.flush()
        return supplier

    def upsert_customer(
        self, source_customer_id: str, location_id: int | None, flush: bool = True
    ) -> m.Customer:
        existing = self.session.scalar(
            select(m.Customer).where(m.Customer.source_customer_id == source_customer_id)
        )
        if existing:
            existing.location_id = location_id
            if flush:
                self.session.flush()
            return existing
        customer = m.Customer(source_customer_id=source_customer_id, location_id=location_id)
        self.session.add(customer)
        if flush:
            self.session.flush()
        return customer


class OrderShipmentRepository(BaseRepository):
    def upsert_order(self, source_order_id: str, flush: bool = True, **values) -> m.Order:
        existing = self.session.scalar(select(m.Order).where(m.Order.source_order_id == source_order_id))
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            if flush:
                self.session.flush()
            return existing
        order = m.Order(source_order_id=source_order_id, **values)
        self.session.add(order)
        if flush:
            self.session.flush()
        return order

    def upsert_shipment(self, order_id: int, flush: bool = True, **values) -> m.Shipment:
        existing = self.session.scalar(select(m.Shipment).where(m.Shipment.order_id == order_id))
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            if flush:
                self.session.flush()
            return existing
        shipment = m.Shipment(order_id=order_id, **values)
        self.session.add(shipment)
        if flush:
            self.session.flush()
        return shipment


# ---------------------------------------------------------------------------
# Contextual metrics (SQL/Python only -- never delegated to the LLM)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OverallBaseline:
    shipment_count: int
    late_shipment_count: int
    late_rate: float | None
    average_delay_days: float | None  # average, among LATE shipments only
    median_delay_days: float | None  # median, among LATE shipments only
    delay_percentile: float | None  # this incident's delay percentile in the population


@dataclass(frozen=True)
class GroupPerformance:
    """Shared shape for supplier / region / shipping-mode performance."""

    label: str | None
    shipment_count: int | None
    late_shipment_count: int | None
    late_rate: float | None
    average_delay_days: float | None
    median_delay_days: float | None
    late_rate_ratio: float | None  # group late_rate / overall late_rate
    average_delay_ratio: float | None  # group avg delay / overall avg delay
    trend: str | None = None  # "improving" | "stable" | "deteriorating" | None


@dataclass(frozen=True)
class BusinessImpact:
    order_value: float | None
    quantity: int | None
    order_value_percentile: float | None


@dataclass(frozen=True)
class IncidentSignals:
    """Factual, derived signals -- never causal claims."""

    delay_vs_average: float | None
    supplier_performance: str | None  # "elevated" | "normal" | "below_average" | None
    regional_performance: str | None
    supplier_trend: str | None
    regional_trend: str | None


@dataclass(frozen=True)
class IncidentContextProfile:
    incident_type: str
    delay_days: int | None
    overall: OverallBaseline
    supplier: GroupPerformance | None
    region: GroupPerformance | None
    shipping_mode: GroupPerformance | None
    business_impact: BusinessImpact
    signals: IncidentSignals

    def to_dict(self) -> dict:
        return asdict(self)


class ContextualMetricsRepository(BaseRepository):
    """Builds an IncidentContextProfile using aggregate SQL queries only.

    Every group (overall/supplier/region/shipping-mode) costs a small,
    fixed number of aggregate queries -- never a per-row scan or an N+1
    pattern. Medians use an ORDER BY ... OFFSET/LIMIT to fetch only the
    1-2 middle rows rather than loading the population into Python; on
    very large tables without a supporting index this is still O(n) at
    the database layer, which is a known trade-off documented here rather
    than solved with an approximate estimator.
    """

    def build_profile(self, incident: m.Incident) -> IncidentContextProfile:
        shipment = incident.shipment
        order = shipment.order
        current_delay = shipment.delay_days or 0
        anchor_date = self._anchor_date()

        overall = self._overall_baseline(current_delay)

        supplier = None
        if order and order.supplier_id:
            supplier_name = order.supplier.name if order.supplier else None
            supplier = self._supplier_group(order.supplier_id, supplier_name, overall, anchor_date)

        region = None
        if order and order.customer and order.customer.location and order.customer.location.region:
            region_name = order.customer.location.region
            region = self._region_group(region_name, overall, anchor_date)

        shipping_mode = None
        if shipment.shipping_mode:
            shipping_mode = self._mode_group(shipment.shipping_mode, overall)

        business_impact = self._business_impact(order)
        signals = self._build_signals(current_delay, overall, supplier, region)

        return IncidentContextProfile(
            incident_type=incident.incident_type,
            delay_days=shipment.delay_days,
            overall=overall,
            supplier=supplier,
            region=region,
            shipping_mode=shipping_mode,
            business_impact=business_impact,
            signals=signals,
        )

    # -- overall -----------------------------------------------------------

    def _overall_baseline(self, current_delay: int) -> OverallBaseline:
        stmt = select(
            func.count(m.Shipment.id),
            func.sum(case((m.Shipment.delay_days > 0, 1), else_=0)),
            func.avg(case((m.Shipment.delay_days > 0, m.Shipment.delay_days))),
            func.sum(case((m.Shipment.delay_days <= current_delay, 1), else_=0)),
        ).where(m.Shipment.delay_days.is_not(None))
        total, late_count, avg_late_delay, at_or_below = self.session.execute(stmt).one()

        total = total or 0
        late_count = late_count or 0
        late_rate = (late_count / total) if total else None
        percentile = (at_or_below / total * 100) if total else None
        median = self._median_delay_among_late(where=[])

        return OverallBaseline(
            shipment_count=total,
            late_shipment_count=late_count,
            late_rate=self._round(late_rate),
            average_delay_days=self._round(avg_late_delay),
            median_delay_days=median,
            delay_percentile=self._round(percentile),
        )

    # -- supplier / region / shipping-mode groups ---------------------------

    def _supplier_group(
        self, supplier_id: int, supplier_name: str | None, overall: OverallBaseline, anchor_date: date | None
    ) -> GroupPerformance:
        where = [m.Order.supplier_id == supplier_id]
        stmt = (
            select(
                func.count(m.Shipment.id),
                func.sum(case((m.Shipment.delay_days > 0, 1), else_=0)),
                func.avg(case((m.Shipment.delay_days > 0, m.Shipment.delay_days))),
            )
            .select_from(m.Shipment)
            .join(m.Order, m.Order.id == m.Shipment.order_id)
            .where(m.Shipment.delay_days.is_not(None), *where)
        )
        total, late_count, avg_delay = self.session.execute(stmt).one()
        total = total or 0
        late_count = late_count or 0
        late_rate = (late_count / total) if total else None
        median = self._median_delay_among_late(where=where, join_order=True)
        trend = self._trend_generic(where, anchor_date, join_order=True)

        return GroupPerformance(
            label=supplier_name,
            shipment_count=total,
            late_shipment_count=late_count,
            late_rate=self._round(late_rate),
            average_delay_days=self._round(avg_delay),
            median_delay_days=median,
            late_rate_ratio=self._ratio(late_rate, overall.late_rate),
            average_delay_ratio=self._ratio(avg_delay, overall.average_delay_days),
            trend=trend,
        )

    def _region_group(
        self, region_name: str, overall: OverallBaseline, anchor_date: date | None
    ) -> GroupPerformance:
        where = [m.Location.region == region_name]
        stmt = (
            select(
                func.count(m.Shipment.id),
                func.sum(case((m.Shipment.delay_days > 0, 1), else_=0)),
                func.avg(case((m.Shipment.delay_days > 0, m.Shipment.delay_days))),
            )
            .select_from(m.Shipment)
            .join(m.Order, m.Order.id == m.Shipment.order_id)
            .join(m.Customer, m.Customer.id == m.Order.customer_id)
            .join(m.Location, m.Location.id == m.Customer.location_id)
            .where(m.Shipment.delay_days.is_not(None), *where)
        )
        total, late_count, avg_delay = self.session.execute(stmt).one()
        total = total or 0
        late_count = late_count or 0
        late_rate = (late_count / total) if total else None
        median = self._median_delay_among_late(where=where, join_customer_location=True)
        trend = self._trend_generic(where, anchor_date, join_customer_location=True)

        return GroupPerformance(
            label=region_name,
            shipment_count=total,
            late_shipment_count=late_count,
            late_rate=self._round(late_rate),
            average_delay_days=self._round(avg_delay),
            median_delay_days=median,
            late_rate_ratio=self._ratio(late_rate, overall.late_rate),
            average_delay_ratio=self._ratio(avg_delay, overall.average_delay_days),
            trend=trend,
        )

    def _mode_group(self, mode: str, overall: OverallBaseline) -> GroupPerformance:
        # Spec does not ask for a shipping-mode trend, only ratios.
        where = [m.Shipment.shipping_mode == mode]
        stmt = select(
            func.count(m.Shipment.id),
            func.sum(case((m.Shipment.delay_days > 0, 1), else_=0)),
            func.avg(case((m.Shipment.delay_days > 0, m.Shipment.delay_days))),
        ).where(m.Shipment.delay_days.is_not(None), *where)
        total, late_count, avg_delay = self.session.execute(stmt).one()
        total = total or 0
        late_count = late_count or 0
        late_rate = (late_count / total) if total else None
        median = self._median_delay_among_late(where=where)

        return GroupPerformance(
            label=mode,
            shipment_count=total,
            late_shipment_count=late_count,
            late_rate=self._round(late_rate),
            average_delay_days=self._round(avg_delay),
            median_delay_days=median,
            late_rate_ratio=self._ratio(late_rate, overall.late_rate),
            average_delay_ratio=self._ratio(avg_delay, overall.average_delay_days),
            trend=None,
        )

    # -- business impact -----------------------------------------------------

    def _business_impact(self, order: m.Order | None) -> BusinessImpact:
        if order is None or order.order_value is None:
            return BusinessImpact(
                order_value=float(order.order_value) if order and order.order_value is not None else None,
                quantity=order.quantity if order else None,
                order_value_percentile=None,
            )
        stmt = select(
            func.count(m.Order.id),
            func.sum(case((m.Order.order_value <= order.order_value, 1), else_=0)),
        ).where(m.Order.order_value.is_not(None))
        total, at_or_below = self.session.execute(stmt).one()
        percentile = self._round(at_or_below / total * 100) if total else None
        return BusinessImpact(
            order_value=float(order.order_value),
            quantity=order.quantity,
            order_value_percentile=percentile,
        )

    # -- signals --------------------------------------------------------------

    def _build_signals(
        self,
        current_delay: int,
        overall: OverallBaseline,
        supplier: GroupPerformance | None,
        region: GroupPerformance | None,
    ) -> IncidentSignals:
        delay_vs_average = self._ratio(current_delay, overall.average_delay_days)
        return IncidentSignals(
            delay_vs_average=delay_vs_average,
            supplier_performance=self._performance_label(supplier.late_rate_ratio) if supplier else None,
            regional_performance=self._performance_label(region.late_rate_ratio) if region else None,
            supplier_trend=supplier.trend if supplier else None,
            regional_trend=region.trend if region else None,
        )

    @staticmethod
    def _performance_label(ratio: float | None) -> str | None:
        if ratio is None:
            return None
        if ratio >= PERFORMANCE_ELEVATED_RATIO:
            return "elevated"
        if ratio <= PERFORMANCE_BELOW_AVERAGE_RATIO:
            return "below_average"
        return "normal"

    # -- presentation helpers (live queries, not part of the persisted ------
    # -- Gemini-facing profile -- for dashboard charts/comparisons) ---------

    def region_comparison(self, current_region: str | None, limit: int = 6) -> list[dict]:
        """Late rate per region, for the top `limit` regions by shipment
        volume, plus the overall network baseline as a synthetic row. Used
        to render a 'late rate by region' bar chart. Returns [] if the
        canonical schema has no region data at all."""
        stmt = (
            select(
                m.Location.region,
                func.count(m.Shipment.id),
                func.sum(case((m.Shipment.delay_days > 0, 1), else_=0)),
            )
            .select_from(m.Shipment)
            .join(m.Order, m.Order.id == m.Shipment.order_id)
            .join(m.Customer, m.Customer.id == m.Order.customer_id)
            .join(m.Location, m.Location.id == m.Customer.location_id)
            .where(m.Shipment.delay_days.is_not(None), m.Location.region.is_not(None))
            .group_by(m.Location.region)
            .order_by(func.count(m.Shipment.id).desc())
            .limit(limit)
        )
        rows = self.session.execute(stmt).all()
        if not rows:
            return []

        overall_total = sum(r[1] for r in rows)
        overall_late = sum(r[2] or 0 for r in rows)
        results = [
            {
                "region": region,
                "shipment_count": count,
                "late_rate": self._round((late or 0) / count) if count else None,
                "is_current": region == current_region,
            }
            for region, count, late in rows
        ]
        results.append({
            "region": "Network baseline",
            "shipment_count": overall_total,
            "late_rate": self._round(overall_late / overall_total) if overall_total else None,
            "is_current": False,
            "is_baseline": True,
        })
        return results

    def region_trend(self, region: str | None, weeks: int = 8) -> list[dict]:
        """Weekly late-rate buckets for one region over the last `weeks`
        weeks (anchored to the most recent data, not wall-clock time), for
        a trend line chart. Returns [] if region is None or there's no
        regional data."""
        if not region:
            return []
        anchor = self._anchor_date()
        if anchor is None:
            return []
        window_start = anchor - timedelta(weeks=weeks)

        stmt = (
            select(m.Shipment.expected_delivery_date, m.Shipment.delay_days)
            .select_from(m.Shipment)
            .join(m.Order, m.Order.id == m.Shipment.order_id)
            .join(m.Customer, m.Customer.id == m.Order.customer_id)
            .join(m.Location, m.Location.id == m.Customer.location_id)
            .where(
                m.Location.region == region,
                m.Shipment.delay_days.is_not(None),
                m.Shipment.expected_delivery_date > window_start,
                m.Shipment.expected_delivery_date <= anchor,
            )
        )
        rows = self.session.execute(stmt).all()
        if not rows:
            return []

        buckets: dict[int, list[int]] = {}
        for expected_date, delay_days in rows:
            week_index = (anchor - expected_date).days // 7
            buckets.setdefault(week_index, []).append(1 if delay_days > 0 else 0)

        series = []
        for week_index in sorted(buckets.keys(), reverse=True):  # oldest week first
            delays = buckets[week_index]
            week_end = anchor - timedelta(weeks=week_index)
            series.append({
                "week_ending": week_end.isoformat(),
                "shipment_count": len(delays),
                "late_rate": self._round(sum(delays) / len(delays)),
            })
        return series

    # -- trend ------------------------------------------------------------

    def _anchor_date(self) -> date | None:
        """Most recent expected_delivery_date observed in the data. Used
        as the reference point for recent-vs-previous trend windows so
        trend detection depends only on the data, not on wall-clock time."""
        return self.session.scalar(select(func.max(m.Shipment.expected_delivery_date)))

    def _trend_generic(
        self,
        base_where: list,
        anchor_date: date | None,
        join_order: bool = False,
        join_customer_location: bool = False,
    ) -> str | None:
        if anchor_date is None:
            return None
        recent_start, recent_end, prev_start, prev_end = self._windows(anchor_date)
        recent_total, recent_late = self._late_counts_for(
            base_where
            + [
                m.Shipment.expected_delivery_date > recent_start,
                m.Shipment.expected_delivery_date <= recent_end,
            ],
            join_order=join_order,
            join_customer_location=join_customer_location,
        )
        prev_total, prev_late = self._late_counts_for(
            base_where
            + [
                m.Shipment.expected_delivery_date > prev_start,
                m.Shipment.expected_delivery_date <= prev_end,
            ],
            join_order=join_order,
            join_customer_location=join_customer_location,
        )
        return self._classify_trend(recent_total, recent_late, prev_total, prev_late)

    @staticmethod
    def _windows(anchor_date: date, window_days: int = TREND_WINDOW_DAYS) -> tuple[date, date, date, date]:
        recent_end = anchor_date
        recent_start = anchor_date - timedelta(days=window_days)
        prev_end = recent_start
        prev_start = recent_start - timedelta(days=window_days)
        return recent_start, recent_end, prev_start, prev_end

    @staticmethod
    def _classify_trend(
        recent_total: int,
        recent_late: int,
        prev_total: int,
        prev_late: int,
        min_sample: int = TREND_MIN_SAMPLE_SIZE,
    ) -> str | None:
        if recent_total < min_sample or prev_total < min_sample:
            return None
        recent_rate = recent_late / recent_total
        prev_rate = prev_late / prev_total
        if prev_rate == 0:
            return "deteriorating" if recent_rate > 0 else "stable"
        relative_change = (recent_rate - prev_rate) / prev_rate
        if relative_change > TREND_RELATIVE_CHANGE_THRESHOLD:
            return "deteriorating"
        if relative_change < -TREND_RELATIVE_CHANGE_THRESHOLD:
            return "improving"
        return "stable"

    def _late_counts_for(
        self, where: list, join_order: bool = False, join_customer_location: bool = False
    ) -> tuple[int, int]:
        stmt = (
            select(func.count(), func.sum(case((m.Shipment.delay_days > 0, 1), else_=0)))
            .select_from(m.Shipment)
            .where(m.Shipment.delay_days.is_not(None), *where)
        )
        if join_order or join_customer_location:
            stmt = stmt.join(m.Order, m.Order.id == m.Shipment.order_id)
        if join_customer_location:
            stmt = stmt.join(m.Customer, m.Customer.id == m.Order.customer_id).join(
                m.Location, m.Location.id == m.Customer.location_id
            )
        total, late = self.session.execute(stmt).one()
        return total or 0, late or 0

    # -- median helper (OFFSET/LIMIT, never loads the full population) ------

    def _median_delay_among_late(
        self, where: list, join_order: bool = False, join_customer_location: bool = False
    ) -> float | None:
        count_stmt = select(func.count()).select_from(m.Shipment).where(m.Shipment.delay_days > 0, *where)
        if join_order or join_customer_location:
            count_stmt = count_stmt.join(m.Order, m.Order.id == m.Shipment.order_id)
        if join_customer_location:
            count_stmt = count_stmt.join(m.Customer, m.Customer.id == m.Order.customer_id).join(
                m.Location, m.Location.id == m.Customer.location_id
            )
        count = self.session.scalar(count_stmt)
        if not count:
            return None

        order_stmt = select(m.Shipment.delay_days).where(m.Shipment.delay_days > 0, *where)
        if join_order or join_customer_location:
            order_stmt = order_stmt.join(m.Order, m.Order.id == m.Shipment.order_id)
        if join_customer_location:
            order_stmt = order_stmt.join(m.Customer, m.Customer.id == m.Order.customer_id).join(
                m.Location, m.Location.id == m.Customer.location_id
            )
        order_stmt = order_stmt.order_by(m.Shipment.delay_days)

        if count % 2 == 1:
            mid = self.session.scalar(order_stmt.offset(count // 2).limit(1))
            return float(mid) if mid is not None else None
        pair = self.session.execute(order_stmt.offset(count // 2 - 1).limit(2)).scalars().all()
        if len(pair) < 2:
            return float(pair[0]) if pair else None
        return round((float(pair[0]) + float(pair[1])) / 2, 3)

    @staticmethod
    def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
        if numerator is None or denominator is None or denominator == 0:
            return None
        # Postgres returns AVG()/SUM() on integer/numeric columns as
        # decimal.Decimal, while SQLite (used in tests) returns float.
        # Decimal / float raises TypeError, so normalize both to float
        # before dividing regardless of which dialect produced them.
        return round(float(numerator) / float(denominator), 3)

    @staticmethod
    def _round(value) -> float | None:
        if value is None:
            return None
        return round(float(value), 3)


# ---------------------------------------------------------------------------
# Phase 1 state machine (Postgres-backed handoff)
# ---------------------------------------------------------------------------

class InvestigationRunRepository(BaseRepository):
    def create_run(self, incident_id: int) -> m.InvestigationRun:
        run = m.InvestigationRun(
            run_id=str(uuid4()),
            incident_id=incident_id,
            status=m.InvestigationRunStatus.PENDING,
            current_stage=m.InvestigationStage.INCIDENT_INTAKE,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def get_by_run_id(self, run_id: str) -> m.InvestigationRun | None:
        return self.session.scalar(
            select(m.InvestigationRun).where(m.InvestigationRun.run_id == run_id)
        )

    def get_latest_for_incidents(self, incident_ids: list[int]) -> dict[int, m.InvestigationRun]:
        """Most recent run per incident (by started_at), for the incidents
        list view so it can show 'already triaged' status and let the
        frontend reuse a completed run instead of re-invoking Gemini on
        every click. One query regardless of list size."""
        if not incident_ids:
            return {}
        rows = self.session.scalars(
            select(m.InvestigationRun)
            .where(m.InvestigationRun.incident_id.in_(incident_ids))
            .order_by(m.InvestigationRun.incident_id, m.InvestigationRun.started_at.desc())
        ).all()
        latest: dict[int, m.InvestigationRun] = {}
        for run in rows:
            if run.incident_id not in latest:
                latest[run.incident_id] = run
        return latest

    def mark_running(self, run_id: str) -> None:
        run = self.get_by_run_id(run_id)
        run.status = m.InvestigationRunStatus.RUNNING

    def set_contextual_metrics(self, run_id: str, metrics: dict) -> None:
        """Persist the metrics snapshot that was (or will be) shown to
        Gemini, so the frontend and any later processing can reuse it
        without recomputing, and so there's a historical record of what
        informed the triage even if live metrics later drift."""
        run = self.get_by_run_id(run_id)
        run.contextual_metrics = metrics

    def mark_completed(self, run_id: str) -> None:
        run = self.get_by_run_id(run_id)
        run.status = m.InvestigationRunStatus.COMPLETED
        run.current_stage = m.InvestigationStage.READY_FOR_HYPOTHESIS
        run.completed_at = datetime.now(timezone.utc)

    def mark_failed(self, run_id: str, error_message: str) -> None:
        run = self.get_by_run_id(run_id)
        run.status = m.InvestigationRunStatus.FAILED
        run.error_message = error_message[:4000]
        run.completed_at = datetime.now(timezone.utc)

    def insert_intake(
        self,
        run_id: str,
        incident_type: str,
        severity: m.IncidentSeverity,
        rationale: str,
        recommended_next_step: str,
        normalized_summary: str,
        triage_assessment: str,
        impact: m.IncidentImpact | None = None,
        priority: m.InvestigationPriority | None = None,
    ) -> m.IncidentIntake:
        intake = m.IncidentIntake(
            run_id=run_id,
            incident_type=incident_type,
            severity=severity,
            impact=impact,
            priority=priority,
            rationale=rationale,
            recommended_next_step=recommended_next_step,
            normalized_summary=normalized_summary,
            triage_assessment=triage_assessment,
        )
        self.session.add(intake)
        self.session.flush()
        return intake

    def get_intake(self, run_id: str) -> m.IncidentIntake | None:
        return self.session.scalar(
            select(m.IncidentIntake).where(m.IncidentIntake.run_id == run_id)
        )

    def mark_hypothesis_generated(self, run_id: str) -> None:
        """Phase 2 completion: advance the stage marker. Status stays
        COMPLETED (the run itself succeeded); current_stage is what tracks
        pipeline progress across phases."""
        run = self.get_by_run_id(run_id)
        run.current_stage = m.InvestigationStage.HYPOTHESIS_GENERATED

    def set_ruled_out_hypotheses(self, run_id: str, ruled_out: list[dict]) -> None:
        run = self.get_by_run_id(run_id)
        run.ruled_out_hypotheses = ruled_out


# ---------------------------------------------------------------------------
# Phase 2: hypothesis persistence
# ---------------------------------------------------------------------------

class HypothesisRepository(BaseRepository):
    def replace_hypotheses(
        self, run_id: str, hypotheses: list[dict]
    ) -> list[m.Hypothesis]:
        """Delete any existing hypotheses for this run and insert the new
        set. Re-running Phase 2 for the same run replaces its hypotheses
        rather than accumulating duplicates -- each run represents one
        Phase 2 pass, consistent with how Phase 1 handles re-triage (a
        fresh `investigation_runs` row, not appended state)."""
        self.session.execute(
            m.Hypothesis.__table__.delete().where(m.Hypothesis.run_id == run_id)
        )
        rows = []
        for rank, item in enumerate(hypotheses, start=1):
            hypothesis = m.Hypothesis(
                run_id=run_id,
                rank=rank,
                statement=item["statement"],
                rationale=item["rationale"],
                supporting_signals=item["supporting_signals"],
                confidence=m.HypothesisConfidence(item["confidence"]),
                what_would_confirm=item["what_would_confirm"],
                what_would_refute=item["what_would_refute"],
                why_ranked_here=item["why_ranked_here"],
            )
            self.session.add(hypothesis)
            rows.append(hypothesis)
        self.session.flush()
        return rows

    def get_by_run_id(self, run_id: str) -> list[m.Hypothesis]:
        return list(
            self.session.scalars(
                select(m.Hypothesis)
                .where(m.Hypothesis.run_id == run_id)
                .order_by(m.Hypothesis.rank)
            )
        )

    def get_similar_past_hypotheses(
        self,
        supplier_id: int | None,
        region: str | None,
        exclude_run_id: str,
        limit: int = 5,
    ) -> list[dict]:
        """Cross-incident memory: the top-ranked hypothesis from each of
        the most recent OTHER investigation runs sharing this incident's
        supplier and/or region, whose Phase 2 has already completed.
        Gives the Hypothesis Agent real historical context instead of
        reasoning about every incident in total isolation. Returns []
        when there's no supplier/region to match on, or no prior runs
        exist yet -- never fabricated."""
        if supplier_id is None and region is None:
            return []

        match_conditions = []
        if supplier_id is not None:
            match_conditions.append(m.Order.supplier_id == supplier_id)
        if region:
            match_conditions.append(m.Location.region == region)

        stmt = (
            select(
                m.InvestigationRun.run_id,
                m.Incident.id,
                m.IncidentIntake.incident_type,
                m.IncidentIntake.severity,
                m.Hypothesis.statement,
                m.Hypothesis.confidence,
                m.InvestigationRun.started_at,
            )
            .select_from(m.Hypothesis)
            .join(m.InvestigationRun, m.InvestigationRun.run_id == m.Hypothesis.run_id)
            .join(m.Incident, m.Incident.id == m.InvestigationRun.incident_id)
            .join(m.Shipment, m.Shipment.id == m.Incident.shipment_id)
            .join(m.Order, m.Order.id == m.Shipment.order_id)
            .outerjoin(m.Customer, m.Customer.id == m.Order.customer_id)
            .outerjoin(m.Location, m.Location.id == m.Customer.location_id)
            .outerjoin(m.IncidentIntake, m.IncidentIntake.run_id == m.InvestigationRun.run_id)
            .where(
                m.Hypothesis.rank == 1,
                m.InvestigationRun.run_id != exclude_run_id,
                m.InvestigationRun.current_stage == m.InvestigationStage.HYPOTHESIS_GENERATED,
                or_(*match_conditions),
            )
            .order_by(m.InvestigationRun.started_at.desc())
            .limit(limit)
        )
        rows = self.session.execute(stmt).all()
        return [
            {
                "run_id": row.run_id,
                "incident_id": row.id,
                "incident_type": row.incident_type,
                "severity": row.severity.value if row.severity else None,
                "top_hypothesis": row.statement,
                "confidence": row.confidence.value,
            }
            for row in rows
        ]