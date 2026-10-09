# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Parser for Austrian Post POSTTRACK tracking-event XML files.

Pure module — no frappe imports — so the parser is unit-testable against
fixture files without a site.

Format (TrackingEvent_V2.0.0): ``TrackingData > TrackingEvents`` containing
one ``Header`` and repeated ``Event`` elements. Real files have two quirks the
parser must absorb rather than reject:

* a UTF-8 BOM, and **double-encoded umlauts** ("GÃ¼nzburg" where "Günzburg"
  is meant) — repaired heuristically, per field, never fatally;
* free-text garbage in structured fields (``EventPostalCode`` carries city
  names and hub labels like "Eurodis") — passed through as-is, the data model
  stores them verbatim.

Tag matching is namespace-agnostic (namespaces are stripped) so a
TrackingVersion bump that only moves the namespace does not break ingestion.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from typing import Any

from .status_mapping import map_post_event, post_event_text

#: Carrier identity for Austrian Post. Mirrors
#: ``parcel.carrier_routing.AUSTRIAN_POST`` — duplicated as a literal because
#: carrier_routing imports frappe and this module must stay pure.
CARRIER = "AUSTRIAN_POST"

#: Event fields lifted verbatim from the XML into the raw payload.
_EVENT_FIELDS = (
    "ParcelEventId",
    "IdentCode",
    "ReferenceIdentCode",
    "ReferencedParcelIdentCode",
    "ColliRefNr",
    "CustomerNumber",
    "CustomerShipmentNr",
    "ShpRefNr",
    "EventTimestamp",
    "EventCountry",
    "EventPostalCode",
    "EventCity",
    "ParcelEventTypeCode",
    "ParcelEventReasonCode",
    "ShipmentState",
    "Weight",
    "ConsigneeName",
    "Remark",
    # Optional per the format spec ("Trackingdaten an Kunden" V2.1, Anhang 1),
    # and worth keeping whenever the Post does send them: BranchKey is the ID
    # of the post office holding a deposited parcel, the two SAP numbers are
    # the Post's own billing order/invoice references, and the three Ref
    # numbers are customer-side references that may carry our own bookkeeping.
    "BranchKey",
    "SAPOrderNr",
    "SAPInvoiceNr",
    "CostCenterRefNr",
    "AlternativeRefNr",
    "OriginCustomerNumber",
    "InsertDate",
)


class PostTrackingParseError(Exception):
    """Raised when a file is structurally unusable (not XML, wrong root)."""


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _repair_mojibake(value: str) -> str:
    """Undo one round of latin-1/UTF-8 double encoding, if present.

    "GÃ¼nzburg" -> "Günzburg". The repair is kept only when it removes the
    tell-tale "Ã"/"Â" sequences; anything else (legitimate "Ã" in a name,
    characters latin-1 cannot express) passes through unchanged. Never raises.
    """
    if "Ã" not in value and "Â" not in value:
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    if repaired.count("Ã") + repaired.count("Â") < value.count("Ã") + value.count("Â"):
        return repaired
    return value


def _normalize_timestamp(value: str) -> str:
    """'2025-11-14T12:32:18.000' -> '2025-11-14 12:32:18' (frappe Datetime)."""
    value = value.strip().replace("T", " ")
    if "." in value:
        value = value.split(".", 1)[0]
    return value


def parse(content: bytes) -> dict[str, Any]:
    """Parse one POSTTRACK file into header info + normalized event dicts.

    Returns ``{"header": {...}, "events": [event, ...]}`` where each event is
    ready for ``events.upsert_events`` (Parcel Tracking Event field names).
    Malformed *individual* values degrade (kept raw / empty); only a file that
    is not parseable XML at all raises :class:`PostTrackingParseError`.
    """
    text = content.decode("utf-8", errors="replace").lstrip("﻿")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PostTrackingParseError(f"Not parseable as XML: {exc}") from exc

    header: dict[str, str] = {}
    raw_events: list[dict[str, str]] = []
    for element in root.iter():
        tag = _strip_ns(element.tag)
        if tag == "Header":
            header = {_strip_ns(child.tag): (child.text or "").strip() for child in element}
        elif tag == "Event":
            fields = {_strip_ns(child.tag): (child.text or "").strip() for child in element}
            raw_events.append(fields)

    return {"header": header, "events": [_to_tracking_event(raw) for raw in raw_events]}


def _to_tracking_event(raw: dict[str, str]) -> dict[str, Any]:
    get = lambda key: _repair_mojibake(raw.get(key, ""))  # noqa: E731

    parcel_event_id = raw.get("ParcelEventId", "")
    if parcel_event_id:
        event_id = f"AP::{parcel_event_id}"
    else:
        # Defensive: an event without its id is still stored, keyed by content.
        digest = hashlib.sha1(
            "|".join(raw.get(f, "") for f in _EVENT_FIELDS).encode("utf-8")
        ).hexdigest()[:16]
        event_id = f"AP::content::{digest}"

    shipment_state = raw.get("ShipmentState", "")
    type_code = raw.get("ParcelEventTypeCode", "")
    reason_code = raw.get("ParcelEventReasonCode", "")

    return {
        "carrier": CARRIER,
        "event_id": event_id,
        "tracking_number": raw.get("IdentCode", ""),
        "reference_ident_code": raw.get("ReferenceIdentCode", ""),
        "event_timestamp": _normalize_timestamp(raw.get("EventTimestamp", "")),
        "event_code": type_code,
        "reason_code": reason_code,
        "carrier_status": shipment_state,
        "canonical_status": map_post_event(shipment_state, type_code, reason_code),
        # The Post's own wording for the code pair, so an event row reads as a
        # sentence instead of "ZUH/BN". Remark is the fallback: it is usually
        # empty and, when set, carries free text like the drop-off location.
        "description": post_event_text(type_code, reason_code) or get("Remark"),
        "location_city": get("EventCity"),
        "location_postal_code": get("EventPostalCode"),
        "location_country": raw.get("EventCountry", ""),
        "raw_payload": json.dumps(
            {key: get(key) for key in _EVENT_FIELDS if raw.get(key)}, ensure_ascii=False, indent=1
        ),
    }
