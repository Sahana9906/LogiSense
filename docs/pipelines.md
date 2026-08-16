# Pipelines

## Data ingestion

`logisense load-olist --data-dir /path/to/olist/csvs` and
`logisense load-dataco --csv-path /path/to/DataCoSupplyChainDataset.csv`
map their respective public datasets into the canonical schema. All
dataset-specific knowledge (file/column names, granularity quirks,
derived-field logic) lives entirely in `services/ingestion/olist_loader.py`
/ `dataco_loader.py` -- Phase 1 and incident detection never see it. See
each module's docstring for its documented mapping assumptions:

- **Olist**: each order *item* becomes one canonical `Order` (Olist orders
  can span multiple products/sellers, canonical `Order` is 1:1:1).
  `Location.region` is derived from a static Brazilian state -> IBGE
  region table.
- **DataCo**: no supplier/seller concept exists in the raw data at all, so
  `Order.supplier_id` stays NULL for every row. There's no explicit
  expected/actual delivery date either -- both are derived from
  `shipping date + Days for shipment/shipping`, the same way the
  dataset's own `Late_delivery_risk` column is computed.

Run `logisense detect-incidents` afterward in both cases -- loaders only
populate dates, they never compute delay or create incidents themselves.

Throughput: both loaders are simple row-by-row upserts (~500-700 rows/sec
in testing, CPU-bound rather than disk-bound). For DataCo's ~180K rows
that's roughly 5-6 minutes -- fine for a one-time batch load, but not
tuned for repeated large reloads. A future optimization would batch
inserts with `bulk_insert_mappings`/COPY instead of per-row ORM upserts.

## Incident detection (`logisense detect-incidents`)

Scans `shipments` where both `expected_delivery_date` and
`actual_delivery_date` are populated, computes `delay_days`, and creates an
`incidents` row (`incident_type=delivery_delay`) for any positive delay
that doesn't already have one. Purely data-derived; no fabricated rows.

## Phase 1 (`logisense start-investigation --incident-id ...` or
`POST /investigations`)

1. Create an `investigation_runs` row (`status=pending`).
2. Mark it `running`.
3. Retrieve canonical context.
4. Build a contextual metrics profile (`ContextualMetricsRepository`) —
   overall/supplier/regional/shipping-mode comparisons, business impact,
   trend, and anomaly signals — computed entirely in SQL/Python.
5. Call Gemini with a structured-output schema; Gemini interprets the
   metrics, it never computes them.
6. Validate; on failure, retry once with a correction prompt.
7. On success: insert `incident_intake` (incident_type, severity, impact,
   priority, rationale, recommended_next_step, normalized_summary), mark
   the run `completed` and `current_stage=ready_for_hypothesis`.
8. On failure: mark the run `failed` with `error_message` set.

The same incident can be investigated multiple times — each call creates a
new `investigation_runs` row with a fresh `run_id`.

### Contextual metrics engine

`ContextualMetricsRepository.build_profile(incident)` computes, using
aggregate SQL only (no N+1, no full-table loads):

- **Overall baseline** — shipment count, late count/rate, average and
  median delay among late shipments, and this incident's delay percentile.
- **Supplier** — the same stats scoped to the incident's supplier, plus
  `late_rate_ratio` / `average_delay_ratio` against the overall baseline,
  plus a recent-vs-previous-30-day trend (`improving` / `stable` /
  `deteriorating`, or `None` if either window has fewer than 5 samples).
- **Region** — identical shape, scoped by the customer's location region.
- **Shipping mode** — same ratios, no trend (not requested).
- **Business impact** — order value, quantity, and order-value percentile.
- **Signals** — `delay_vs_average`, categorical `supplier_performance` /
  `regional_performance` labels (`elevated` / `normal` / `below_average`),
  and the two trend labels. These are descriptive signals, never causal
  claims, and never a severity determination by themselves.

Any metric that cannot be computed because the canonical schema lacks the
underlying data (no supplier, no region, no shipping mode, no order value,
too few historical samples) is `None` rather than fabricated.
