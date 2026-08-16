import pytest

from logisense import models as m
from logisense.services.gemini_client import (
    GeminiTriageResponse,
    GeminiUsage,
    IntakeGenerationError,
    parse_triage_text,
)
from logisense.services.intake import IncidentIntakeService

from .factories import make_incident, make_order, make_shipment


class FakeModelClient:
    """Test double for IntakeModelClient."""

    model_name = "fake-model"

    def __init__(self, response: GeminiTriageResponse | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls = 0

    def classify(self, prompt):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


def _make_incident(session):
    order = make_order(session, order_value=1000, quantity=5)
    shipment = make_shipment(session, order, delay_days=7)
    return make_incident(session, shipment, deviation_value=7)


def _sample_triage_text(severity="High"):
    return f"""INCIDENT TYPE

Shipment Delay

SEVERITY

{severity}

WHAT HAPPENED

The shipment departed on schedule but arrived several days after the
expected delivery date.

WHY THIS SEVERITY

The delay significantly exceeds the historical average, and the supplier's
late-rate is elevated relative to the network baseline.

POTENTIAL BUSINESS IMPACT

This may affect customer satisfaction and downstream fulfillment
commitments tied to this order.

IMPORTANT SIGNALS

- Delivery date has passed
- Supplier late-rate ratio is elevated vs. the network baseline

INVESTIGATION PRIORITIES

1. Check warehouse status.
2. Verify carrier handoff.
3. Check delivery information.

INITIAL ASSESSMENT

This is an initial assessment based on available operational data;
evidence is not yet sufficient to confirm a root cause."""


def _make_response(severity="High", tokens=123):
    assessment = parse_triage_text(_sample_triage_text(severity))
    return GeminiTriageResponse(assessment=assessment, usage=GeminiUsage(tokens_used=tokens))


def test_successful_investigation_persists_full_intake(session):
    incident = _make_incident(session)
    client = FakeModelClient(response=_make_response(severity="High"))

    result = IncidentIntakeService(session=session, model_client=client).start_investigation(incident.id)

    assert result.status == "completed"
    assert result.current_stage == "ready_for_hypothesis"

    run = session.query(m.InvestigationRun).filter_by(run_id=result.run_id).one()
    assert run.status == m.InvestigationRunStatus.COMPLETED
    assert run.error_message is None

    intake = session.query(m.IncidentIntake).filter_by(run_id=result.run_id).one()
    assert intake.severity == m.IncidentSeverity.HIGH
    assert intake.incident_type == "shipment_delay"
    assert intake.impact is None  # no longer produced by this prompt
    assert intake.priority is None
    assert "Check warehouse status" in intake.recommended_next_step
    assert "INCIDENT TYPE" in intake.triage_assessment
    assert "INVESTIGATION PRIORITIES" in intake.triage_assessment
    assert "IMPORTANT SIGNALS" in intake.triage_assessment


def test_incident_not_found_raises_lookup_error(session):
    client = FakeModelClient()
    with pytest.raises(LookupError):
        IncidentIntakeService(session=session, model_client=client).start_investigation(999999)


def test_gemini_failure_marks_run_failed_without_fabricating_result(session):
    incident = _make_incident(session)
    client = FakeModelClient(error=IntakeGenerationError("invalid output after retry"))

    with pytest.raises(IntakeGenerationError):
        IncidentIntakeService(session=session, model_client=client).start_investigation(incident.id)

    run = session.query(m.InvestigationRun).one()
    assert run.status == m.InvestigationRunStatus.FAILED
    assert run.error_message is not None
    assert session.query(m.IncidentIntake).count() == 0


def test_same_incident_can_be_investigated_multiple_times(session):
    incident = _make_incident(session)
    client = FakeModelClient(response=_make_response(severity="Medium"))
    service = IncidentIntakeService(session=session, model_client=client)

    first = service.start_investigation(incident.id)
    second = service.start_investigation(incident.id)

    assert first.run_id != second.run_id
    assert session.query(m.InvestigationRun).filter_by(incident_id=incident.id).count() == 2
