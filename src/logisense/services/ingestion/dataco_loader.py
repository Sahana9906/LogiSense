"""DataCo Smart Supply Chain ingestion loader.

Maps the single flat DataCo CSV into the canonical schema. All DataCo
column-name knowledge lives in this module -- Phase 1 and incident
detection never see it.

Mapping notes / assumptions (documented since they are judgment calls):

- Granularity: each CSV row is already one order *item* (there's an
  `Order Item Id` distinct from `Order Id`; an order can span several
  rows). Same approach as the Olist loader: each row becomes one
  canonical Order, `source_order_id = "{Order Id}:{Order Item Id}"`.
- DataCo has no supplier/seller concept at all (no seller/vendor column
  anywhere in the schema) -- Order.supplier_id stays NULL for every row.
  Supplier-based contextual metrics will simply read as "unavailable" for
  this dataset, which is the correct behaviour for an optional concept.
- DataCo has no explicit expected/actual delivery date columns. It does
  have `shipping date (DateOrders)` (when the order shipped),
  `Days for shipment (scheduled)`, and `Days for shipping (real)`. This is
  exactly how the dataset's own `Late_delivery_risk` column is derived, so
  the same derivation is used here:
      expected_delivery_date = shipping_date + Days for shipment (scheduled)
      actual_delivery_date   = shipping_date + Days for shipping (real)
- Two distinct location concepts exist in the raw data: `Customer
  City/State/Country` (the customer's registered address) and `Order
  City/State/Country/Region` (that specific order's destination). These
  are loaded as two separate canonical Locations -- Customer.location and
  Shipment.destination -- rather than collapsed into one, since they can
  differ. `Order Region` is used directly as Location.region.
- There is no warehouse/origin field, so Shipment.origin_location_id stays
  NULL. freight_cost also stays NULL (no separate freight column; `Sales`/
  `Order Item Total` already represent the item's price, not a cost
  breakdown that would let freight be isolated without guessing).
- A handful of rows in the public release contain corrupted characters in
  free-text fields (e.g. a mis-encoded Indian state name) -- these are
  passed through as-is rather than "corrected", since guessing the
  original value would be fabrication.
- Rows missing an Order Id or Order Item Id are skipped and counted.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from logisense import models as m
from logisense.repositories import DimensionRepository, OrderShipmentRepository

DATE_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y")


@dataclass
class DataCoLoadResult:
    rows_read: int = 0
    canonical_orders_upserted: int = 0
    shipments_upserted: int = 0
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    locations_total: int = 0
    products_total: int = 0
    customers_total: int = 0


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_datetime(value: str | None) -> datetime | None:
    value = _clean(value)
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_decimal(value: str | None) -> Decimal | None:
    value = _clean(value)
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _parse_int(value: str | None) -> int | None:
    value = _clean(value)
    if not value:
        return None
    try:
        return int(round(float(value)))
    except ValueError:
        return None


class DataCoLoader:
    def __init__(self, session: Session, csv_path: Path, batch_size: int = 2000) -> None:
        self.session = session
        self.csv_path = Path(csv_path)
        self.batch_size = batch_size
        self.dimensions = DimensionRepository(session)
        self.order_shipments = OrderShipmentRepository(session)

        self._location_cache: dict[tuple, object] = {}
        self._product_cache: dict[str, object] = {}
        self._customer_cache: dict[str, object] = {}

    def load(self) -> DataCoLoadResult:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"DataCo CSV not found: {self.csv_path}")

        result = DataCoLoadResult()
        with self.csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader, start=1):
                result.rows_read += 1
                order_id = _clean(row.get("Order Id"))
                item_id = _clean(row.get("Order Item Id"))
                if not order_id or not item_id:
                    result.skipped_rows += 1
                    if len(result.warnings) < 50:
                        result.warnings.append(f"row {i}: missing Order Id or Order Item Id")
                    continue

                self._load_row(row)

                if i % self.batch_size == 0:
                    self.session.commit()

        self.session.commit()
        result.canonical_orders_upserted = result.rows_read - result.skipped_rows
        result.shipments_upserted = result.canonical_orders_upserted
        result.locations_total = self.session.scalar(select(func.count(m.Location.id))) or 0
        result.products_total = self.session.scalar(select(func.count(m.Product.id))) or 0
        result.customers_total = self.session.scalar(select(func.count(m.Customer.id))) or 0
        return result

    # -- per-row processing ---------------------------------------------

    def _load_row(self, row: dict) -> None:
        order_id = _clean(row.get("Order Id"))
        item_id = _clean(row.get("Order Item Id"))
        if not order_id or not item_id:
            raise ValueError("missing Order Id or Order Item Id")

        customer_location = self._get_or_create_location(
            row.get("Customer City"), row.get("Customer State"), row.get("Customer Country"), region=None
        )
        destination_location = self._get_or_create_location(
            row.get("Order City"), row.get("Order State"), row.get("Order Country"),
            region=_clean(row.get("Order Region")),
        )

        customer = None
        customer_source_id = _clean(row.get("Customer Id"))
        if customer_source_id:
            customer = self._get_or_create_customer(customer_source_id, customer_location)

        product = None
        product_source_id = _clean(row.get("Product Card Id"))
        if product_source_id:
            product = self._get_or_create_product(
                product_source_id, _clean(row.get("Product Name")), _clean(row.get("Category Name"))
            )

        # One round-trip for every new/changed dimension row touched above,
        # instead of one round-trip per field.
        self.session.flush()

        order_date = _parse_datetime(row.get("order date (DateOrders)"))
        order = self.order_shipments.upsert_order(
            source_order_id=f"{order_id}:{item_id}",
            order_date=order_date.date() if order_date else None,
            order_value=_parse_decimal(row.get("Order Item Total")),
            quantity=_parse_int(row.get("Order Item Quantity")),
            status=_clean(row.get("Order Status")),
            customer_id=customer.id if customer else None,
            product_id=product.id if product else None,
            supplier_id=None,  # DataCo has no supplier/seller concept
            flush=False,
        )
        self.session.flush()  # populate order.id before the shipment FK needs it

        shipping_dt = _parse_datetime(row.get("shipping date (DateOrders)"))
        scheduled_days = _parse_int(row.get("Days for shipment (scheduled)"))
        real_days = _parse_int(row.get("Days for shipping (real)"))
        expected_delivery_date = (
            (shipping_dt + timedelta(days=scheduled_days)).date()
            if shipping_dt and scheduled_days is not None
            else None
        )
        actual_delivery_date = (
            (shipping_dt + timedelta(days=real_days)).date()
            if shipping_dt and real_days is not None
            else None
        )

        self.order_shipments.upsert_shipment(
            order_id=order.id,
            origin_location_id=None,  # no warehouse/origin field in DataCo
            destination_location_id=destination_location.id if destination_location else None,
            shipping_mode=_clean(row.get("Shipping Mode")),
            freight_cost=None,  # not present in DataCo
            expected_delivery_date=expected_delivery_date,
            actual_delivery_date=actual_delivery_date,
            delay_days=None,  # computed later by IncidentDetectionService
            flush=False,
        )

    # -- dimension helpers (memoized within one load() call) -------------

    def _get_or_create_location(self, city, state, country, region: str | None):
        city, state, country = _clean(city), _clean(state), _clean(country)
        if not any([city, state, country, region]):
            return None
        key = (city, state, country, region)
        if key not in self._location_cache:
            self._location_cache[key] = self.dimensions.upsert_location(
                city=city, state=state, country=country, region=region, flush=False
            )
        return self._location_cache[key]

    def _get_or_create_customer(self, source_customer_id: str, location):
        if source_customer_id not in self._customer_cache:
            self._customer_cache[source_customer_id] = self.dimensions.upsert_customer(
                source_customer_id=source_customer_id,
                location_id=location.id if location else None,
                flush=False,
            )
        return self._customer_cache[source_customer_id]

    def _get_or_create_product(self, source_product_id: str, name: str | None, category: str | None):
        if source_product_id not in self._product_cache:
            self._product_cache[source_product_id] = self.dimensions.upsert_product(
                source_product_id=source_product_id, name=name, category=category, flush=False
            )
        return self._product_cache[source_product_id]
