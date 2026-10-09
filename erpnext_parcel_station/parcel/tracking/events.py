# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Store carrier tracking events and maintain Shipment.tracking_status.

All three ingestion paths (Post SFTP files, FedEx poll, GLS poll) deliver
plain event dicts to :func:`upsert_events`; everything stateful lives here:

* **Dedup** — ``Parcel Tracking Event.event_id`` is unique. Post files
  re-deliver events across files, FedEx/GLS return the full scan history on
  every poll, so "already stored" is the normal case, not an error.
* **Matching** — an event belongs to the Shipment whose ``awb_number`` equals
  the event's tracking code. Post events carry two codes (``IdentCode`` may be
  a partner-network code, ``ReferenceIdentCode`` the Post's own), either may
  be the stored one. Events that match nothing are stored as **orphans** and
  re-matched later (:func:`rematch_orphan_events`) — no data is dropped while
  the matching assumptions are calibrated against real files.
* **Status** — :func:`update_shipment_status` derives the canonical status
  from the newest matched event. Terminal statuses are sticky: a re-delivered
  transit event must not "un-deliver" a parcel. Written via
  ``frappe.db.set_value`` because Shipments are submitted documents (same
  style as the ``awb_number`` write in ``infra/gls_integration.py``).
"""

from __future__ import annotations

from typing import Any, Iterable

import frappe
from frappe.utils import add_days, cint, get_datetime, now_datetime

from ..carrier_routing import AUSTRIAN_POST, FEDEX, GLS, resolve_carrier
from .status_mapping import TERMINAL_STATUSES

#: Public carrier tracking pages, keyed by carrier identity. Used for
#: Shipment.tracking_url in the Desk and for the "track at the carrier" button
#: on the customer's tracking page (tracking.api.get_tracking_page). Mails and
#: get_order_tracking deliberately carry no carrier link.
TRACKING_URLS = {
    AUSTRIAN_POST: "https://www.post.at/sv/sendungsdetails?snr={tracking}",
    # Verified 2026-10-02. The former ".../AT/de/paketverfolgung" answers with
    # a redirect to GLS's 404 page.
    GLS: "https://gls-group.com/AT/de/paket-verfolgen?match={tracking}",
    FEDEX: "https://www.fedex.com/fedextrack/?trknbr={tracking}",
}


def tracking_url_for(carrier_identity: str, tracking_number: str | None) -> str | None:
    template = TRACKING_URLS.get(carrier_identity)
    if not template or not tracking_number:
        return None
    return template.format(tracking=tracking_number)


# ---------------------------------------------------------------------------
# storing events
# ---------------------------------------------------------------------------


def upsert_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Insert new events, skip known ones, match Shipments where possible.

    Returns a summary dict: ``inserted`` / ``duplicates`` / ``matched`` /
    ``orphans`` counters plus ``shipments`` — the set of Shipment names that
    received new events (callers refresh those via
    :func:`update_shipment_status`).
    """
    summary: dict[str, Any] = {
        "inserted": 0,
        "duplicates": 0,
        "matched": 0,
        "orphans": 0,
        "shipments": set(),
    }

    for event in events:
        event_id = event.get("event_id")
        if not event_id:
            continue
        if frappe.db.exists("Parcel Tracking Event", {"event_id": event_id}):
            summary["duplicates"] += 1
            continue

        shipment = event.get("shipment") or find_shipment(
            event.get("tracking_number"), event.get("reference_ident_code")
        )

        doc = frappe.get_doc({"doctype": "Parcel Tracking Event", **event, "shipment": shipment})
        try:
            doc.insert(ignore_permissions=True)
        except frappe.UniqueValidationError:
            # Two files in one batch carrying the same event, or a concurrent
            # worker — the unique index is the arbiter, losing is fine.
            summary["duplicates"] += 1
            continue

        summary["inserted"] += 1
        if shipment:
            summary["matched"] += 1
            summary["shipments"].add(shipment)
        else:
            summary["orphans"] += 1

    return summary


def find_shipment(*codes: str | None) -> str | None:
    """The submitted Shipment whose ``awb_number`` equals one of ``codes``."""
    candidates = [c for c in codes if c]
    if not candidates:
        return None
    rows = frappe.get_all(
        "Shipment",
        filters={"awb_number": ("in", candidates), "docstatus": 1},
        pluck="name",
        limit=1,
    )
    return rows[0] if rows else None


def rematch_orphan_events() -> set[str]:
    """Attach stored orphan events to Shipments that exist (or got their
    awb_number) by now. Returns the Shipment names that gained events.

    Bounded by the tracking cutoff so ancient unmatchable events (partner
    codes of pre-ERPNext shipments) stop being rescanned forever.
    """
    cutoff = add_days(now_datetime(), -_cutoff_days())
    orphans = frappe.get_all(
        "Parcel Tracking Event",
        filters={"shipment": ("is", "not set"), "event_timestamp": (">=", cutoff)},
        fields=["name", "tracking_number", "reference_ident_code"],
    )

    touched: set[str] = set()
    for orphan in orphans:
        shipment = find_shipment(orphan.tracking_number, orphan.reference_ident_code)
        if shipment:
            frappe.db.set_value(
                "Parcel Tracking Event", orphan.name, "shipment", shipment, update_modified=False
            )
            touched.add(shipment)
    return touched


