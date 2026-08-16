from decimal import Decimal
from pathlib import Path

from logisense import models as m
from logisense.services.incident_detection import IncidentDetectionService
from logisense.services.ingestion.dataco_loader import DataCoLoader

FIXTURE = Path(__file__).parent / "fixtures" / "dataco_sample.csv"


def test_load_creates_canonical_rows_with_derived_delivery_dates(session):
    result = DataCoLoader(session, FIXTURE).load()

    assert result.rows_read == 4
    assert result.canonical_orders_upserted == 4
    assert result.skipped_rows == 0

    order1 = session.query(m.Order).filter_by(source_order_id="77202:180517").one()
    assert order1.order_value == Decimal("314.64")
    assert order1.quantity == 1
    assert order1.status == "COMPLETE"
    assert order1.supplier_id is None  # DataCo has no supplier concept

    shipment1 = session.query(m.Shipment).filter_by(order_id=order1.id).one()
    # shipping date 2/3/2018 + scheduled 4 days -> expected 2/7/2018
    assert shipment1.expected_delivery_date.isoformat() == "2018-02-07"
    # shipping date 2/3/2018 + real 3 days -> actual 2/6/2018 (arrived early)
    assert shipment1.actual_delivery_date.isoformat() == "2018-02-06"
    assert shipment1.shipping_mode == "Standard Class"
    assert shipment1.origin_location_id is None  # no warehouse field in DataCo
    assert shipment1.freight_cost is None  # not present in DataCo

    assert shipment1.destination.region == "Southeast Asia"
    assert shipment1.destination.city == "Bekasi"

    customer1 = order1.customer
    assert customer1.location.city == "Caguas"
    assert customer1.location.country == "Puerto Rico"


def test_late_order_derives_positive_delay(session):
    DataCoLoader(session, FIXTURE).load()
    order2 = session.query(m.Order).filter_by(source_order_id="75939:179254").one()
    shipment2 = session.query(m.Shipment).filter_by(order_id=order2.id).one()
    # shipping date 1/18 + scheduled 4 -> expected 1/22; + real 5 -> actual 1/23
    assert shipment2.expected_delivery_date.isoformat() == "2018-01-22"
    assert shipment2.actual_delivery_date.isoformat() == "2018-01-23"


def test_missing_shipping_date_yields_null_delivery_dates_not_fabricated(session):
    DataCoLoader(session, FIXTURE).load()
    order4 = session.query(m.Order).filter_by(source_order_id="77301:180601").one()
    shipment4 = session.query(m.Shipment).filter_by(order_id=order4.id).one()
    assert shipment4.expected_delivery_date is None
    assert shipment4.actual_delivery_date is None
    assert shipment4.destination is not None  # Order Country was still present
    assert shipment4.destination.city is None


def test_loaded_data_flows_into_incident_detection(session):
    DataCoLoader(session, FIXTURE).load()
    result = IncidentDetectionService(session).detect()

    # Only order 75939:179254 (1 day late) qualifies; order 77202:180517
    # arrived early, order 77300:180600 on time, order 77301:180601 has no
    # delivery dates at all.
    assert result.incidents_created == 1
    incident = session.query(m.Incident).one()
    assert incident.deviation_value == 1


def test_running_load_twice_is_idempotent(session):
    loader = DataCoLoader(session, FIXTURE)
    first = loader.load()
    second = DataCoLoader(session, FIXTURE).load()

    assert session.query(m.Order).count() == first.canonical_orders_upserted
    assert second.canonical_orders_upserted == first.canonical_orders_upserted
    assert second.customers_total == first.customers_total
