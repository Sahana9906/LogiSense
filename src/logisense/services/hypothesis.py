"""Phase 2 orchestration: Hypothesis Generation.

PHASE 1 RESULT (from Postgres) -> Gemini -> validated hypotheses -> PostgreSQL

Reads Phase 1's persisted state (incident_intake + the contextual_metrics
snapshot already stored on investigation_runs) rather than recomputing
anything -- Postgres is the sole Phase 1 -> Phase 2 handoff, same as
Phase 1's own handoff principle.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from logisense import models as m
from logisense.repositories import HypothesisRepository, IncidentRepository, InvestigationRunRepository
from logisense.services.gemini_client import HypothesisModelClient, IntakeGenerationError
from logisense.services.prompts import HypothesisPromptBuilder


@dataclass(frozen=True)
class HypothesisGenerationResult:
    run_id: str
    current_stage: str
    hypothesis_count: int
    ruled_out_count: int
    similar_past_incidents_used: int


class HypothesisGenerationService:
    def __init__(
        self,
        session: Session,
        model_client: HypothesisModelClient,
        prompt_builder: HypothesisPromptBuilder | None = None,
    ) -> None:
        self.session = session
        self.model_client = model_client
        self.prompt_builder = prompt_builder or HypothesisPromptBuilder()
        self.incidents = IncidentRepository(session)
        self.runs = InvestigationRunRepository(session)
        self.hypotheses = HypothesisRepository(session)

    def generate(self, run_id: str) -> HypothesisGenerationResult:
        run = self.runs.get_by_run_id(run_id)
        if run is None:
            raise LookupError(f"Investigation run not found: {run_id}")
        if run.status != m.InvestigationRunStatus.COMPLETED:
            raise ValueError(f"Run {run_id} has not completed Phase 1 (status={run.status.value})")

        intake = self.runs.get_intake(run_id)
        if intake is None:
            raise LookupError(f"No Phase 1 triage found for run: {run_id}")

        incident = self.incidents.get_with_context(run.incident_id)
        if incident is None:
            raise LookupError(f"Incident not found: {run.incident_id}")

        order = incident.shipment.order
        supplier_id = order.supplier_id if order else None
        region = (
            order.customer.location.region
            if order and order.customer and order.customer.location
            else None
        )
        similar_past = self.hypotheses.get_similar_past_hypotheses(
            supplier_id=supplier_id, region=region, exclude_run_id=run_id
        )

        try:
            prompt = self.prompt_builder.build(
                incident, intake, run.contextual_metrics or {}, similar_past
            )
            response = self.model_client.generate(prompt)

            hypothesis_dicts = [
                {
                    "statement": h.statement,
                    "rationale": h.rationale,
                    "supporting_signals": h.supporting_signals,
                    "confidence": h.confidence,
                    "what_would_confirm": h.what_would_confirm,
                    "what_would_refute": h.what_would_refute,
                    "why_ranked_here": h.why_ranked_here,
                }
                for h in response.hypotheses
            ]
            ruled_out_dicts = [
                {"statement": r.statement, "reason_ruled_out": r.reason_ruled_out}
                for r in response.ruled_out
            ]
            self.hypotheses.replace_hypotheses(run_id, hypothesis_dicts)
            self.runs.set_ruled_out_hypotheses(run_id, ruled_out_dicts)
            self.runs.mark_hypothesis_generated(run_id)
            self.session.commit()
        except IntakeGenerationError as exc:
            self.session.rollback()
            self.runs.mark_failed(run_id, error_message=f"Phase 2 failed: {exc}")
            self.session.commit()
            raise
        except Exception as exc:
            self.session.rollback()
            self.runs.mark_failed(run_id, error_message=f"Phase 2 failed: {exc}")
            self.session.commit()
            raise

        updated_run = self.runs.get_by_run_id(run_id)
        return HypothesisGenerationResult(
            run_id=run_id,
            current_stage=updated_run.current_stage.value,
            hypothesis_count=len(hypothesis_dicts),
            ruled_out_count=len(ruled_out_dicts),
            similar_past_incidents_used=len(similar_past),
        )