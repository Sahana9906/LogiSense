from datetime import date

import pytest

from logisense.repositories import ContextualMetricsRepository, IncidentRepository

from .factories import (
    make_customer,
    make_incident,
    make_location,
    make_order,
    make_shipment,
    make_supplier,
)


def _profile_for(session, incident):
    incident_with_context = IncidentRepository(session).get_with_context(incident.id)
    return ContextualMetricsRepository(session).build_profile(incident_with_context)


# ---------------------------------------------------------------------------
# 1. Overall baseline + percentile
# ---------------------------------------------------------------------------

def test_overall_baseline_average_median_and_percentile(session):
    delays = [0, 0, 0, 0, 0, 0, 2, 4, 6, 8]
    current_shipment = None
    for d in delays:
        order = make_order(session)
        shipment = make_shipment(session, order, delay_days=d)
        if d == 6:
            current_shipment = shipment

    incident = make_incident(session, current_shipment, deviation_value=6)
    profile = _profile_for(session, incident)

    assert profile.delay_days == 6
    assert profile.overall.shipment_count == 10
    assert profile.overall.late_shipment_count == 4
    assert profile.overall.late_rate == pytest.approx(0.4)
    assert profile.overall.average_delay_days == pytest.approx(5.0)
    assert profile.overall.median_delay_days == pytest.approx(5.0)
    assert profile.overall.delay_percentile == pytest.approx(90.0)

    # No supplier/region/mode attached to any of these shipments.
    assert profile.supplier is None
    assert profile.region is None
    assert profile.shipping_mode is None

    assert profile.signals.delay_vs_average == pytest.approx(6 / 5.0, abs=0.01)


# ---------------------------------------------------------------------------
# 2. Supplier metrics + ratios
# ---------------------------------------------------------------------------

def test_supplier_ratio_and_signal_label(session):
    supplier = make_supplier(session, name="Acme Logistics")
    for d in [0, 0, 5, 5, 10]:
        order = make_order(session, supplier=supplier)
        shipment = make_shipment(session, order, delay_days=d)
        if d == 5:
            incident_shipment = shipment  # reuse the last one with d == 5

    for d in [0, 0, 0, 0, 0]:
        order = make_order(session)  # no supplier -> unaffiliated baseline shipments
        make_shipment(session, order, delay_days=d)

    incident = make_incident(session, incident_shipment, deviation_value=5)
    profile = _profile_for(session, incident)

    assert profile.overall.shipment_count == 10
    assert profile.overall.late_rate == pytest.approx(0.3)

    assert profile.supplier is not None
    assert profile.supplier.label == "Acme Logistics"
    assert profile.supplier.shipment_count == 5
    assert profile.supplier.late_shipment_count == 3
    assert profile.supplier.late_rate == pytest.approx(0.6)
    assert profile.supplier.late_rate_ratio == pytest.approx(2.0, abs=0.01)
    assert profile.supplier.average_delay_ratio == pytest.approx(1.0, abs=0.01)

    assert profile.signals.supplier_performance == "elevated"


def test_zero_baseline_ratios_are_none_not_divide_by_zero(session):
    supplier = make_supplier(session, name="OnTime Co")
    for d in [0, 0, 0, 0, 0]:
        order = make_order(session, supplier=supplier)
        shipment = make_shipment(session, order, delay_days=d)
    for d in [0, 0, 0, 0, 0]:
        order = make_order(session)
        make_shipment(session, order, delay_days=d)

    incident = make_incident(session, shipment, deviation_value=0)
    profile = _profile_for(session, incident)

    assert profile.overall.late_rate == pytest.approx(0.0)
    assert profile.overall.average_delay_days is None  # AVG over empty (no late) set
    assert profile.supplier.late_rate_ratio is None
    assert profile.supplier.average_delay_ratio is None
    assert profile.signals.supplier_performance is None


def test_missing_supplier_is_omitted_safely(session):
    order = make_order(session)  # no supplier
    shipment = make_shipment(session, order, delay_days=3)
    incident = make_incident(session, shipment, deviation_value=3)

    profile = _profile_for(session, incident)

    assert profile.supplier is None
    assert profile.signals.supplier_performance is None
    assert profile.signals.supplier_trend is None


# ---------------------------------------------------------------------------
# 3. Regional metrics + trend
# ---------------------------------------------------------------------------

