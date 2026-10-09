# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Daily tracking digest: one mail listing everything that needs a human.

Four sections, each skipped when empty (no mail at all when everything is
fine — the digest earns attention by staying silent on good days):

1. **Probleme/Retouren** — shipments in Problem/Returned/Lost whose latest
   event is recent (terminal states stay listed for ``stuck_threshold_days``
   so ops sees each at least once, then they age out).
2. **Hängende Sendungen** — open shipments whose newest event (or, without
   any event, whose creation) is older than ``stuck_threshold_days``.
3. **Unzugeordnete Events** — orphan events of the last 24 h; a growing count
   means the awb_number matching assumption needs calibrating.
4. **Fehlgeschlagene Post-Dateien** — Failed Post Tracking File rows of the
   last 24 h.

Configured in Parcel Station Settings (digest_enabled, digest_recipients,
stuck_threshold_days).
"""

from __future__ import annotations

import re

import frappe
from frappe.utils import add_days, cint, cstr, formatdate, get_url_to_form, now_datetime, nowdate

from .status_mapping import PROBLEM_STATUSES, TERMINAL_STATUSES


def send_tracking_digest() -> dict | None:
    """Scheduler entry point (daily, before the workday)."""
    settings = frappe.get_single("Parcel Station Settings")
    if not cint(settings.get("digest_enabled")):
        return None
    recipients = _recipients(settings)
    if not recipients:
        frappe.logger().info("[tracking-digest] skipped: no recipients configured.")
        return None

    try:
        threshold_days = cint(settings.get("stuck_threshold_days")) or 7
        sections = [
            _problem_section(threshold_days),
            _stuck_section(threshold_days, cint(settings.get("tracking_cutoff_days")) or 30),
            _orphan_section(),
            _failed_files_section(),
        ]
        sections = [section for section in sections if section]
        if not sections:
            frappe.logger().info("[tracking-digest] nothing to report.")
            return {"sent": False}

        frappe.sendmail(
            recipients=recipients,
            subject=f"Paket-Tracking Digest {formatdate(nowdate())}",
            message="<h3>Paket-Tracking Digest</h3>" + "".join(sections),
        )
        return {"sent": True, "sections": len(sections)}
    except Exception:
        frappe.log_error(title="Tracking digest failed", message=frappe.get_traceback())
        return None


def _recipients(settings) -> list[str]:
    raw = cstr(settings.get("digest_recipients"))
    return [address.strip() for address in re.split(r"[,;\n]", raw) if address.strip()]


def _shipment_link(name: str) -> str:
    return f'<a href="{get_url_to_form("Shipment", name)}">{name}</a>'


def _latest_event_map(shipment_names: list[str]) -> dict[str, str]:
    """Shipment -> newest event_timestamp, one grouped query."""
    if not shipment_names:
        return {}
    rows = frappe.get_all(
        "Parcel Tracking Event",
        filters={"shipment": ("in", shipment_names)},
        fields=["shipment", "max(event_timestamp) as latest"],
        group_by="shipment",
    )
    return {row.shipment: row.latest for row in rows}


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f'<th style="text-align:left;padding:2px 8px">{h}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td style="padding:2px 8px">{cell}</td>' for cell in row) + "</tr>"
        for row in rows
    )
    return f'<table border="1" cellspacing="0" style="border-collapse:collapse"><tr>{head}</tr>{body}</table>'


def _problem_section(threshold_days: int) -> str:
    cutoff = add_days(now_datetime(), -threshold_days)
    shipments = frappe.get_all(
        "Shipment",
        filters={"docstatus": 1, "tracking_status": ("in", sorted(PROBLEM_STATUSES))},
        fields=["name", "awb_number", "tracking_status", "tracking_status_info"],
    )
    latest = _latest_event_map([row.name for row in shipments])
    rows = [
        [
            _shipment_link(row.name),
            row.tracking_status,
            row.awb_number or "",
            cstr(row.tracking_status_info or ""),
        ]
        for row in shipments
        # keep listing while the newest event is recent, then age out
        if latest.get(row.name) and str(latest[row.name]) >= str(cutoff)
    ]
    if not rows:
        return ""
    return "<h4>Probleme / Retouren</h4>" + _table(["Shipment", "Status", "Tracking-Nr.", "Letzter Stand"], rows)


def _stuck_section(threshold_days: int, cutoff_days: int) -> str:
    window_start = add_days(now_datetime(), -cutoff_days)
    stale_before = add_days(now_datetime(), -threshold_days)
    shipments = frappe.get_all(
        "Shipment",
        filters={
            "docstatus": 1,
            "awb_number": ("is", "set"),
            "tracking_status": ("not in", sorted(TERMINAL_STATUSES | PROBLEM_STATUSES)),
            "creation": (">=", window_start),
        },
        fields=["name", "awb_number", "tracking_status", "creation"],
    )
    latest = _latest_event_map([row.name for row in shipments])
    rows = []
    for row in shipments:
        newest = latest.get(row.name)
        reference = newest or row.creation
        if str(reference) < str(stale_before):
            rows.append(
                [
                    _shipment_link(row.name),
                    row.tracking_status or "",
                    row.awb_number,
                    f"letztes Event: {newest}" if newest else "noch kein Event",
                ]
            )
    if not rows:
        return ""
    return (
        f"<h4>Hängende Sendungen (&gt; {threshold_days} Tage ohne Bewegung)</h4>"
        + _table(["Shipment", "Status", "Tracking-Nr.", "Stand"], rows)
    )


def _orphan_section() -> str:
    since = add_days(now_datetime(), -1)
    orphans = frappe.get_all(
        "Parcel Tracking Event",
        filters={"shipment": ("is", "not set"), "creation": (">=", since)},
        fields=["tracking_number", "reference_ident_code", "carrier"],
    )
    if not orphans:
        return ""
    codes: dict[str, int] = {}
    for row in orphans:
        key = f"{row.carrier}: {row.tracking_number or row.reference_ident_code}"
        codes[key] = codes.get(key, 0) + 1
    top = sorted(codes.items(), key=lambda item: -item[1])[:15]
    listing = "".join(f"<li>{code} ({count} Events)</li>" for code, count in top)
    return (
        f"<h4>Unzugeordnete Events (letzte 24 h: {len(orphans)})</h4>"
        f"<ul>{listing}</ul>"
        "<p>Events ohne passendes Shipment (awb_number). Häufung deutet auf ein Matching-Problem hin.</p>"
    )


def _failed_files_section() -> str:
    since = add_days(now_datetime(), -1)
    failed = frappe.get_all(
        "Post Tracking File",
        filters={"status": "Failed", "fetched_at": (">=", since)},
        fields=["name", "error_message"],
    )
    if not failed:
        return ""
    rows = [
        [f'<a href="{get_url_to_form("Post Tracking File", row.name)}">{row.name}</a>', cstr(row.error_message or "")]
        for row in failed
    ]
    return "<h4>Fehlgeschlagene Post-Dateien (letzte 24 h)</h4>" + _table(["Datei", "Fehler"], rows)
