# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Poll the FedEx Track API for open shipments.

FedEx offers no push mechanism — tracking is poll-only, batched at 30 numbers
per request (``fedex_api.TRACK_BATCH_SIZE``). The response repeats the full
scan history each time, so almost every returned event is already stored;
``events.upsert_events`` treats that as the normal case.

Gate: ``FedEx Settings -> Enable FedEx Tracking Poll`` on top of the general
``Enable FedEx Integration`` (label creation may be live long before anyone
wants the poll traffic, and vice versa during testing).
"""

from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import cint

from ..carrier_routing import FEDEX
from ..infra.fedex_api import TRACK_BATCH_SIZE, FedExAPIClient, FedExNotConfiguredError
from . import events
from .status_mapping import map_fedex_code


def poll_fedex_tracking() -> dict[str, Any] | None:
    """Scheduler entry point."""
    if not cint(frappe.db.get_single_value("FedEx Settings", "enable_fedex_tracking")):
        frappe.logger().info("[fedex-tracking] skipped: poll disabled in FedEx Settings.")
        return None

    try:
        # The Track API lives in its own FedEx Developer Portal project with
        # its own key pair — the Ship credentials get 403 on /track/v1.
        client = FedExAPIClient.from_tracking_settings()
    except FedExNotConfiguredError as exc:
        frappe.logger().info(f"[fedex-tracking] skipped: {exc}")
        return None

    open_rows = events.open_shipments(FEDEX)
    by_awb = {row.awb_number: row.name for row in open_rows}
    summary: dict[str, Any] = {"shipments": len(open_rows), "inserted": 0, "failed_batches": 0}

    numbers = list(by_awb)
    touched: set[str] = set()
    for start in range(0, len(numbers), TRACK_BATCH_SIZE):
        batch = numbers[start : start + TRACK_BATCH_SIZE]
        try:
            response = client.track_shipments(batch)
            batch_events = _events_from_response(response, by_awb)
            result = events.upsert_events(batch_events)
            summary["inserted"] += result["inserted"]
            touched |= result["shipments"]
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            summary["failed_batches"] += 1
            frappe.log_error(
                title="FedEx tracking: batch failed",
                message=f"Tracking numbers: {', '.join(batch)}\n\n{frappe.get_traceback()}",
            )
            frappe.db.commit()

    events.update_shipment_statuses(touched)
    frappe.db.commit()
    frappe.logger().info(f"[fedex-tracking] run done: {summary}")
    return summary


def _events_from_response(response: dict, by_awb: dict[str, str]) -> list[dict[str, Any]]:
    """Flatten a Track API response into Parcel Tracking Event dicts.

    Per FedEx: ``output.completeTrackResults[].trackResults[]`` with
    ``scanEvents[]`` (history) and ``latestStatusDetail`` (current state).
    Only the scan events are stored — the latest status is just the newest
    scan, and storing it twice would need a second dedup rule.
    """
    collected: list[dict[str, Any]] = []
    output = (response or {}).get("output") or {}
    for complete in output.get("completeTrackResults") or []:
        tracking_number = complete.get("trackingNumber") or ""
        shipment = by_awb.get(tracking_number)
        for track_result in complete.get("trackResults") or []:
            if (track_result.get("error") or {}).get("code"):
                # e.g. TRACKING.TRACKINGNUMBER.NOTFOUND right after label
                # creation — normal for freshly labelled parcels, not an error.
                continue
            for scan in track_result.get("scanEvents") or []:
                collected.append(_scan_to_event(tracking_number, shipment, scan))
    return collected


def _scan_to_event(tracking_number: str, shipment: str | None, scan: dict) -> dict[str, Any]:
    timestamp = _normalize_timestamp(scan.get("date") or "")
    event_type = scan.get("eventType") or ""
    derived = scan.get("derivedStatusCode") or ""
    description = scan.get("eventDescription") or ""
    location = scan.get("scanLocation") or {}

    digest = hashlib.sha1(
        f"{timestamp}|{event_type}|{derived}|{description}".encode("utf-8")
    ).hexdigest()[:16]

    return {
        "carrier": FEDEX,
        "event_id": f"FEDEX::{tracking_number}::{digest}",
        "tracking_number": tracking_number,
        "shipment": shipment,
        "event_timestamp": timestamp,
        "event_code": event_type,
        "carrier_status": derived,
        "canonical_status": map_fedex_code(derived or event_type),
        "description": description[:500],
        "location_city": location.get("city") or "",
        "location_postal_code": location.get("postalCode") or "",
        "location_country": location.get("countryCode") or "",
        "raw_payload": frappe.as_json(scan),
    }


def _normalize_timestamp(value: str) -> str:
    """FedEx sends ISO 8601 with offset ('2026-09-05T14:20:00+02:00')."""
    value = value.strip().replace("T", " ")
    for separator in ("+", "Z"):
        if separator in value:
            value = value.split(separator, 1)[0]
    # A trailing offset like ' -04:00' after the space split is rare; keep the
    # date-time prefix frappe's Datetime accepts.
    return value[:19]