def test_region_trend_deteriorating(session):
    south = make_location(session, country="IN", region="South")
    previous_dates = [date(2026, 1, 5), date(2026, 1, 10), date(2026, 1, 15), date(2026, 1, 20), date(2026, 1, 25)]
    previous_delays = [0, 0, 0, 0, 5]  # late_rate = 0.2
    recent_dates = [date(2026, 2, 5), date(2026, 2, 10), date(2026, 2, 15), date(2026, 2, 20), date(2026, 3, 1)]
    recent_delays = [5, 5, 5, 5, 0]  # late_rate = 0.8

    anchor_shipment = None
    for d, delay in zip(previous_dates, previous_delays):
        customer = make_customer(session, location=south)
        order = make_order(session, customer=customer)
        make_shipment(session, order, expected_delivery_date=d, delay_days=delay)
    for d, delay in zip(recent_dates, recent_delays):
        customer = make_customer(session, location=south)
        order = make_order(session, customer=customer)
        shipment = make_shipment(session, order, expected_delivery_date=d, delay_days=delay)
        if d == date(2026, 3, 1):
            anchor_shipment = shipment

    incident = make_incident(session, anchor_shipment, deviation_value=0)
    profile = _profile_for(session, incident)

    assert profile.region is not None
    assert profile.region.label == "South"
    assert profile.region.shipment_count == 10
    assert profile.region.late_shipment_count == 5
    assert profile.region.late_rate == pytest.approx(0.5)
    assert profile.region.trend == "deteriorating"
    assert profile.signals.regional_trend == "deteriorating"


def test_region_trend_none_when_sample_too_small(session):
    south = make_location(session, country="IN", region="South")
    # Only 3 shipments per window -- below TREND_MIN_SAMPLE_SIZE (5).
    previous_dates = [date(2026, 1, 5), date(2026, 1, 15), date(2026, 1, 25)]
    recent_dates = [date(2026, 2, 5), date(2026, 2, 15), date(2026, 3, 1)]

    anchor_shipment = None
    for d in previous_dates:
        customer = make_customer(session, location=south)
        order = make_order(session, customer=customer)
        make_shipment(session, order, expected_delivery_date=d, delay_days=0)
    for d in recent_dates:
        customer = make_customer(session, location=south)
        order = make_order(session, customer=customer)
        shipment = make_shipment(session, order, expected_delivery_date=d, delay_days=5)
        if d == date(2026, 3, 1):
            anchor_shipment = shipment

    incident = make_incident(session, anchor_shipment, deviation_value=5)
    profile = _profile_for(session, incident)

    assert profile.region.trend is None


def test_missing_region_is_omitted_safely(session):
    customer = make_customer(session, location=None)  # no location at all
    order = make_order(session, customer=customer)
    shipment = make_shipment(session, order, delay_days=3)
    incident = make_incident(session, shipment, deviation_value=3)

    profile = _profile_for(session, incident)

    assert profile.region is None
    assert profile.signals.regional_performance is None
    assert profile.signals.regional_trend is None


# ---------------------------------------------------------------------------
# 4. Shipping-mode metrics
# ---------------------------------------------------------------------------

def test_shipping_mode_ratio_and_no_trend(session):
    incident_shipment = None
    for d in [0, 0, 5, 5, 10]:
        order = make_order(session)
        shipment = make_shipment(session, order, shipping_mode="Road", delay_days=d)
        if d == 5:
            incident_shipment = shipment
    for d in [0, 0, 0, 0, 0]:
        order = make_order(session)
        make_shipment(session, order, shipping_mode="Air", delay_days=d)

    incident = make_incident(session, incident_shipment, deviation_value=5)
    profile = _profile_for(session, incident)

    assert profile.shipping_mode is not None
    assert profile.shipping_mode.label == "Road"
    assert profile.shipping_mode.shipment_count == 5
    assert profile.shipping_mode.late_shipment_count == 3
    assert profile.shipping_mode.late_rate_ratio == pytest.approx(2.0, abs=0.01)
    assert profile.shipping_mode.trend is None  # not specified for shipping mode


def test_missing_shipping_mode_is_omitted_safely(session):
    order = make_order(session)
    shipment = make_shipment(session, order, shipping_mode=None, delay_days=3)
    incident = make_incident(session, shipment, deviation_value=3)

    profile = _profile_for(session, incident)

    assert profile.shipping_mode is None


# ---------------------------------------------------------------------------
# 5. Business impact / percentile
# ---------------------------------------------------------------------------

def test_order_value_percentile(session):
    for value in [100, 200, 300, 400, 500, 600, 800, 900, 1000]:
        make_order(session, order_value=value)  # no shipment needed for percentile calc

    target_order = make_order(session, order_value=700, quantity=42)
    shipment = make_shipment(session, target_order, delay_days=1)
    incident = make_incident(session, shipment, deviation_value=1)

    profile = _profile_for(session, incident)

    assert profile.business_impact.order_value == pytest.approx(700.0)
    assert profile.business_impact.quantity == 42
    # 7 of 10 order values (including itself) are <= 700 -> 70th percentile.
    assert profile.business_impact.order_value_percentile == pytest.approx(70.0)


def test_missing_order_value_is_omitted_safely(session):
    order = make_order(session, order_value=None, quantity=5)
    shipment = make_shipment(session, order, delay_days=1)
    incident = make_incident(session, shipment, deviation_value=1)

    profile = _profile_for(session, incident)

    assert profile.business_impact.order_value is None
    assert profile.business_impact.order_value_percentile is None
    assert profile.business_impact.quantity == 5
