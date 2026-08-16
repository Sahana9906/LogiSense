"""Brazilian state -> official macro-region mapping.

Public, static geographic reference data (IBGE's five official regions),
not derived from or specific to any dataset's content. Used by the Olist
loader to populate Location.region with something more meaningful than a
bare two-letter state code, so regional contextual metrics are actually
useful.
"""

STATE_TO_REGION: dict[str, str] = {
    # Norte
    "AC": "Norte", "AP": "Norte", "AM": "Norte", "PA": "Norte",
    "RO": "Norte", "RR": "Norte", "TO": "Norte",
    # Nordeste
    "AL": "Nordeste", "BA": "Nordeste", "CE": "Nordeste", "MA": "Nordeste",
    "PB": "Nordeste", "PE": "Nordeste", "PI": "Nordeste", "RN": "Nordeste",
    "SE": "Nordeste",
    # Centro-Oeste
    "DF": "Centro-Oeste", "GO": "Centro-Oeste", "MT": "Centro-Oeste", "MS": "Centro-Oeste",
    # Sudeste
    "ES": "Sudeste", "MG": "Sudeste", "RJ": "Sudeste", "SP": "Sudeste",
    # Sul
    "PR": "Sul", "RS": "Sul", "SC": "Sul",
}


def region_for_state(state: str | None) -> str | None:
    if not state:
        return None
    return STATE_TO_REGION.get(state.strip().upper())
