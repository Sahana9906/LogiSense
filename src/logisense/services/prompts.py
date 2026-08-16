"""Phase 1 prompt construction -- the Triage Agent.

Demonstrates the three required GenAI concepts:
  1. Role prompting       -> fixed system role (Triage Agent).
  2. Contextual prompting  -> incident/operational/business/metrics blocks.
  3. Structured output     -> a fixed, human-readable section format the
     model must follow exactly (not JSON -- this output is meant to be
     displayed to an operations user directly).

Gemini receives pre-calculated contextual metrics (see
repositories.ContextualMetricsRepository) and interprets them. It never
computes statistics itself.
"""
from __future__ import annotations

from dataclasses import dataclass

from logisense import models as m
from logisense.repositories import IncidentContextProfile

SYSTEM_PROMPT = """You are the LogiSense AI Supply-Chain Triage Agent.

Your job is to perform an initial assessment of the provided supply-chain
incident.

Analyze the incident and produce the following sections:

INCIDENT TYPE
SEVERITY
WHAT HAPPENED
WHY THIS SEVERITY
POTENTIAL BUSINESS IMPACT
IMPORTANT SIGNALS
INVESTIGATION PRIORITIES
INITIAL ASSESSMENT

Use only information contained in the supplied incident and context.

Do not invent facts.

Distinguish known facts from assumptions.

Do not claim a confirmed root cause when evidence is insufficient.

Severity must be assessed using the overall incident context, not a
hard-coded rule. Weigh the operational deviation, the supplier/regional/
shipping-mode comparisons and trends, the business impact, and any other
available signals together. Severity must be exactly one of: Low, Medium,
High, Critical.

INVESTIGATION PRIORITIES are practical next things an investigator should
check -- investigation suggestions, not confirmed root causes.

The result will be displayed directly to an operations user, so make it
concise, clear and professional.

Return your answer as human-readable text using exactly these section
headings, each on its own line, followed by the generated content for
that section:

INCIDENT TYPE

[generated answer]

SEVERITY

[generated answer -- exactly one of: Low, Medium, High, Critical]

WHAT HAPPENED

[generated answer]

WHY THIS SEVERITY

[generated answer]

POTENTIAL BUSINESS IMPACT

[generated answer]

IMPORTANT SIGNALS

[generated bullet points]

INVESTIGATION PRIORITIES

[generated numbered list]

INITIAL ASSESSMENT

[generated answer -- must state this is an initial assessment when
evidence is insufficient]

Do not return JSON. Do not return Python objects. Do not return
hard-coded example content -- every value must be generated from the
actual incident and context supplied below."""


def _fmt(value) -> str:
    return "unavailable" if value is None else str(value)


def _flatten(obj, prefix: str = "") -> list[tuple[str, object]]:
    if isinstance(obj, dict):
        items: list[tuple[str, object]] = []
        for key, value in obj.items():
            items.extend(_flatten(value, f"{prefix}.{key}" if prefix else key))
        return items
    return [(prefix, obj)]


def _format_metrics(profile: IncidentContextProfile) -> str:
    lines = [
        f"- {key}: {'unavailable' if value is None else value}"
        for key, value in _flatten(profile.to_dict())
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class PhaseOnePrompt:
    system: str
    user: str

    @property
    def combined(self) -> str:
        return f"{self.system}\n\n{self.user}"


class PromptBuilder:
    def build(self, incident: m.Incident, profile: IncidentContextProfile) -> PhaseOnePrompt:
        shipment = incident.shipment
        order = shipment.order
        product = order.product if order else None
        customer = order.customer if order else None
        supplier = order.supplier if order else None

        incident_context = "\n".join(
            [
                f"- incident_id: {incident.id}",
                f"- data_derived_incident_type: {incident.incident_type}",
                f"- deviation_value: {_fmt(incident.deviation_value)}",
                f"- detection_rationale: {incident.rationale}",
            ]
        )

        operational_context = "\n".join(
            [
                f"- expected_delivery_date: {_fmt(shipment.expected_delivery_date)}",
                f"- actual_delivery_date: {_fmt(shipment.actual_delivery_date)}",
                f"- delay_days: {_fmt(shipment.delay_days)}",
                f"- shipping_mode: {_fmt(shipment.shipping_mode)}",
                f"- origin: {_fmt(self._location_label(shipment.origin))}",
                f"- destination: {_fmt(self._location_label(shipment.destination))}",
            ]
        )

        business_context = "\n".join(
            [
                f"- order_value: {_fmt(order.order_value) if order else 'unavailable'}",
                f"- quantity: {_fmt(order.quantity) if order else 'unavailable'}",
                f"- order_status: {_fmt(order.status) if order else 'unavailable'}",
                f"- freight_cost: {_fmt(shipment.freight_cost)}",
                f"- product_category: {_fmt(product.category) if product else 'unavailable'}",
                f"- supplier: {_fmt(supplier.name) if supplier else 'unavailable'}",
                f"- supplier_location: {_fmt(self._location_label(supplier.location)) if supplier else 'unavailable'}",
                f"- customer_location: {_fmt(self._location_label(customer.location)) if customer else 'unavailable'}",
            ]
        )

        contextual_metrics = _format_metrics(profile)

        user_prompt = f"""INCIDENT:
{incident_context}

OPERATIONAL DATA:
{operational_context}

BUSINESS CONTEXT:
{business_context}

CONTEXTUAL METRICS (pre-calculated -- do not recompute):
{contextual_metrics}"""

        return PhaseOnePrompt(system=SYSTEM_PROMPT, user=user_prompt)

    @staticmethod
    def _location_label(location) -> str | None:
        if location is None:
            return None
        parts = [p for p in [location.city, location.state, location.country] if p]
        return ", ".join(parts) if parts else location.region
