# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Remind the customer about a parcel that is waiting to be collected.

Post office, Abholstation, GLS ParcelShop, FedEx Hold at Location: the carrier
reports ``Ready for Pickup`` and then waits. Most people collect within a day
or two; the ones who do not are usually the ones who never saw the carrier's
notification card. After the holding period the parcel comes back as a
return, and both sides lose — so once a parcel has been lying there longer
than ``pickup_reminder_days`` (Parcel Station Settings, default 2) the
customer gets one mail from us.

Mechanics, in the order the daily 09:00 run applies them:

* **Candidates** are submitted Shipments whose *current* ``tracking_status``
  is ``Ready for Pickup``. The status is recomputed from the newest event on
  every ingestion, so a parcel collected yesterday is already ``Delivered``
  and never a candidate — the run reads the live state, it does not act on a
  stale snapshot.
* **Waiting since** the earliest ``Ready for Pickup`` event of the shipment
  (calendar days via ``date_diff``, strictly greater than the threshold).
* **One mail per shipment**, guarded by ``Shipment.pickup_reminder_sent_at``
  (Custom Field from ``fixtures/custom_field.json``). A parcel that keeps
  lying there ages into the digest's stuck section for ops — this reminder
  is not an escalation ladder.
* **Recipient** is the Shipment's delivery contact e-mail, falling back to the
  delivery customer's ``email_id``. No address → logged, skipped, no marker
  (the shipment shows up again tomorrow, which is the right behaviour: a
  contact added in the meantime gets the mail).
* **Language** follows ``Customer.language`` (``de`` unless it starts with
  something else); the two Email Templates are configured in Parcel Station
  Settings and seeded by ``patches/v15/seed_pickup_reminder_templates``.
  Without templates a plain built-in text goes out, so a deleted template
  degrades the wording, not the feature.

What the mail says and does NOT say: the pickup location and the tracking
number (the customer needs it at the counter), but **no carrier tracking
link** — the customer journey stays on our own pages (see
``docs/tracking_api.md``) — and no event free text, because carrier remarks
may carry neighbour or recipient names.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import add_days, cint, cstr, date_diff, escape_html, formatdate, getdate, now_datetime, nowdate

from ..carrier_routing import AUSTRIAN_POST, FEDEX, GLS, resolve_carrier
from .status_mapping import STATUS_READY_FOR_PICKUP

SENT_AT_FIELD = "pickup_reminder_sent_at"
DEFAULT_DAYS = 2

#: Customer-facing carrier names, keyed by carrier identity.
CARRIER_LABELS = {
    AUSTRIAN_POST: "Österreichische Post",
    GLS: "GLS",
    FEDEX: "FedEx",
}


def get_settings() -> frappe._dict:
    settings = frappe.get_single("Parcel Station Settings")
    return frappe._dict(
        enabled=cint(settings.get("pickup_reminder_enabled")),
        # Empty on sites that predate the field (Singles keep no row until the
        # first save, so the JSON default never reaches the DB by itself).
        days=cint(settings.get("pickup_reminder_days")) or DEFAULT_DAYS,
        sender=settings.get("pickup_reminder_sender") or None,
        email_template=settings.get("pickup_reminder_email_template") or None,
        email_template_en=settings.get("pickup_reminder_email_template_en") or None,
        cutoff_days=cint(settings.get("tracking_cutoff_days")) or 30,
    )


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def send_pickup_reminders() -> dict[str, Any] | None:
    """Scheduler entry point (daily 09:00 — the customer can collect today)."""
    settings = get_settings()
    if not settings.enabled:
        return None

    summary: dict[str, Any] = {"sent": [], "skipped": 0, "failed": 0}
    for candidate in due_shipments(settings):
        try:
            recipient = send_reminder(candidate, settings)
            if recipient:
                summary["sent"].append(candidate.name)
            else:
                summary["skipped"] += 1
            _commit_if_available()
        except Exception:
            frappe.db.rollback()
            summary["failed"] += 1
            frappe.log_error(
                title=f"Pickup reminder failed for {candidate.name}",
                message=frappe.get_traceback(),
            )
    frappe.logger().info(f"[pickup-reminder] {summary}")
    return summary


