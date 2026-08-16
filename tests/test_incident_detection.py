from datetime import date

from logisense import models as m
from logisense.services.incident_detection import IncidentDetectionService

from .factories import make_order, make_shipment


def test_on_time_shipment_creates_no_incident(session):
    order = make_order(session)
    make_shipment(
        session, order, expected_delivery_date=date(2026, 1, 10), actual_delivery_date=date(2026, 1, 10)
    )

    result = IncidentDetectionService(session).detect()

    assert result.shipments_scanned == 1
    assert result.incidents_created == 0
    assert session.query(m.Incident).count() == 0


def test_late_shipment_creates_incident_with_correct_delay(session):
    order = make_order(session)
    make_shipment(
        session, order, expected_delivery_date=date(2026, 1, 1), actual_delivery_date=date(2026, 1, 8)
    )

    result = IncidentDetectionService(session).detect()

    assert result.incidents_created == 1
    incident = session.query(m.Incident).one()
    assert incident.incident_type == "delivery_delay"
    assert incident.deviation_value == 7


def test_early_delivery_creates_no_incident(session):
    order = make_order(session)
    make_shipment(
        session, order, expected_delivery_date=date(2026, 1, 10), actual_delivery_date=date(2026, 1, 5)
    )

    result = IncidentDetectionService(session).detect()

    assert result.incidents_created == 0
    assert session.query(m.Incident).count() == 0


def test_running_detection_twice_does_not_duplicate_incident(session):
    order = make_order(session)
    make_shipment(
        session, order, expected_delivery_date=date(2026, 1, 1), actual_delivery_date=date(2026, 1, 8)
    )
    svc = IncidentDetectionService(session)

    first = svc.detect()
    second = svc.detect()

    assert first.incidents_created == 1
    assert second.incidents_created == 0
    assert session.query(m.Incident).count() == 1


def test_shipment_missing_dates_is_skipped(session):
    order = make_order(session)
    make_shipment(session, order, expected_delivery_date=None, actual_delivery_date=None)

    result = IncidentDetectionService(session).detect()

    assert result.shipments_scanned == 0
    assert result.incidents_created == 0
