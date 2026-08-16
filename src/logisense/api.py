from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from logisense import models as m
from logisense.config import get_settings
from logisense.db import get_session
from logisense.repositories import ContextualMetricsRepository, IncidentRepository, InvestigationRunRepository
from logisense.services.gemini_client import GeminiIntakeClient
from logisense.services.intake import IncidentIntakeService

app = FastAPI(title="LogiSense AI - Phase 1: Incident Intake & Triage", version="1.0.0")

# Local dev tool: the static frontend is opened as a file:// page or served
# from a different port, so allow it to call this API from any origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class InvestigationCreateRequest(BaseModel):
    incident_id: int = Field(gt=0)


class InvestigationCreateResponse(BaseModel):
    run_id: str
    incident_id: int
    status: str
    current_stage: str


class OperationalDetails(BaseModel):
    expected_delivery_date: date | None
    actual_delivery_date: date | None
    delay_days: int | None
    shipping_mode: str | None


class BusinessContext(BaseModel):
    order_value: float | None
    supplier: str | None
    region: str | None


class IncidentListItem(BaseModel):
    incident_id: int
    incident_type: str
    deviation_value: float | None
    detected_at: str
    region: str | None
    supplier: str | None
    latest_run_id: str | None = None
    latest_status: str | None = None
    latest_severity: str | None = None


class InvestigationDetailResponse(BaseModel):
    run_id: str
    incident_id: int
    status: str
    current_stage: str
    error_message: str | None = None

    operational_details: OperationalDetails
    business_context: BusinessContext

    incident_type: str | None = None
    severity: str | None = None
    impact: str | None = None
    priority: str | None = None
    rationale: str | None = None
    recommended_next_step: str | None = None
    normalized_summary: str | None = None

    # Dashboard data: the metrics snapshot Gemini actually saw (frozen at
    # triage time), plus two live comparison views for charts.
    contextual_metrics: dict | None = None
    region_comparison: list[dict] = Field(default_factory=list)
    region_trend: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/incidents", response_model=list[IncidentListItem])
def list_incidents(
    session: Annotated[Session, Depends(get_session)],
    limit: int = 50,
) -> list[IncidentListItem]:
    incidents = IncidentRepository(session).list_recent(limit=limit)
    latest_runs = InvestigationRunRepository(session).get_latest_for_incidents(
        [incident.id for incident in incidents]
    )
    runs_repo = InvestigationRunRepository(session)

    items = []
    for incident in incidents:
        order = incident.shipment.order
        customer = order.customer if order else None
        supplier = order.supplier if order else None
        region = customer.location.region if customer and customer.location else None

        latest_run = latest_runs.get(incident.id)
        latest_severity = None
        if latest_run and latest_run.status == m.InvestigationRunStatus.COMPLETED:
            intake = runs_repo.get_intake(latest_run.run_id)
            latest_severity = intake.severity.value if intake else None

        items.append(
            IncidentListItem(
                incident_id=incident.id,
                incident_type=incident.incident_type,
                deviation_value=float(incident.deviation_value) if incident.deviation_value is not None else None,
                detected_at=incident.detected_at.isoformat(),
                region=region,
                supplier=supplier.name if supplier else None,
                latest_run_id=latest_run.run_id if latest_run else None,
                latest_status=latest_run.status.value if latest_run else None,
                latest_severity=latest_severity,
            )
        )
    return items


@app.post("/investigations", response_model=InvestigationCreateResponse)
def create_investigation(
    request: InvestigationCreateRequest,
    session: Annotated[Session, Depends(get_session)],
) -> InvestigationCreateResponse:
    settings = get_settings()
    try:
        result = IncidentIntakeService(
            session=session,
            model_client=GeminiIntakeClient(settings),
        ).start_investigation(request.incident_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Incident intake failed: {exc}") from exc

    return InvestigationCreateResponse(
        run_id=result.run_id,
        incident_id=result.incident_id,
        status=result.status,
        current_stage=result.current_stage,
    )


@app.get("/investigations/{run_id}", response_model=InvestigationDetailResponse)
def get_investigation(
    run_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> InvestigationDetailResponse:
    runs = InvestigationRunRepository(session)
    run = runs.get_by_run_id(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Investigation run not found: {run_id}")

    incident = IncidentRepository(session).get_with_context(run.incident_id)
    shipment = incident.shipment
    order = shipment.order
    supplier = order.supplier if order else None
    customer = order.customer if order else None
    region = customer.location.region if customer and customer.location else None

    intake = runs.get_intake(run_id)

    region_comparison: list[dict] = []
    region_trend: list[dict] = []
    if region:
        metrics_repo = ContextualMetricsRepository(session)
        region_comparison = metrics_repo.region_comparison(current_region=region)
        region_trend = metrics_repo.region_trend(region)

    return InvestigationDetailResponse(
        run_id=run.run_id,
        incident_id=run.incident_id,
        status=run.status.value,
        current_stage=run.current_stage.value,
        error_message=run.error_message,
        operational_details=OperationalDetails(
            expected_delivery_date=shipment.expected_delivery_date,
            actual_delivery_date=shipment.actual_delivery_date,
            delay_days=shipment.delay_days,
            shipping_mode=shipment.shipping_mode,
        ),
        business_context=BusinessContext(
            order_value=float(order.order_value) if order and order.order_value is not None else None,
            supplier=supplier.name if supplier else None,
            region=region,
        ),
        incident_type=intake.incident_type if intake else None,
        severity=intake.severity.value if intake else None,
        impact=intake.impact.value if intake and intake.impact else None,
        priority=intake.priority.value if intake and intake.priority else None,
        rationale=intake.rationale if intake else None,
        recommended_next_step=intake.recommended_next_step if intake else None,
        normalized_summary=intake.normalized_summary if intake else None,
        contextual_metrics=run.contextual_metrics,
        region_comparison=region_comparison,
        region_trend=region_trend,
    )
