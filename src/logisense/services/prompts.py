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


# ---------------------------------------------------------------------------
# Phase 2: Hypothesis Agent
#
# Unlike Phase 1's fixed-heading human-readable text (meant to be read
# directly by an operations user), hypotheses are inherently a list of
# distinct, sub-structured items -- so this phase uses the GenAI SDK's
# native structured-output support (a JSON schema) rather than free text.
# That's a deliberate per-phase choice, not an inconsistency: the shape of
# the desired output should drive the output mode.
# ---------------------------------------------------------------------------

HYPOTHESIS_SYSTEM_PROMPT = """You are the LogiSense AI Supply-Chain Hypothesis Agent.

Your job is to generate candidate root-cause hypotheses for a supply-chain
incident that has already been triaged (Phase 1).

You will be given the Phase 1 triage assessment (incident type, severity,
and the full triage narrative), the same pre-calculated contextual metrics
used during triage, and -- when available -- the top hypothesis from
similar past incidents sharing this incident's supplier and/or region. Do
not recompute or restate statistics yourself -- only use the ones
provided.

Use the similar-past-incidents context as cross-incident memory: if a
pattern recurs (the same kind of hypothesis reappearing for this supplier
or region), say so explicitly and let it inform your confidence level. If
no similar past incidents are supplied, reason from this incident alone
and say so -- do not invent a pattern that isn't there.

Generate between 2 and 5 candidate hypotheses that could explain why this
incident occurred.

Each hypothesis must be:
- Plausible given only the supplied incident, triage, context, and
  cross-incident memory data.
- Testable/falsifiable: state what evidence would confirm it and what
  evidence would refute it.
- Clearly NOT a confirmed root cause -- these are candidates for a human
  investigator to check, not conclusions.
- Accompanied by an explicit explanation of why it sits at its rank
  relative to the other hypotheses you generated (why_ranked_here) --
  ranking must be justified, not silent.

Do not invent facts that are not present in the supplied data.
Do not treat any single hypothesis as certain.
Assign each hypothesis a qualitative confidence level (low | medium |
high) reflecting how well the available signals support it -- this must
be your own contextual judgment, not a hardcoded rule or numeric
probability. Order hypotheses from most to least plausible.

Additionally, list (in "ruled_out") any plausible-sounding explanations
you considered but did not include as a full hypothesis, with a short
reason each was ruled out (e.g. contradicted by a specific signal, or
redundant with a stronger hypothesis already listed). This can be an
empty list if you considered no other explanations worth mentioning.

Return the required structured result -- no additional commentary outside
the structured fields."""


@dataclass(frozen=True)
class HypothesisPrompt:
    system: str
    user: str

    @property
    def combined(self) -> str:
        return f"{self.system}\n\n{self.user}"


def _format_similar_past_hypotheses(similar_past: list[dict]) -> str:
    if not similar_past:
        return "unavailable -- no similar past incidents found for this supplier/region"
    lines = []
    for item in similar_past:
        lines.append(
            f"- incident_type={item['incident_type']}, severity={item['severity']}, "
            f"top_hypothesis=\"{item['top_hypothesis']}\" (confidence={item['confidence']})"
        )
    return "\n".join(lines)


class HypothesisPromptBuilder:
    def build(
        self,
        incident: m.Incident,
        intake: m.IncidentIntake,
        contextual_metrics: dict,
        similar_past_hypotheses: list[dict] | None = None,
    ) -> HypothesisPrompt:
        triage_context = "\n".join(
            [
                f"- incident_type: {intake.incident_type}",
                f"- severity: {intake.severity.value}",
            ]
        )

        metrics_block = "\n".join(
            f"- {key}: {'unavailable' if value is None else value}"
            for key, value in _flatten(contextual_metrics or {})
        )

        similar_block = _format_similar_past_hypotheses(similar_past_hypotheses or [])

        user_prompt = f"""PHASE 1 TRIAGE RESULT:
{triage_context}

PHASE 1 FULL TRIAGE ASSESSMENT:
{intake.triage_assessment}

CONTEXTUAL METRICS (pre-calculated -- do not recompute):
{metrics_block}

SIMILAR PAST INCIDENTS (same supplier/region, most recent first -- cross-incident memory):
{similar_block}"""

        return HypothesisPrompt(system=HYPOTHESIS_SYSTEM_PROMPT, user=user_prompt)
