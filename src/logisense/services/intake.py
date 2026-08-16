"""Phase 1 orchestration: Incident Intake & Triage.

DATABASE -> contextual metrics -> Gemini -> validated result -> DATABASE

Postgres is the only persistence/handoff mechanism. No JSON file is ever
written as a Phase 1 -> Phase 2 handoff.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from logisense import models as m
from logisense.repositories import (
    ContextualMetricsRepository,
    IncidentRepository,
    InvestigationRunRepository,
)
from logisense.services.gemini_client import IntakeGenerationError, IntakeModelClient
from logisense.services.prompts import PromptBuilder


@dataclass(frozen=True)
class StartInvestigationResult:
    run_id: str
    incident_id: int
    status: str
    current_stage: str


class IncidentIntakeService:
    def __init__(
        self,
        session: Session,
        model_client: IntakeModelClient,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.session = session
        self.model_client = model_client
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.incidents = IncidentRepository(session)
        self.metrics = ContextualMetricsRepository(session)
        self.runs = InvestigationRunRepository(session)

    def start_investigation(self, incident_id: int) -> StartInvestigationResult:
        incident = self.incidents.get_with_context(incident_id)
        if incident is None:
            raise LookupError(f"Incident not found: {incident_id}")

        run = self.runs.create_run(incident_id=incident_id)
        self.session.commit()

        try:
            self.runs.mark_running(run.run_id)
            self.session.flush()

            profile = self.metrics.build_profile(incident)
            self.runs.set_contextual_metrics(run.run_id, profile.to_dict())
            self.session.flush()

            prompt = self.prompt_builder.build(incident, profile)
            response = self.model_client.classify(prompt)
            assessment = response.assessment

            self.runs.insert_intake(
                run_id=run.run_id,
                incident_type=assessment.incident_type,
                severity=m.IncidentSeverity(assessment.severity),
                rationale=assessment.sections["WHY THIS SEVERITY"],
                recommended_next_step=assessment.sections["INVESTIGATION PRIORITIES"],
                normalized_summary=assessment.sections["INITIAL ASSESSMENT"],
                triage_assessment=assessment.raw_text,
            )
            self.runs.mark_completed(run.run_id)
            self.session.commit()
        except IntakeGenerationError as exc:
            self.session.rollback()
            self.runs.mark_failed(run.run_id, error_message=str(exc))
            self.session.commit()
            raise
        except Exception as exc:
            self.session.rollback()
            self.runs.mark_failed(run.run_id, error_message=str(exc))
            self.session.commit()
            raise

        completed_run = self.runs.get_by_run_id(run.run_id)
        return StartInvestigationResult(
            run_id=completed_run.run_id,
            incident_id=completed_run.incident_id,
            status=completed_run.status.value,
            current_stage=completed_run.current_stage.value,
        )
