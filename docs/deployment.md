# Deployment Guide

Run PostgreSQL as a managed service or container with daily backups and
migration access limited to deployment automation.

Recommended deployment steps:

1. Build and publish an application image with this package installed.
2. Inject environment variables from a secret manager: `DATABASE_URL`,
   `GEMINI_API_KEY`, `GEMINI_MODEL`.
3. Run `logisense db-upgrade` as a release task.
4. Run `logisense load-olist --data-dir ...` (or an equivalent loader for
   another dataset, owned by its own module under `services/ingestion/`)
   to populate the canonical schema.
5. Run `logisense detect-incidents` on a schedule (or after each ingestion
   run) to create data-derived incidents.
6. Run `logisense serve` (or `uvicorn logisense.api:app`) behind an
   authenticated internal gateway.

Operational checks should monitor: incidents created per run, investigation
run failure rate, `error_message` contents on failed runs, Gemini latency,
and structured-output validation failure rate (first-call vs. after the
one-shot correction retry).