# ---------------------------------------------------------------------------
# canonical status on the Shipment
# ---------------------------------------------------------------------------


def update_shipment_status(shipment_name: str, reopen: bool = False) -> str | None:
    """Recompute tracking_status/-_info/-_url from the newest stored event.

    ``reopen`` lifts the stickiness of a terminal status. It is for the one
    case where the stored status was wrong rather than outdated: events that
    were re-read with a corrected mapping.

    Returns the status written, or None when nothing changed.
    """
    latest = frappe.get_all(
        "Parcel Tracking Event",
        filters={"shipment": shipment_name},
        fields=["canonical_status", "event_timestamp", "description", "location_city", "location_country"],
        order_by="event_timestamp desc, creation desc",
        limit=1,
    )
    if not latest:
        return None
    newest = latest[0]
    new_status = newest.canonical_status
    if not new_status:
        return None

    current = frappe.db.get_value("Shipment", shipment_name, ["tracking_status", "awb_number"], as_dict=True)
    if not current:
        return None

    # Sticky terminals: once Delivered/Returned/Lost, only another terminal
    # event (e.g. a return booked after a delivery dispute) may change it.
    if not reopen and current.tracking_status in TERMINAL_STATUSES and new_status not in TERMINAL_STATUSES:
        return None

    location = ", ".join(p for p in (newest.location_city, newest.location_country) if p)
    info_parts = [str(get_datetime(newest.event_timestamp)), new_status]
    if location:
        info_parts.append(location)
    if newest.description:
        info_parts.append(newest.description)
    # tracking_status_info is ERPNext's own Data field (varchar(140)); a long
    # carrier free text (GLS PaketShop notes) used to overflow it. The full
    # text stays on the Parcel Tracking Event.
    info = " | ".join(info_parts)[:140]

    values: dict[str, Any] = {"tracking_status": new_status, "tracking_status_info": info}
    url = tracking_url_for(_shipment_carrier(shipment_name), current.awb_number)
    if url:
        values["tracking_url"] = url

    frappe.db.set_value("Shipment", shipment_name, values, update_modified=False)
    frappe.logger().info(f"[tracking] Shipment={shipment_name} tracking_status={new_status}")
    return new_status


def update_shipment_statuses(shipment_names: Iterable[str], reopen: bool = False) -> None:
    for name in shipment_names:
        try:
            update_shipment_status(name, reopen=reopen)
        except Exception:
            frappe.log_error(
                title=f"Tracking: status update failed for {name}",
                message=frappe.get_traceback(),
            )


def _shipment_carrier(shipment_name: str) -> str:
    row = frappe.db.get_value(
        "Shipment",
        shipment_name,
        ["name", "carrier", "carrier_service", "custom_carrier_service", "delivery_type"],
        as_dict=True,
    )
    return resolve_carrier(row or {})


# ---------------------------------------------------------------------------
# poll selection
# ---------------------------------------------------------------------------


def open_shipments(carrier_identity: str) -> list[dict[str, Any]]:
    """Submitted Shipments of one carrier still worth polling.

    "Open" = has a tracking number, not in a terminal status, and created
    within the cutoff window. The carrier column is free text ("Österreichische
    Post"), so the filter runs through ``resolve_carrier`` instead of trusting
    the raw value.
    """
    cutoff = add_days(now_datetime(), -_cutoff_days())
    rows = frappe.get_all(
        "Shipment",
        filters={
            "docstatus": 1,
            "awb_number": ("is", "set"),
            "tracking_status": ("not in", sorted(TERMINAL_STATUSES)),
            "creation": (">=", cutoff),
        },
        fields=["name", "awb_number", "carrier", "carrier_service", "custom_carrier_service", "delivery_type"],
        order_by="creation asc",
    )
    return [row for row in rows if resolve_carrier(row) == carrier_identity]


def _cutoff_days() -> int:
    return (
        cint(frappe.db.get_single_value("Parcel Station Settings", "tracking_cutoff_days")) or 30
    )


def code_inventory() -> list[dict[str, Any]]:
    """Distinct raw-code combinations across all stored events, with counts.

    Calibration helper (``bench execute ...events.code_inventory``): the
    carriers' official code lists are gated or incomplete — the Austrian Post
    Event-/Reason-Liste is a restricted customer document — so the mapping
    tables in ``status_mapping.py`` are tuned against what the carriers
    actually send. Run on a site with real events and compare the
    ``canonical_status`` column against what the codes turn out to mean.
    """
    return frappe.get_all(
        "Parcel Tracking Event",
        fields=[
            "carrier",
            "event_code",
            "reason_code",
            "carrier_status",
            "canonical_status",
            "count(name) as events",
        ],
        group_by="carrier, event_code, reason_code, carrier_status, canonical_status",
        order_by="carrier asc, events desc",
    )
