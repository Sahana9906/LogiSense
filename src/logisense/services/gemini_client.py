"""Gemini clients for the Phase 1 Triage Agent and Phase 2 Hypothesis Agent.

Phase 1 (Triage Agent) returns human-readable text with a fixed set of
section headings (not JSON -- this text is meant to be shown to an
operations user directly). Phase 2 (Hypothesis Agent) returns a
structured JSON list, since hypotheses are inherently sub-structured list
items rather than a single narrative -- a deliberate per-phase choice.

Both clients parse/validate their model's output and retry exactly once
with a correction prompt on failure. If it fails again, the caller must
mark the investigation FAILED -- neither client ever fabricates a result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from google import genai
from pydantic import BaseModel, Field, ValidationError, field_validator

from logisense.config import Settings
from logisense.services.prompts import HypothesisPrompt, PhaseOnePrompt

REQUIRED_SECTIONS = [
    "INCIDENT TYPE",
    "SEVERITY",
    "WHAT HAPPENED",
    "WHY THIS SEVERITY",
    "POTENTIAL BUSINESS IMPACT",
    "IMPORTANT SIGNALS",
    "INVESTIGATION PRIORITIES",
    "INITIAL ASSESSMENT",
]

VALID_SEVERITIES = {"low", "medium", "high", "critical"}

# Tolerate minor formatting the model might add despite instructions not
# to (markdown bold, a trailing colon) without accepting arbitrary text as
# a heading.
_HEADING_PATTERN = re.compile(
    r"^[ \t]*\**[ \t]*(" + "|".join(re.escape(h) for h in REQUIRED_SECTIONS) + r")[ \t]*:?[ \t]*\**[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)


class TriageParseError(ValueError):
    """Raised when Gemini's text response is missing/malformed sections."""


@dataclass(frozen=True)
class TriageAssessment:
    raw_text: str
    incident_type: str
    severity: str  # normalized: low | medium | high | critical
    sections: dict[str, str]  # canonical heading -> section body text


def parse_triage_text(text: str) -> TriageAssessment:
    if not text or not text.strip():
        raise TriageParseError("Empty response from Gemini")

    matches = list(_HEADING_PATTERN.finditer(text))
    found = {match.group(1).strip().upper() for match in matches}
    missing = [h for h in REQUIRED_SECTIONS if h not in found]
    if missing:
        raise TriageParseError(f"Missing required section(s): {', '.join(missing)}")

    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        canonical = match.group(1).strip().upper()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[canonical] = text[start:end].strip()

    empty = [h for h in REQUIRED_SECTIONS if not sections.get(h)]
    if empty:
        raise TriageParseError(f"Section(s) returned empty: {', '.join(empty)}")

    incident_type_line = sections["INCIDENT TYPE"].splitlines()[0].strip()
    incident_type = incident_type_line.lower().replace(" ", "_")
    if not (3 <= len(incident_type) <= 128):
        raise TriageParseError(f"Invalid INCIDENT TYPE extracted: {incident_type_line!r}")

    severity_line = sections["SEVERITY"].splitlines()[0].strip().lower()
    severity = re.sub(r"[^a-z]", "", severity_line)
    if severity not in VALID_SEVERITIES:
        raise TriageParseError(
            f"SEVERITY must be one of low/medium/high/critical, got: {sections['SEVERITY'][:80]!r}"
        )

    return TriageAssessment(
        raw_text=text.strip(),
        incident_type=incident_type,
        severity=severity,
        sections=sections,
    )


@dataclass(frozen=True)
class GeminiUsage:
    tokens_used: int | None


@dataclass(frozen=True)
class GeminiTriageResponse:
    assessment: TriageAssessment
    usage: GeminiUsage


class IntakeGenerationError(RuntimeError):
    """Raised when Gemini's output could not be parsed/validated even
    after one correction-prompt retry."""


class IntakeModelClient(Protocol):
    model_name: str

    def classify(self, prompt: PhaseOnePrompt) -> GeminiTriageResponse:
        ...


class GeminiIntakeClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY must be set before running Phase 1 intake")
        self.model_name = settings.gemini_model
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def classify(self, prompt: PhaseOnePrompt) -> GeminiTriageResponse:
        try:
            return self._call(prompt.combined)
        except (TriageParseError, ValueError) as first_error:
            correction_prompt = self._correction_prompt(prompt, first_error)
            try:
                return self._call(correction_prompt)
            except (TriageParseError, ValueError) as second_error:
                raise IntakeGenerationError(
                    f"Gemini triage output invalid after retry: {second_error}"
                ) from second_error

    def _call(self, prompt_text: str) -> GeminiTriageResponse:
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt_text,
        )
        if not response.text:
            raise ValueError("Empty response from Gemini")
        assessment = parse_triage_text(response.text)
        usage_metadata = getattr(response, "usage_metadata", None)
        tokens_used = getattr(usage_metadata, "total_token_count", None) if usage_metadata else None
        return GeminiTriageResponse(assessment=assessment, usage=GeminiUsage(tokens_used=tokens_used))

    @staticmethod
    def _correction_prompt(prompt: PhaseOnePrompt, error: Exception) -> str:
        headings_block = "\n\n".join(f"{h}\n\n[generated answer]" for h in REQUIRED_SECTIONS)
        return f"""{prompt.combined}

Your previous response could not be validated. Error: {error}

Return ONLY plain human-readable text using exactly these section
headings, each on its own line, with no markdown formatting, no JSON, and
no extra headings or commentary outside these sections:

{headings_block}

SEVERITY must be exactly one of: Low, Medium, High, Critical."""