@frappe.whitelist()
def preview_pickup_reminders() -> list[dict[str, Any]]:
    """What the next run would mail, without sending anything.

    ``bench execute erpnext_parcel_station.parcel.tracking.pickup_reminder.preview_pickup_reminders``
    """
    frappe.only_for("System Manager")
    settings = get_settings()
    return [
        {
            "shipment": row.name,
            "carrier": CARRIER_LABELS.get(resolve_carrier(row), row.carrier),
            "tracking_number": row.awb_number,
            "ready_since": str(row.ready_since),
            "days_waiting": row.days_waiting,
            "pickup_location": row.pickup_location,
            "recipient": _recipient(row),
            "language": _language(row),
        }
        for row in due_shipments(settings)
    ]


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def due_shipments(settings: frappe._dict | None = None) -> list[frappe._dict]:
    """Ready-for-pickup Shipments that have waited longer than the threshold
    and have not been reminded yet. Each row carries ``ready_since``,
    ``days_waiting`` and ``pickup_location`` for the mail."""
    settings = settings or get_settings()
    shipments = _ready_for_pickup_shipments()
    if not shipments:
        return []

    first_pickup_event = _first_pickup_event_map([row.name for row in shipments])
    today = nowdate()
    # The window is the DEPOSIT date, not the Shipment's creation: a parcel
    # is deposited days after the label, and the reminder must not silently
    # skip a shipment because the label was printed a month earlier.
    window_start = str(add_days(now_datetime(), -settings.cutoff_days))
    due = []
    for row in shipments:
        event = first_pickup_event.get(row.name)
        if not event:
            # Status without an event: set by hand, or the event was purged.
            # Nothing to date the wait from, so nothing to remind about.
            continue
        if str(event.event_timestamp) < window_start:
            continue
        days_waiting = date_diff(today, getdate(event.event_timestamp))
        if days_waiting <= settings.days:
            continue
        row.ready_since = event.event_timestamp
        row.days_waiting = days_waiting
        row.pickup_location = _location(event)
        due.append(row)
    return due


def _ready_for_pickup_shipments() -> list[frappe._dict]:
    filters: dict[str, Any] = {
        "docstatus": 1,
        "tracking_status": STATUS_READY_FOR_PICKUP,
        "awb_number": ("is", "set"),
    }
    if frappe.get_meta("Shipment").has_field(SENT_AT_FIELD):
        filters[SENT_AT_FIELD] = ("is", "not set")
    else:
        # Without the marker every run would mail every waiting parcel again.
        frappe.log_error(
            title="Pickup reminder: marker field missing",
            message=f"Shipment.{SENT_AT_FIELD} is not installed; no reminders sent.",
        )
        return []
    return frappe.get_all(
        "Shipment",
        filters=filters,
        fields=[
            "name",
            "awb_number",
            "carrier",
            "carrier_service",
            "custom_carrier_service",
            "delivery_type",
            "delivery_customer",
            "delivery_contact_name",
            "delivery_contact_email",
        ],
        order_by="creation asc",
    )


def _first_pickup_event_map(shipment_names: list[str]) -> dict[str, frappe._dict]:
    """Shipment -> its earliest Ready-for-Pickup event (timestamp + location).

    The earliest, not the newest: GLS and FedEx return the full history on
    every poll and the Post repeats events across files, so a later copy of
    the same deposit must not restart the clock.
    """
    rows = frappe.get_all(
        "Parcel Tracking Event",
        filters={
            "shipment": ("in", shipment_names),
            "canonical_status": STATUS_READY_FOR_PICKUP,
        },
        fields=["shipment", "event_timestamp", "location_city", "location_postal_code", "location_country"],
        order_by="event_timestamp asc, creation asc",
    )
    first: dict[str, frappe._dict] = {}
    for row in rows:
        first.setdefault(row.shipment, row)
    return first


def _location(event) -> str:
    """"Bregenz-Schendlingen, AT" — the carrier's branch label for the mail.
    Post files carry the pickup office in EventCity, GLS the ParcelShop city."""
    return ", ".join(
        part for part in (event.location_city or event.location_postal_code, event.location_country) if part
    )


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------


def send_reminder(row, settings: frappe._dict | None = None) -> str | None:
    """Mail one shipment's customer and stamp the marker. Returns the
    recipient, or None when the customer has no address."""
    settings = settings or get_settings()
    recipient = _recipient(row)
    if not recipient:
        frappe.logger().warning(
            f"[pickup-reminder] {row.name}: no e-mail address for {row.delivery_customer}; skipped."
        )
        return None

    language = _language(row)
    context = render_context(row, language)
    subject, message = _render_email(settings, language, context)

    kwargs: dict[str, Any] = {
        "recipients": [recipient],
        "subject": subject,
        "message": message,
        "reference_doctype": "Shipment",
        "reference_name": row.name,
    }
    if settings.sender:
        kwargs["sender"] = settings.sender
    frappe.sendmail(**kwargs)
    frappe.db.set_value("Shipment", row.name, SENT_AT_FIELD, now_datetime(), update_modified=False)
    frappe.logger().info(f"[pickup-reminder] {row.name} -> {recipient} ({language})")
    return recipient


def _recipient(row) -> str | None:
    if row.get("delivery_contact_email"):
        return row.delivery_contact_email
    if row.get("delivery_customer"):
        return frappe.db.get_value("Customer", row.delivery_customer, "email_id") or None
    return None


