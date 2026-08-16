from datetime import date

import pytest

from logisense import models as m
from logisense.repositories import ContextualMetricsRepository, IncidentRepository

from .factories import make_customer, make_incident, make_location, make_order, make_shipment


def test_region_comparison_includes_baseline_and_flags_current(session):
    south = make_location(session, country="IN", region="South")
    north = make_location(session, country="IN", region="North")

    for loc, delays in [(south, [0, 0, 5, 5, 5]), (north, [0, 0, 0, 0, 5])]:
        for d in delays:
            customer = make_customer(session, location=loc)
            order = make_order(session, customer=customer)
            make_shipment(session, order, delay_days=d)

    rows = ContextualMetricsRepository(session).region_comparison(current_region="South")
    by_region = {r["region"]: r for r in rows}

    assert by_region["South"]["shipment_count"] == 5
    assert by_region["South"]["late_rate"] == pytest.approx(0.6)
    assert by_region["South"]["is_current"] is True
    assert by_region["North"]["is_current"] is False
    assert "Network baseline" in by_region
    assert by_region["Network baseline"]["shipment_count"] == 10


def test_region_comparison_empty_when_no_region_data(session):
    order = make_order(session)
    make_shipment(session, order, delay_days=1)
    rows = ContextualMetricsRepository(session).region_comparison(current_region=None)
    assert rows == []


def test_region_trend_buckets_by_week(session):
    south = make_location(session, country="IN", region="South")
    anchor = date(2026, 3, 1)

    # Week 0 (most recent): 2 of 2 late. Week 1: 0 of 2 late.
    dates_and_delays = [
        (date(2026, 2, 28), 5), (date(2026, 3, 1), 5),   # week 0
        (date(2026, 2, 21), 0), (date(2026, 2, 22), 0),  # week 1
    ]
    for d, delay in dates_and_delays:
        customer = make_customer(session, location=south)
        order = make_order(session, customer=customer)
        make_shipment(session, order, expected_delivery_date=d, delay_days=delay)

    series = ContextualMetricsRepository(session).region_trend("South", weeks=4)

    assert len(series) == 2
    # chronological order: oldest week first
    assert series[0]["late_rate"] == pytest.approx(0.0)
    assert series[1]["late_rate"] == pytest.approx(1.0)


def test_region_trend_empty_when_region_missing(session):
    assert ContextualMetricsRepository(session).region_trend(None) == []
    assert ContextualMetricsRepository(session).region_trend("Nowhere") == []


def test_contextual_metrics_snapshot_is_persisted_on_run(session):
    from logisense.repositories import InvestigationRunRepository

    order = make_order(session)
    shipment = make_shipment(session, order, delay_days=3)
    incident = make_incident(session, shipment, deviation_value=3)

    runs = InvestigationRunRepository(session)
    run = runs.create_run(incident_id=incident.id)

    incident_ctx = IncidentRepository(session).get_with_context(incident.id)
    profile = ContextualMetricsRepository(session).build_profile(incident_ctx)
    runs.set_contextual_metrics(run.run_id, profile.to_dict())
    session.commit()

    reloaded = runs.get_by_run_id(run.run_id)
    assert reloaded.contextual_metrics is not None
    assert reloaded.contextual_metrics["delay_days"] == 3
    assert "overall" in reloaded.contextual_metrics
