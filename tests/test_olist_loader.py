from pathlib import Path

from logisense import models as m
from logisense.services.incident_detection import IncidentDetectionService
from logisense.services.ingestion.br_regions import region_for_state
from logisense.services.ingestion.olist_loader import OlistLoader

FIXTURES = Path(__file__).parent / "fixtures" / "olist_sample"


def test_region_mapping_is_deterministic():
    assert region_for_state("SP") == "Sudeste"
    assert region_for_state("PR") == "Sul"
    assert region_for_state("AM") == "Norte"
    assert region_for_state(None) is None
    assert region_for_state("ZZ") is None


def test_load_creates_canonical_rows_and_skips_unknown_order(session):
    result = OlistLoader(session, FIXTURES).load()

    # order4 references an order_id absent from the orders file -> skipped.
    assert result.order_items_read == 4
    assert result.canonical_orders_upserted == 3
    assert result.skipped_rows == 1
    assert any("order_id=order4" in w for w in result.warnings)

    assert result.customers_total == 2
    assert result.suppliers_total == 1
    assert result.products_total == 2

    orders = {o.source_order_id: o for o in session.query(m.Order).all()}
    assert "order1:1" in orders
    order1 = orders["order1:1"]
    assert order1.order_value == 150
    assert order1.quantity == 1
    assert order1.status == "delivered"

    shipment1 = session.query(m.Shipment).filter_by(order_id=order1.id).one()
    assert shipment1.expected_delivery_date.isoformat() == "2017-05-08"
    assert shipment1.actual_delivery_date.isoformat() == "2017-05-12"
    assert shipment1.freight_cost == 15.50
    assert shipment1.shipping_mode is None  # not present in Olist

    customer1 = order1.customer
    assert customer1.location.city == "Sao Paulo"
    assert customer1.location.state == "SP"
    assert customer1.location.country == "Brazil"
    assert customer1.location.region == "Sudeste"

    supplier1 = order1.supplier
    assert supplier1.location.region == "Sul"  # Curitiba, PR


def test_undelivered_order_loads_with_null_actual_date_not_fabricated(session):
    OlistLoader(session, FIXTURES).load()
    order3 = session.query(m.Order).filter_by(source_order_id="order3:1").one()
    shipment3 = session.query(m.Shipment).filter_by(order_id=order3.id).one()
    assert shipment3.actual_delivery_date is None
    assert shipment3.expected_delivery_date is not None


def test_loaded_data_flows_into_incident_detection(session):
    OlistLoader(session, FIXTURES).load()
    result = IncidentDetectionService(session).detect()

    # order1 is 4 days late; order2 arrived early; order3 has no actual date yet.
    assert result.incidents_created == 1
    incident = session.query(m.Incident).one()
    assert incident.deviation_value == 4


def test_running_load_twice_is_idempotent(session):
    loader = OlistLoader(session, FIXTURES)
    first = loader.load()
    second = OlistLoader(session, FIXTURES).load()

    assert session.query(m.Order).count() == first.canonical_orders_upserted
    assert second.canonical_orders_upserted == first.canonical_orders_upserted
    assert second.customers_total == first.customers_total
    assert session.query(m.Location).count() > 0
