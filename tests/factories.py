from decimal import Decimal
from itertools import count

from logisense import models as m

_order_seq = count(1)


def make_location(session, city=None, state=None, country=None, region=None):
    loc = m.Location(city=city, state=state, country=country, region=region)
    session.add(loc)
    session.flush()
    return loc


def make_supplier(session, name="Supplier", location=None):
    supplier = m.Supplier(name=name, location_id=location.id if location else None)
    session.add(supplier)
    session.flush()
    return supplier


def make_customer(session, location=None):
    customer = m.Customer(location_id=location.id if location else None)
    session.add(customer)
    session.flush()
    return customer


def make_product(session, name="Product", category=None):
    product = m.Product(name=name, category=category)
    session.add(product)
    session.flush()
    return product


def make_order(session, customer=None, product=None, supplier=None, order_value=None, quantity=None):
    order = m.Order(
        source_order_id=f"ORD-{next(_order_seq)}",
        customer_id=customer.id if customer else None,
        product_id=product.id if product else None,
        supplier_id=supplier.id if supplier else None,
        order_value=Decimal(str(order_value)) if order_value is not None else None,
        quantity=quantity,
    )
    session.add(order)
    session.flush()
    return order


def make_shipment(
    session,
    order,
    expected_delivery_date=None,
    actual_delivery_date=None,
    shipping_mode=None,
    origin=None,
    destination=None,
    delay_days=None,
):
    shipment = m.Shipment(
        order_id=order.id,
        expected_delivery_date=expected_delivery_date,
        actual_delivery_date=actual_delivery_date,
        shipping_mode=shipping_mode,
        origin_location_id=origin.id if origin else None,
        destination_location_id=destination.id if destination else None,
        delay_days=delay_days,
    )
    session.add(shipment)
    session.flush()
    return shipment


def make_incident(session, shipment, incident_type="delivery_delay", deviation_value=None, rationale="test"):
    incident = m.Incident(
        shipment_id=shipment.id,
        incident_type=incident_type,
        deviation_value=deviation_value,
        rationale=rationale,
    )
    session.add(incident)
    session.flush()
    return incident
