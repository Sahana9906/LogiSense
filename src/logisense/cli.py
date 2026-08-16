import typer
from alembic import command
from alembic.config import Config

from logisense.config import get_settings
from logisense.db import SessionLocal
from logisense.logging import configure_logging
from logisense.repositories import InvestigationRunRepository
from logisense.services.gemini_client import GeminiIntakeClient
from logisense.services.incident_detection import IncidentDetectionService
from logisense.services.ingestion.dataco_loader import DataCoLoader
from logisense.services.ingestion.olist_loader import OlistLoader
from logisense.services.intake import IncidentIntakeService

app = typer.Typer(help="LogiSense AI Phase 1 operational commands.")


@app.callback()
def main() -> None:
    configure_logging(get_settings().log_level)


@app.command("db-upgrade")
def db_upgrade() -> None:
    command.upgrade(Config("alembic.ini"), "head")


@app.command("load-olist")
def load_olist(
    data_dir: str = typer.Option(..., help="Directory containing the extracted Olist CSV files."),
) -> None:
    """Load the public Olist Brazilian E-Commerce dataset into the
    canonical schema. Does not detect incidents -- run detect-incidents
    afterward."""
    from pathlib import Path

    with SessionLocal() as session:
        result = OlistLoader(session, Path(data_dir)).load()
    typer.echo(
        f"order_items_read={result.order_items_read} "
        f"canonical_orders_upserted={result.canonical_orders_upserted} "
        f"shipments_upserted={result.shipments_upserted} "
        f"skipped_rows={result.skipped_rows}"
    )
    typer.echo(
        f"totals: locations={result.locations_total} products={result.products_total} "
        f"suppliers={result.suppliers_total} customers={result.customers_total}"
    )
    for warning in result.warnings[:20]:
        typer.echo(f"warning: {warning}")
    if len(result.warnings) > 20:
        typer.echo(f"... and {len(result.warnings) - 20} more warnings")


@app.command("load-dataco")
def load_dataco(
    csv_path: str = typer.Option(..., help="Path to DataCoSupplyChainDataset.csv"),
    batch_size: int = typer.Option(2000, min=100, help="Rows per commit batch."),
) -> None:
    """Load the DataCo Smart Supply Chain dataset into the canonical
    schema. Does not detect incidents -- run detect-incidents afterward."""
    from pathlib import Path

    with SessionLocal() as session:
        result = DataCoLoader(session, Path(csv_path), batch_size=batch_size).load()
    typer.echo(
        f"rows_read={result.rows_read} "
        f"canonical_orders_upserted={result.canonical_orders_upserted} "
        f"shipments_upserted={result.shipments_upserted} "
        f"skipped_rows={result.skipped_rows}"
    )
    typer.echo(
        f"totals: locations={result.locations_total} products={result.products_total} "
        f"customers={result.customers_total}"
    )
    for warning in result.warnings[:20]:
        typer.echo(f"warning: {warning}")
    if len(result.warnings) > 20:
        typer.echo(f"... and {len(result.warnings) - 20} more warnings")


@app.command("detect-incidents")
def detect_incidents(
    limit: int = typer.Option(1000, min=1, help="Max shipments to scan for new incidents."),
) -> None:
    """Scan the canonical shipments table and create incidents for real,
    data-derived delivery deviations. Never fabricates data."""
    with SessionLocal() as session:
        result = IncidentDetectionService(session).detect(limit=limit)
    typer.echo(
        f"Scanned {result.shipments_scanned} shipments, "
        f"created {result.incidents_created} incident(s)."
    )


@app.command("start-investigation")
def start_investigation(
    incident_id: int = typer.Option(..., min=1, help="Incident id from the incidents table."),
) -> None:
    settings = get_settings()
    with SessionLocal() as session:
        result = IncidentIntakeService(
            session=session,
            model_client=GeminiIntakeClient(settings),
        ).start_investigation(incident_id=incident_id)

        typer.echo(
            f"run_id={result.run_id} status={result.status} current_stage={result.current_stage}"
        )

        if result.status == "completed":
            intake = InvestigationRunRepository(session).get_intake(result.run_id)
            if intake is not None:
                typer.echo("")
                typer.echo(intake.triage_assessment)


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run("logisense.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
