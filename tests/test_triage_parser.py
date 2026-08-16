import pytest

from logisense.services.gemini_client import TriageParseError, parse_triage_text

GOOD_TEXT = """INCIDENT TYPE

Shipment Delay

SEVERITY

High

WHAT HAPPENED

The shipment arrived after the expected delivery date.

WHY THIS SEVERITY

The delay is well above the historical average for this route.

POTENTIAL BUSINESS IMPACT

Customer satisfaction may be affected for this order.

IMPORTANT SIGNALS

- Delivery date has passed
- Shipment remains delayed relative to baseline

INVESTIGATION PRIORITIES

1. Check warehouse status.
2. Verify carrier handoff.

INITIAL ASSESSMENT

This is an initial assessment; evidence is not yet sufficient for a
confirmed root cause."""


def test_parses_all_required_sections():
    result = parse_triage_text(GOOD_TEXT)
    assert result.incident_type == "shipment_delay"
    assert result.severity == "high"
    assert "arrived after the expected delivery date" in result.sections["WHAT HAPPENED"]
    assert "Check warehouse status" in result.sections["INVESTIGATION PRIORITIES"]
    assert result.raw_text.startswith("INCIDENT TYPE")


def test_tolerates_markdown_bold_headings():
    text = GOOD_TEXT.replace("SEVERITY\n\nHigh", "**SEVERITY**\n\nHigh")
    result = parse_triage_text(text)
    assert result.severity == "high"


def test_case_insensitive_severity_value():
    text = GOOD_TEXT.replace("High", "HIGH", 1)
    result = parse_triage_text(text)
    assert result.severity == "high"


def test_missing_section_raises():
    broken = GOOD_TEXT.replace("IMPORTANT SIGNALS\n\n- Delivery date has passed\n- Shipment remains delayed relative to baseline\n\n", "")
    with pytest.raises(TriageParseError, match="IMPORTANT SIGNALS"):
        parse_triage_text(broken)


def test_empty_section_raises():
    broken = GOOD_TEXT.replace(
        "WHAT HAPPENED\n\nThe shipment arrived after the expected delivery date.\n\n",
        "WHAT HAPPENED\n\nWHY THIS SEVERITY\n\n",
    )
    # WHAT HAPPENED now immediately followed by the next heading -> empty body
    with pytest.raises(TriageParseError):
        parse_triage_text(broken)


def test_invalid_severity_value_raises():
    broken = GOOD_TEXT.replace("SEVERITY\n\nHigh", "SEVERITY\n\nSuper Urgent")
    with pytest.raises(TriageParseError, match="SEVERITY"):
        parse_triage_text(broken)


def test_empty_response_raises():
    with pytest.raises(TriageParseError):
        parse_triage_text("")
    with pytest.raises(TriageParseError):
        parse_triage_text("   ")


def test_json_response_raises_missing_sections():
    with pytest.raises(TriageParseError):
        parse_triage_text('{"incident_type": "shipment_delay", "severity": "high"}')