# ---------------------------------------------------------------------------
# Phase 2: Hypothesis Agent (structured JSON output)
# ---------------------------------------------------------------------------

class HypothesisItem(BaseModel):
    statement: str = Field(min_length=10, max_length=500)
    rationale: str = Field(min_length=15, max_length=2000)
    supporting_signals: str = Field(min_length=10, max_length=1000)
    confidence: str = Field(pattern="^(low|medium|high)$")
    what_would_confirm: str = Field(min_length=10, max_length=1000)
    what_would_refute: str = Field(min_length=10, max_length=1000)
    why_ranked_here: str = Field(min_length=10, max_length=1000)

    @field_validator("confidence")
    @classmethod
    def normalize_confidence(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator(
        "statement", "rationale", "supporting_signals",
        "what_would_confirm", "what_would_refute", "why_ranked_here",
    )
    @classmethod
    def collapse_whitespace(cls, value: str) -> str:
        return " ".join(value.strip().split())


class RuledOutHypothesis(BaseModel):
    statement: str = Field(min_length=10, max_length=300)
    reason_ruled_out: str = Field(min_length=10, max_length=500)

    @field_validator("statement", "reason_ruled_out")
    @classmethod
    def collapse_whitespace(cls, value: str) -> str:
        return " ".join(value.strip().split())


class HypothesisSetResponse(BaseModel):
    hypotheses: list[HypothesisItem] = Field(min_length=2, max_length=5)
    ruled_out: list[RuledOutHypothesis] = Field(default_factory=list, max_length=5)


@dataclass(frozen=True)
class GeminiHypothesisResponse:
    hypotheses: list[HypothesisItem]
    ruled_out: list[RuledOutHypothesis]
    usage: GeminiUsage


class HypothesisModelClient(Protocol):
    model_name: str

    def generate(self, prompt: HypothesisPrompt) -> GeminiHypothesisResponse:
        ...


class GeminiHypothesisClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY must be set before running Phase 2 hypothesis generation")
        self.model_name = settings.gemini_model
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def generate(self, prompt: HypothesisPrompt) -> GeminiHypothesisResponse:
        try:
            return self._call(prompt.combined)
        except (ValidationError, ValueError) as first_error:
            correction_prompt = self._correction_prompt(prompt, first_error)
            try:
                return self._call(correction_prompt)
            except (ValidationError, ValueError) as second_error:
                raise IntakeGenerationError(
                    f"Gemini hypothesis output invalid after retry: {second_error}"
                ) from second_error

    def _call(self, prompt_text: str) -> GeminiHypothesisResponse:
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt_text,
            config={
                "response_mime_type": "application/json",
                "response_schema": HypothesisSetResponse,
            },
        )
        if not response.text:
            raise ValueError("Empty response from Gemini")
        parsed = HypothesisSetResponse.model_validate_json(response.text)
        usage_metadata = getattr(response, "usage_metadata", None)
        tokens_used = getattr(usage_metadata, "total_token_count", None) if usage_metadata else None
        return GeminiHypothesisResponse(
            hypotheses=parsed.hypotheses,
            ruled_out=parsed.ruled_out,
            usage=GeminiUsage(tokens_used=tokens_used),
        )

    @staticmethod
    def _correction_prompt(prompt: HypothesisPrompt, error: Exception) -> str:
        return f"""{prompt.combined}

Your previous response could not be validated against the required JSON
schema. Error: {error}

Return ONLY valid JSON matching exactly this shape, with 2 to 5 items in
"hypotheses" and 0 to 5 items in "ruled_out", no markdown, and no extra
keys:
{{
    "hypotheses": [
        {{
            "statement": "...",
            "rationale": "...",
            "supporting_signals": "...",
            "confidence": "low|medium|high",
            "what_would_confirm": "...",
            "what_would_refute": "...",
            "why_ranked_here": "..."
        }}
    ],
    "ruled_out": [
        {{
            "statement": "...",
            "reason_ruled_out": "..."
        }}
    ]
}}"""
