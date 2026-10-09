# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Poll GLS ShipIT Track & Trace for open shipments.

Unlike FedEx there is no batch endpoint — ``/parceldetails`` takes exactly one
TrackID per request — so the run is capped by ``GLS Settings -> Max Parcels
per Poll Run`` and processes the oldest shipments first (they are closest to
delivery, i.e. closest to leaving the poll set). Each parcel is isolated in
its own try/except: one TrackID GLS refuses to answer must not abort the rest.

The response shape differs slightly between ShipIT installations, so the
extraction reads both spellings (``History``/``history`` etc.) and stores the
untouched entry as raw payload; unknown status codes degrade to "In Transit"
(see ``status_mapping``).

A history entry as production answers it (captured 2026-10-02)::

    {"Date": "2026-10-02T05:21:23+02:00", "StatusCode": "IN_DELIVERY",
     "Description": "The parcel is expected to be delivered during the day.",
     "Location": "Ansfelden AT 380", "LocationCode": "AT 380", "Country": "AT"}
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import frappe
from frappe.utils import cint

from ..carrier_routing import GLS
from ..infra.gls_api import GLSAPIClient, GLSAPIError
from . import events
from .status_mapping import known_gls_status, map_gls_event


def poll_gls_tracking() -> dict[str, Any] | None:
    """Scheduler entry point."""
    settings = frappe.get_single("GLS Settings")
    if not cint(settings.get("enable_gls_tracking")):
        frappe.logger().info("[gls-tracking] skipped: poll disabled in GLS Settings.")
        return None

    try:
        client = GLSAPIClient.from_settings()
    except GLSAPIError as exc:
        # from_settings throws when credentials are incomplete — for the
        # scheduler that is "not configured", not a failure.
        frappe.logger().info(f"[gls-tracking] skipped: {exc}")
        return None

    open_rows = events.open_shipments(GLS)
    limit = cint(settings.get("gls_max_parcels_per_run")) or 200
    if len(open_rows) > limit:
        frappe.logger().warning(
            f"[gls-tracking] {len(open_rows)} open shipments exceed the per-run cap of {limit}; "
            f"polling the oldest {limit}."
        )
        open_rows = open_rows[:limit]

    summary: dict[str, Any] = {"shipments": len(open_rows), "inserted": 0, "failed": 0}
    touched: set[str] = set()
    for index, row in enumerate(open_rows, start=1):
        try:
            payload = client.get_parcel_details(row.awb_number)
            result = events.upsert_events(_events_from_details(row.awb_number, row.name, payload))
            summary["inserted"] += result["inserted"]
            touched |= result["shipments"]
        except Exception:
            frappe.db.rollback()
            summary["failed"] += 1
            frappe.log_error(
                title=f"GLS tracking: poll failed for {row.name}",
                message=f"TrackID: {row.awb_number}\n\n{frappe.get_traceback()}",
            )
        if index % 25 == 0:
            frappe.db.commit()
    frappe.db.commit()

    events.update_shipment_statuses(touched)
    frappe.db.commit()
    frappe.logger().info(f"[gls-tracking] run done: {summary}")
    return summary


