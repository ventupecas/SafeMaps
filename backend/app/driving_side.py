"""Which side of the road traffic keeps to, by country.

A turn across oncoming traffic is a left turn where traffic keeps right and a
right turn where traffic keeps left. The engine uses the country of the trip's
origin; a trip that crosses into a country with the opposite rule (Hong Kong
to Shenzhen, Thailand to Laos) keeps the origin's rule throughout.
"""

from __future__ import annotations

from enum import StrEnum


class DrivingSide(StrEnum):
    RIGHT = "right"
    LEFT = "left"


# ISO 3166-1 alpha-2 codes of countries and territories that drive on the left.
# Source: https://en.wikipedia.org/wiki/Left-_and_right-hand_traffic
LEFT_HAND_TRAFFIC = frozenset(
    {
        # Europe
        "GB", "IE", "MT", "CY", "IM", "JE", "GG",
        # Asia
        "JP", "IN", "PK", "BD", "LK", "NP", "BT", "MV", "TH", "MY", "SG", "BN", "ID", "TL", "HK", "MO",
        # Africa
        "ZA", "LS", "SZ", "BW", "NA", "ZW", "ZM", "MW", "MZ", "TZ", "KE", "UG", "MU", "SC",
        # Oceania
        "AU", "NZ", "PG", "FJ", "SB", "WS", "TO", "KI", "NR", "TV", "CK", "NU", "NF", "PN", "TK",
        # Americas
        "JM", "TT", "BS", "BB", "AG", "DM", "GD", "KN", "LC", "VC", "GY", "SR",
        # Overseas territories
        "AI", "BM", "KY", "MS", "TC", "VG", "VI", "FK", "SH", "CX", "CC",
    }
)  # fmt: skip


def driving_side_for_country(country_code: str) -> DrivingSide:
    """Driving side for a two-letter ISO country code, such as "GB" or "us"."""
    code = country_code.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise ValueError(f"Expected a two-letter ISO country code, got {country_code!r}")
    return DrivingSide.LEFT if code in LEFT_HAND_TRAFFIC else DrivingSide.RIGHT
