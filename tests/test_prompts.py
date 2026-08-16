from logisense.repositories import ContextualMetricsRepository, IncidentRepository
from logisense.services.prompts import PromptBuilder

from .factories import make_incident, make_order, make_shipment, make_supplier


def test_prompt_embeds_precalculated_metrics_and_forbids_recalculation(session):
    supplier = make_supplier(session, name="Acme Logistics")
    incident_shipment = None
    for d in [0, 0, 5, 5, 10]:
        order = make_order(session, supplier=supplier)
        shipment = make_shipment(session, order, delay_days=d)
        if d == 5:
            incident_shipment = shipment
    for d in [0, 0, 0, 0, 0]:
        order = make_order(session)  # unaffiliated baseline shipments
        make_shipment(session, order, delay_days=d)

    incident = make_incident(session, incident_shipment, deviation_value=5)
    incident_ctx = IncidentRepository(session).get_with_context(incident.id)
    profile = ContextualMetricsRepository(session).build_profile(incident_ctx)

    prompt = PromptBuilder().build(incident_ctx, profile)

    # Role prompting
    assert "LogiSense AI Supply-Chain Triage Agent" in prompt.system

    # The model must be told not to invent facts / not to confirm root cause.
    assert "Do not invent facts" in prompt.system
    assert "Do not claim a confirmed root cause" in prompt.system

    # Contextual prompting: pre-calculated metrics actually appear in the prompt.
    assert "supplier.late_rate_ratio: 2.0" in prompt.user
    assert "supplier.label: Acme Logistics" in prompt.user

    # Structured (fixed-heading, human-readable text) output requirement is present.
    assert "INCIDENT TYPE" in prompt.system
    assert "SEVERITY" in prompt.system
    assert "IMPORTANT SIGNALS" in prompt.system
    assert "INVESTIGATION PRIORITIES" in prompt.system
    assert "INITIAL ASSESSMENT" in prompt.system
    assert "Do not return JSON" in prompt.system