def _language(row) -> str:
    """'de' or 'en' — German is the house default."""
    language = None
    if row.get("delivery_customer"):
        language = frappe.db.get_value("Customer", row.delivery_customer, "language")
    language = cstr(language or "de").lower()
    return "de" if language.startswith("de") else "en"


def render_context(row, language: str) -> dict[str, Any]:
    """Template context: ``doc`` is the Shipment, ``pickup`` the facts of the
    deposit, ``orders`` the customer's order numbers behind this parcel,
    ``company`` the sender's name for the footer."""
    carrier_identity = resolve_carrier(row)
    return {
        "doc": frappe.get_doc("Shipment", row.name),
        "customer_name": _customer_name(row),
        "company": frappe.defaults.get_global_default("company") or "",
        "orders": _sales_orders(row.name),
        "pickup": {
            "carrier": CARRIER_LABELS.get(carrier_identity, row.carrier or carrier_identity),
            "tracking_number": row.awb_number,
            "location": row.pickup_location,
            "ready_since": row.ready_since,
            "ready_since_formatted": formatdate(getdate(row.ready_since)),
            "days_waiting": row.days_waiting,
        },
        "language": language,
    }


def _customer_name(row) -> str:
    """Contact first, customer master second, then the customer id."""
    if row.get("delivery_contact_name"):
        contact = frappe.db.get_value("Contact", row.delivery_contact_name, ["first_name", "last_name"], as_dict=True)
        if contact:
            name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
            if name:
                return name
    if row.get("delivery_customer"):
        return frappe.db.get_value("Customer", row.delivery_customer, "customer_name") or row.delivery_customer
    return ""


def _sales_orders(shipment_name: str) -> list[str]:
    delivery_notes = frappe.get_all(
        "Shipment Delivery Note", filters={"parent": shipment_name}, pluck="delivery_note", distinct=True
    )
    if not delivery_notes:
        return []
    orders = frappe.get_all(
        "Delivery Note Item",
        filters={"parent": ("in", delivery_notes), "against_sales_order": ("is", "set")},
        pluck="against_sales_order",
        distinct=True,
    )
    return sorted(set(orders))


def _render_email(settings: frappe._dict, language: str, context: dict[str, Any]) -> tuple[str, str]:
    template = settings.email_template if language == "de" else (settings.email_template_en or settings.email_template)
    if template and frappe.db.exists("Email Template", template):
        from frappe.email.doctype.email_template.email_template import get_email_template

        values = get_email_template(template, context)
        if values.get("subject") and values.get("message"):
            return values["subject"], values["message"]
    return fallback_email(language, context)


def fallback_email(language: str, context: dict[str, Any]) -> tuple[str, str]:
    """Plain wording for the case that the templates were deleted in the Desk."""
    pickup = context["pickup"]
    name = escape_html(context.get("customer_name") or "")
    orders = ", ".join(escape_html(o) for o in context.get("orders") or [])
    location = escape_html(pickup["location"] or "")
    company = escape_html(context.get("company") or "")
    if language == "de":
        subject = f"Ihr Paket wartet auf Sie – {pickup['carrier']} {pickup['tracking_number']}"
        lines = [
            f"Guten Tag {name},",
            f"Ihr Paket{(' zur Bestellung ' + orders) if orders else ''} liegt seit dem "
            f"{pickup['ready_since_formatted']} bei {escape_html(pickup['carrier'])} zur Abholung bereit"
            + (f" ({location})" if location else "")
            + ".",
            f"Sendungsnummer: <b>{escape_html(pickup['tracking_number'])}</b>",
            "Bitte holen Sie es in den nächsten Tagen ab – nach Ablauf der Aufbewahrungsfrist "
            "geht es sonst an uns zurück.",
            "Falls Sie das Paket inzwischen abgeholt haben, betrachten Sie diese E-Mail bitte als gegenstandslos.",
            f"Herzliche Grüße<br>{company}",
        ]
    else:
        subject = f"Your parcel is waiting for you – {pickup['carrier']} {pickup['tracking_number']}"
        lines = [
            f"Hi {name},",
            f"your parcel{(' for order ' + orders) if orders else ''} has been ready for collection at "
            f"{escape_html(pickup['carrier'])} since {pickup['ready_since_formatted']}"
            + (f" ({location})" if location else "")
            + ".",
            f"Tracking number: <b>{escape_html(pickup['tracking_number'])}</b>",
            "Please collect it within the next few days – once the holding period ends it is returned to us.",
            "If you have collected the parcel in the meantime, please disregard this email.",
            f"Best regards<br>{company}",
        ]
    return subject, "<br><br>".join(lines)


def _commit_if_available() -> None:
    if getattr(frappe, "db", None) and hasattr(frappe.db, "commit"):
        frappe.db.commit()
