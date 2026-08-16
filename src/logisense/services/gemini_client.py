"""Gemini client for the Phase 1 Triage Agent.

Gemini returns human-readable text with a fixed set of section headings
(not JSON -- this text is meant to be shown to an operations user
directly). This client parses that text, validates every required
section is present and non-empty, and extracts incident_type/severity
for the typed database columns. If parsing/validation fails, it retries
exactly once with a correction prompt. If it fails again, the caller must
mark the investigation FAILED -- this client never fabricates a result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from google import genai

from logisense.config import Settings
from logisense.services.prompts import PhaseOnePrompt

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