def _get(entry: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = entry.get(key)
        if value:
            return str(value)
    return default


#: Depot code at the end of a location: "AT 380", "DE R80".
_DEPOT_CODE = re.compile(r"\s+[A-Z]{2}\s+[A-Z]?\d{2,4}$")


def _depot_city(entry: dict) -> str:
    """"Ansfelden AT 380" -> "Ansfelden": the place name without the depot
    codes GLS appends ("RUP München DE R80 DE 080" carries two)."""
    location = _get(entry, "Location", "location").strip()
    while True:
        shorter = _DEPOT_CODE.sub("", location)
        if shorter == location:
            return location
        location = shorter.strip()


def _events_from_details(track_id: str, shipment: str, payload: dict) -> list[dict[str, Any]]:
    """Flatten one parcel-details response into Parcel Tracking Event dicts.

    ShipIT wraps the answer in ``UnitDetail`` — reading the top level instead
    finds no history and yields no events, which looks exactly like a healthy
    run with nothing new (HTTP 200, zero inserts). Hence the unwrap, and the
    warning below: a 200 that produces no events is reported with the keys the
    carrier actually sent, so an installation whose payload differs says so
    instead of silently tracking nothing.
    """
    payload = payload or {}
    detail = payload.get("UnitDetail") or payload.get("unitDetail") or payload
    if not isinstance(detail, dict):
        detail = {}
    history = detail.get("History") or detail.get("history") or detail.get("TUStatusInfo") or []
    parcel_status = _get(detail, "Status", "status", "parcelStatus", "ExitStatus")

    if not history:
        frappe.logger().warning(
            f"[gls-tracking] TrackID={track_id}: response carried no recognisable history; "
            f"top-level keys={sorted(payload)} detail keys={sorted(detail)}"
        )
        return []

    collected: list[dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        date = _get(entry, "Date", "date")
        time = _get(entry, "Time", "time")
        timestamp = f"{date} {time}".strip()[:19]
        description = _get(entry, "Description", "description", "StatusText", "evtDscr")
        code = _get(
            entry, "Code", "code", "StatusCode", "statusCode", "EventNumber", "eventNumber", "evtNo"
        )
        address = entry.get("Address") or entry.get("address") or {}

        digest = hashlib.sha1(f"{timestamp}|{code}|{description}".encode("utf-8")).hexdigest()[:16]
        collected.append(
            {
                "carrier": GLS,
                "event_id": f"GLS::{track_id}::{digest}",
                "tracking_number": track_id,
                "shipment": shipment,
                "event_timestamp": timestamp,
                "event_code": code,
                "carrier_status": code,
                "canonical_status": map_gls_event(code, description),
                "description": description[:500],
                "location_city": _get(address, "City", "city") or _depot_city(entry),
                "location_postal_code": _get(address, "ZIPCode", "zipCode", "zipcode"),
                "location_country": _get(address, "CountryCode", "countryCode")
                or _get(entry, "Country", "country"),
                "raw_payload": frappe.as_json(entry),
            }
        )

    # The parcel-level status is authoritative for the current state — GLS
    # history codes are often opaque. Stamp it onto the newest history event
    # so update_shipment_status (newest event wins) sees e.g. DELIVERED even
    # when no history entry maps cleanly.
    # Only a status we recognise: an unknown word would otherwise overwrite a
    # correctly read event with the "In Transit" fallback.
    parcel_canonical = known_gls_status(parcel_status)
    if collected and parcel_canonical:
        newest = max(collected, key=lambda event: event["event_timestamp"])
        newest["canonical_status"] = parcel_canonical
        newest["carrier_status"] = parcel_status

    return collected


def remap_stored_events() -> dict[str, Any]:
    """Re-read every stored GLS event with the current mapping — canonical
    status and place — and refresh the Shipments behind them.

    A known event is never fetched twice (``upsert_events`` skips it by id),
    so a corrected mapping does not reach the events already in the table by
    itself. Run from a patch after the mapping changed.
    """
    rows = frappe.get_all(
        "Parcel Tracking Event",
        filters={"carrier": GLS},
        fields=[
            "name",
            "shipment",
            "event_code",
            "carrier_status",
            "description",
            "canonical_status",
            "location_city",
            "location_country",
            "raw_payload",
        ],
    )
    changed = 0
    shipments: set[str] = set()
    for row in rows:
        canonical = map_gls_event(row.event_code, row.description)
        # carrier_status differs from the entry's own code only when the
        # parcel-level status was stamped onto the newest event at storing
        # time; that one wins, as it did then.
        if row.carrier_status and row.carrier_status != row.event_code:
            canonical = known_gls_status(row.carrier_status) or canonical
        values: dict[str, Any] = {}
        if canonical != row.canonical_status:
            values["canonical_status"] = canonical
        try:
            entry = json.loads(row.raw_payload or "{}")
        except ValueError:
            entry = {}
        if isinstance(entry, dict):
            if not row.location_city and _depot_city(entry):
                values["location_city"] = _depot_city(entry)
            if not row.location_country and _get(entry, "Country", "country"):
                values["location_country"] = _get(entry, "Country", "country")
        if not values:
            continue
        frappe.db.set_value("Parcel Tracking Event", row.name, values, update_modified=False)
        changed += 1
        if row.shipment:
            shipments.add(row.shipment)
    # reopen: a parcel stored as "Delivered" that was in fact deposited at a
    # ParcelShop has to become "Ready for Pickup" again — and be polled again,
    # which the terminal status had stopped.
    events.update_shipment_statuses(shipments, reopen=True)
    summary = {"events": len(rows), "changed": changed, "shipments": len(shipments)}
    frappe.logger().info(f"[gls-tracking] remap of stored events: {summary}")
    return summary
