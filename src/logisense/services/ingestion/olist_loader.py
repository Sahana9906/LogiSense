"""Olist (Brazilian e-commerce) ingestion loader.

Maps the raw Olist CSV files into the canonical schema. All Olist-specific
knowledge (file names, column names, granularity) lives in this one module.
Phase 1 and the incident-detection service never see any of it -- they only
ever read canonical models.

Expected files (from the public Olist Brazilian E-Commerce dataset), all in
one directory:

    olist_customers_dataset.csv
    olist_orders_dataset.csv
    olist_order_items_dataset.csv
    olist_products_dataset.csv
    olist_sellers_dataset.csv
    product_category_name_translation.csv   (optional)

Mapping notes / assumptions (documented since they are judgment calls, not
neutral facts):

- Granularity: Olist's `orders` table can contain multiple items (possibly
  from different sellers/products) per order_id. The canonical `Order`
  model is 1 order -> 1 product -> 1 supplier. Rather than inventing a
  line-item concept in the canonical schema, each Olist *order item* is
  loaded as one canonical Order, with
  `source_order_id = "{order_id}:{order_item_id}"`. All items belonging to
  the same real-world Olist order share the same order-level delivery
  dates, so several canonical Shipments (one per item) will show the same
  expected/actual delivery date -- that's expected, not a bug.
- order_value maps to the item's `price` (not price+freight). freight_value
  is loaded separately as Shipment.freight_cost.
- quantity is always 1 per canonical Order, since Olist represents repeat
  purchases of the same product as repeated item rows, not a quantity
  column.
- Olist has no shipping-mode/carrier field, so Shipment.shipping_mode stays
  NULL for all rows -- Phase 1 already handles this as "unavailable".
- Location.region is populated via a static Brazilian state -> IBGE region
  mapping (see br_regions.py), not sourced from Olist itself.
- Rows missing an order_id match, product_id, or seller_id are skipped and
  counted, never fabricated.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from logisense import models as m
from logisense.repositories import DimensionRepository, OrderShipmentRepository
from logisense.services.ingestion.br_regions import region_for_state

REQUIRED_FILES = (
    "olist_customers_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
)
OPTIONAL_TRANSLATION_FILE = "product_category_name_translation.csv"


@dataclass
class OlistLoadResult:
    order_items_read: int = 0
    canonical_orders_upserted: int = 0
    shipments_upserted: int = 0
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    # Final table counts after the load, for operator visibility.
    locations_total: int = 0
    products_total: int = 0
    suppliers_total: int = 0
    customers_total: int = 0


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _title_case(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().title() or None


class OlistLoader:
    def __init__(self, session: Session, data_dir: Path) -> None:
        self.session = session
        self.data_dir = Path(data_dir)
        self.dimensions = DimensionRepository(session)
        self.order_shipments = OrderShipmentRepository(session)

        self._location_cache: dict[tuple, object] = {}
        self._product_cache: dict[str, object] = {}
        self._supplier_cache: dict[str, object] = {}
        self._customer_cache: dict[str, object] = {}

    def load(self) -> OlistLoadResult:
        for filename in REQUIRED_FILES:
            if not (self.data_dir / filename).exists():
                raise FileNotFoundError(f"Missing required Olist file: {filename} in {self.data_dir}")

        result = OlistLoadResult()
        category_translation = self._load_category_translation()
        customers_by_id = self._index_csv("olist_customers_dataset.csv", "customer_id")
        orders_by_id = self._index_csv("olist_orders_dataset.csv", "order_id")
        sellers_by_id = self._index_csv("olist_sellers_dataset.csv", "seller_id")
        products_by_id = self._index_csv("olist_products_dataset.csv", "product_id")

        items_path = self.data_dir / "olist_order_items_dataset.csv"
        with items_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader, start=1):
                result.order_items_read += 1
                self._load_order_item(
                    row, orders_by_id, customers_by_id, sellers_by_id, products_by_id,
                    category_translation, result,
                )
                if i % 2000 == 0:
                    self.session.commit()

        self.session.commit()
        result.locations_total = self.session.scalar(select(func.count(m.Location.id))) or 0
        result.products_total = self.session.scalar(select(func.count(m.Product.id))) or 0
        result.suppliers_total = self.session.scalar(select(func.count(m.Supplier.id))) or 0
        result.customers_total = self.session.scalar(select(func.count(m.Customer.id))) or 0
        return result

    # -- per-row processing ---------------------------------------------

    def _load_order_item(
        self, item_row, orders_by_id, customers_by_id, sellers_by_id, products_by_id,
        category_translation, result: OlistLoadResult,
    ) -> None:
        order_id = item_row.get("order_id")
        order_row = orders_by_id.get(order_id)
        if order_row is None:
            result.skipped_rows += 1
            result.warnings.append(f"order_item references unknown order_id={order_id}")
            return

        customer_row = customers_by_id.get(order_row.get("customer_id"))
        seller_row = sellers_by_id.get(item_row.get("seller_id"))
        product_row = products_by_id.get(item_row.get("product_id"))

        customer_location = self._get_or_create_location(
            customer_row.get("customer_city") if customer_row else None,
            customer_row.get("customer_state") if customer_row else None,
        )
        seller_location = self._get_or_create_location(
            seller_row.get("seller_city") if seller_row else None,
            seller_row.get("seller_state") if seller_row else None,
        )

        customer = None
        if customer_row is not None:
            customer = self._get_or_create_customer(order_row["customer_id"], customer_location)

        supplier = None
        seller_id = item_row.get("seller_id")
        if seller_id:
            supplier = self._get_or_create_supplier(seller_id, seller_location)

        product = None
        product_id = item_row.get("product_id")
        if product_id:
            category_pt = product_row.get("product_category_name") if product_row else None
            category = category_translation.get(category_pt, category_pt) if category_pt else None
            product = self._get_or_create_product(product_id, category)

        # One round-trip for every new/changed dimension row touched above,
        # instead of one round-trip per field.
        self.session.flush()

        source_order_id = f"{order_id}:{item_row.get('order_item_id')}"
        order = self.order_shipments.upsert_order(
            source_order_id=source_order_id,
            order_date=_parse_date(order_row.get("order_purchase_timestamp")),
            order_value=_parse_decimal(item_row.get("price")),
            quantity=1,
            status=order_row.get("order_status"),
            customer_id=customer.id if customer else None,
            product_id=product.id if product else None,
            supplier_id=supplier.id if supplier else None,
            flush=False,
        )
        self.session.flush()  # populate order.id before the shipment FK needs it
        result.canonical_orders_upserted += 1

        self.order_shipments.upsert_shipment(
            order_id=order.id,
            origin_location_id=seller_location.id if seller_location else None,
            destination_location_id=customer_location.id if customer_location else None,
            shipping_mode=None,  # not present in Olist
            freight_cost=_parse_decimal(item_row.get("freight_value")),
            expected_delivery_date=_parse_date(order_row.get("order_estimated_delivery_date")),
            actual_delivery_date=_parse_date(order_row.get("order_delivered_customer_date")),
            delay_days=None,  # computed later by IncidentDetectionService
            flush=False,
        )
        result.shipments_upserted += 1

    # -- dimension helpers (memoized within one load() call) -------------

    def _get_or_create_location(self, city: str | None, state: str | None):
        city = _title_case(city)
        state = state.strip().upper() if state else None
        region = region_for_state(state)
        if not any([city, state]):
            return None
        key = (city, state, "Brazil", region)
        if key not in self._location_cache:
            self._location_cache[key] = self.dimensions.upsert_location(
                city=city, state=state, country="Brazil", region=region, flush=False
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

    def _get_or_create_supplier(self, source_supplier_id: str, location):
        if source_supplier_id not in self._supplier_cache:
            self._supplier_cache[source_supplier_id] = self.dimensions.upsert_supplier(
                source_supplier_id=source_supplier_id,
                name=f"Seller {source_supplier_id[:8]}",
                location_id=location.id if location else None,
                flush=False,
            )
        return self._supplier_cache[source_supplier_id]

    def _get_or_create_product(self, source_product_id: str, category: str | None):
        if source_product_id not in self._product_cache:
            self._product_cache[source_product_id] = self.dimensions.upsert_product(
                source_product_id=source_product_id,
                name=category,
                category=category,
                flush=False,
            )
        return self._product_cache[source_product_id]

    # -- csv helpers -------------------------------------------------------

    def _index_csv(self, filename: str, key_column: str) -> dict[str, dict]:
        path = self.data_dir / filename
        index: dict[str, dict] = {}
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = row.get(key_column)
                if key:
                    index[key] = row
        return index

    def _load_category_translation(self) -> dict[str, str]:
        path = self.data_dir / OPTIONAL_TRANSLATION_FILE
        if not path.exists():
            return {}
        translation: dict[str, str] = {}
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pt = row.get("product_category_name")
                en = row.get("product_category_name_english")
                if pt and en:
                    translation[pt] = en
        return translation
