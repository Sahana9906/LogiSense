# LogiSense AI — Phase 1 Architecture

## Scope

This build implements **only Phase 1: Incident Intake & Triage**, plus the
minimal prerequisite infrastructure the pipeline needs to reach it:

```
SUPPLY-CHAIN DATASET (not implemented here — selected separately)
        v
DATA INGESTION & NORMALIZATION (not implemented here)
        v
CANONICAL DATABASE               <-- this repo
        v
INCIDENT DETECTION                <-- this repo (generic, data-derived)
        v
PHASE 1 — INCIDENT INTAKE & TRIAGE  <-- this repo
        v
(Phase 2/3/4/5 — out of scope)
```

Any Phase 3 (RAG/evidence retrieval) or dataset-specific ETL code that
existed in earlier drafts of this project has been removed, since it
assumed a specific dataset's column layout and belongs to a phase that is
explicitly out of scope for this build.

## Canonical schema

The schema is dataset-agnostic. It never assumes a specific source
dataset's field names. Concepts: `locations`, `products`, `suppliers`,
`customers`, `orders`, `shipments`, `incidents`. Business fields are
optional throughout — a dataset that lacks a concept (e.g. no supplier)
simply leaves the corresponding foreign key NULL.

A future ingestion/normalization layer is responsible for mapping any
compatible dataset (Olist, DataCo, Walmart, etc.) into this schema. Phase 1
never touches raw dataset columns — only these canonical models.

## Incident detection

A generic rule, not tied to any dataset: a shipment whose
`actual_delivery_date` is later than its `expected_delivery_date` has
incurred a real, data-derived deviation, so an `incidents` row is created.
Severity is deliberately **not** decided at this stage.

## Phase 1: Incident Intake & Triage

Given an `incident_id`:

1. Retrieve canonical context (incident, shipment, order, product,
   supplier, customer, locations) — only fields that actually exist.
2. Compute contextual statistics purely in SQL/Python (never delegated to
   the LLM): overall/supplier/regional average delay, late-shipment rates.
3. Build a role + contextual + structured-output prompt for Gemini.
4. Call Gemini (Google GenAI SDK) with a strict JSON response schema.
5. Validate and decompose the result into typed columns. On invalid output,
   retry once with a correction prompt; if it fails again, mark the run
   `FAILED` with `error_message` set — never fabricate a result.
6. Persist `incident_intake` and update `investigation_runs`. PostgreSQL is
   the only Phase 1 -> Phase 2 handoff mechanism; no JSON file is written.

Phase 1 explicitly never determines root cause — only incident type,
severity, severity rationale, and a normalized summary.

## API

- `POST /investigations` — `{"incident_id": ...}` -> runs Phase 1 end to
  end and returns the run's status/stage.
- `GET /investigations/{run_id}` — returns the persisted result (from
  Postgres), including operational/business context and the AI triage
  fields, structured for an investigation card in the UI.
