from datetime import date

from fastapi.testclient import TestClient

from logisense import models as m
from logisense.api import app
from logisense.db import get_session
from logisense.repositories import (
    ContextualMetricsRepository,
    IncidentRepository,
    InvestigationRunRepository,
)

from .factories import make_customer, make_incident, make_location, make_order, make_shipment


def test_get_investigation_returns_dashboard_fields(session):
    south = make_location(session, country="IN", region="South")
    customer = make_customer(session, location=south)
    order = make_order(session, customer=customer, order_value=1000, quantity=3)
    shipment = make_shipment(
        session, order, expected_delivery_date=date(2026, 1, 1), delay_days=4, shipping_mode="Road"
    )
    incident = make_incident(session, shipment, deviation_value=4)

    runs = InvestigationRunRepository(session)
    run = runs.create_run(incident_id=incident.id)
    incident_ctx = IncidentRepository(session).get_with_context(incident.id)
    profile = ContextualMetricsRepository(session).build_profile(incident_ctx)
    runs.set_contextual_metrics(run.run_id, profile.to_dict())
    runs.insert_intake(
        run_id=run.run_id,
        incident_type="delivery_delay",
        severity=m.IncidentSeverity.HIGH,
        rationale="Significant deviation from baseline.",
        recommended_next_step="Proceed to hypothesis generation.",
        normalized_summary="The shipment was significantly delayed.",
        triage_assessment="INCIDENT TYPE\n\nDelivery Delay\n\nSEVERITY\n\nHigh",
    )
    runs.mark_completed(run.run_id)
    session.commit()

    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        resp = client.get(f"/investigations/{run.run_id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["severity"] == "high"
    assert body["impact"] is None  # no longer produced by the free-text triage prompt
    assert body["contextual_metrics"]["delay_days"] == 4
    assert body["contextual_metrics"]["overall"]["shipment_count"] >= 1
    assert isinstance(body["region_comparison"], list)
    assert isinstance(body["region_trend"], list)


def test_get_investigation_404_for_unknown_run(session):
    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        resp = client.get("/investigations/does-not-exist")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_list_incidents_returns_recent_incidents(session):
    south = make_location(session, country="IN", region="South")
    customer = make_customer(session, location=south)
    order = make_order(session, customer=customer)
    shipment = make_shipment(session, order, delay_days=5)
    make_incident(session, shipment, deviation_value=5)
    session.commit()

    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        resp = client.get("/incidents")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["incident_type"] == "delivery_delay"
    assert body[0]["region"] == "South"
    assert body[0]["latest_run_id"] is None
    assert body[0]["latest_status"] is None


def test_list_incidents_surfaces_latest_completed_run(session):
    order = make_order(session)
    shipment = make_shipment(session, order, delay_days=3)
    incident = make_incident(session, shipment, deviation_value=3)

    runs = InvestigationRunRepository(session)
    run = runs.create_run(incident_id=incident.id)
    runs.insert_intake(
        run_id=run.run_id,
        incident_type="delivery_delay",
        severity=m.IncidentSeverity.CRITICAL,
        rationale="Severe deviation.",
        recommended_next_step="Escalate immediately.",
        normalized_summary="Critical delay.",
        triage_assessment="INCIDENT TYPE\n\nDelivery Delay\n\nSEVERITY\n\nCritical",
    )
    runs.mark_completed(run.run_id)
    session.commit()

    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        resp = client.get("/incidents")
    finally:
        app.dependency_overrides.clear()

    body = resp.json()
    assert body[0]["latest_run_id"] == run.run_id
    assert body[0]["latest_status"] == "completed"
    assert body[0]["latest_severity"] == "critical"
