"""Shared customs facts: which destinations cross a customs border.

The EU customs-union membership list drives carrier behaviour that has nothing
to do with each other on the surface but is the same question underneath:

  * GLS needs an ``IncotermCode`` only for non-EU destinations.
  * FedEx needs Electronic Trade Documents and a commercial invoice only for
    non-EU destinations — verified against the sandbox, where AT->DE answers
    ``requiredDocuments: ["AIR_WAYBILL"]`` while AT->CH additionally demands
    ``COMMERCIAL_OR_PRO_FORMA_INVOICE``.

Keeping one list means a country joining or leaving is a single edit, not a hunt
through carrier modules that each kept their own copy.
"""

from __future__ import annotations

#: EU customs union members, as ISO 3166-1 alpha-2. Shipments *between* these
#: need no customs declaration. Deliberately excludes post-Brexit GB, and CH/NO
#: which are in EFTA but not the customs union.
EU_COUNTRY_CODES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE",
})


def is_eu(country_code: str | None) -> bool:
    """True if the two-letter country code is inside the EU customs union.

    An empty/unknown code returns True — the historical GLS behaviour, which
    treats "we don't know" as "don't add customs handling" rather than
    generating a customs declaration from missing data.
    """
    code = (country_code or "").strip().upper()
    return not code or code in EU_COUNTRY_CODES


def crosses_customs_border(origin_code: str | None, destination_code: str | None) -> bool:
    """True when the shipment leaves the EU customs union in either direction."""
    return not (is_eu(origin_code) and is_eu(destination_code))
