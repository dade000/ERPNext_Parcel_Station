# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Tell the customer that their parcel is on its way.

A Shipment gets its tracking number when the label is printed at the parcel
station. From then on the customer wants to know three things: that it left,
what is in it, and where to follow it. This module mails exactly that.

Mechanics, in the order the run (12:30 and 17:30) applies them:

* **Candidates** are submitted Shipments to a customer that carry a tracking
  number (``awb_number``) and have not been announced yet. Deliberately a
  batch run rather than a hook on the label call: a label that is cancelled
  or reprinted within the hour must not have mailed the customer already.
* **Nothing from before the switch was turned on.** Only Shipments created
  since ``shipping_notification_start`` are considered; the timestamp is set
  when the feature is enabled, so a deploy never mails the backlog.
* **One mail per recipient and run.** Several parcels to the same customer
  on the same day arrive as one mail listing each parcel.
* **One marker per Shipment**, ``Shipment.shipping_notification_sent_at``
  (Custom Field from ``fixtures/custom_field.json``), stamped only after the
  mail was queued. No address → logged, skipped, no marker; the shipment
  shows up again in the next run.
* **Recipient and language** as in the pickup reminder: delivery contact
  e-mail, then the customer master; ``Customer.language``, German by default.
* **Tracking link** points to our own shop page, never to the carrier: one
  unguessable token per Sales Order (``Sales Order.custom_tracking_page_token``)
  filled into ``shipping_notification_tracking_page_url``. The page is served
  by the webshop from ``tracking.api.get_tracking_page``. Without a configured
  URL, or for a parcel without a Sales Order, the mail simply has no link.
* **Delivery Note PDF** attached per parcel. A PDF that cannot be rendered is
  logged and left out — the customer is better served by a mail without the
  attachment than by no mail at all.

The Email Templates are seeded by
``patches/v15/seed_shipping_notification_templates``; without them a plain
built-in text goes out.
"""

from __future__ import annotations

import secrets
from typing import Any

import frappe
from frappe.utils import cint, cstr, escape_html, flt, now_datetime

from ..carrier_routing import PARCELSHOP_DELIVERY_TYPE, resolve_carrier
from .pickup_reminder import CARRIER_LABELS, _customer_name, _language, _recipient

SETTINGS_DOCTYPE = "Parcel Station Settings"
SENT_AT_FIELD = "shipping_notification_sent_at"
TOKEN_FIELD = "custom_tracking_page_token"


def get_settings() -> frappe._dict:
    # get_singles_dict, not get_single: a Check that was never saved reads as
    # 0 through the document, which would silently turn the attachment off.
    values = frappe.db.get_singles_dict(SETTINGS_DOCTYPE)
    attach = values.get("shipping_notification_attach_delivery_note")
    return frappe._dict(
        enabled=cint(values.get("shipping_notification_enabled")),
        start=values.get("shipping_notification_start") or None,
        sender=values.get("shipping_notification_sender") or None,
        email_template=values.get("shipping_notification_email_template") or None,
        email_template_en=values.get("shipping_notification_email_template_en") or None,
        attach_delivery_note=1 if attach is None else cint(attach),
        print_format=values.get("shipping_notification_print_format") or None,
        tracking_page_url=cstr(values.get("shipping_notification_tracking_page_url")).strip() or None,
    )


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def send_shipping_notifications() -> dict[str, Any] | None:
    """Scheduler entry point (12:30 and 17:30)."""
    settings = get_settings()
    if not settings.enabled:
        return None
    if not settings.start:
        # Enabled past the form (import, console): start now rather than
        # mailing every Shipment that ever got a label.
        frappe.db.set_single_value(SETTINGS_DOCTYPE, "shipping_notification_start", now_datetime())
        _commit_if_available()
        return None

    summary: dict[str, Any] = {"sent": [], "skipped": [], "failed": 0}
    shipments = pending_shipments(settings)
    for row in shipments:
        if not _recipient(row):
            summary["skipped"].append(row.name)
            frappe.logger().warning(
                f"[shipping-notification] {row.name}: no e-mail address for {row.delivery_customer}; skipped."
            )

    for group in group_by_recipient(shipments):
        names = [row.name for row in group.shipments]
        try:
            send_notification(group, settings)
            summary["sent"].extend(names)
            _commit_if_available()
        except Exception:
            frappe.db.rollback()
            summary["failed"] += len(names)
            frappe.log_error(
                title=f"Shipping notification failed for {', '.join(names)}"[:140],
                message=frappe.get_traceback(),
            )
    frappe.logger().info(f"[shipping-notification] {summary}")
    return summary


@frappe.whitelist()
def preview_shipping_notifications() -> list[dict[str, Any]]:
    """What the next run would mail, without sending anything.

    ``bench execute erpnext_parcel_station.parcel.tracking.shipping_notification.preview_shipping_notifications``
    """
    frappe.only_for("System Manager")
    settings = get_settings()
    if not settings.start:
        return []
    return [
        {
            "recipient": group.recipient,
            "language": group.language,
            "shipments": [
                {
                    "shipment": row.name,
                    "carrier": CARRIER_LABELS.get(resolve_carrier(row), row.carrier),
                    "tracking_number": row.awb_number,
                    "orders": sales_orders_by_delivery_note(_delivery_notes(row.name)),
                }
                for row in group.shipments
            ],
        }
        for group in group_by_recipient(pending_shipments(settings))
    ]


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def pending_shipments(settings: frappe._dict | None = None) -> list[frappe._dict]:
    """Labelled Shipments to a customer, created since the start timestamp,
    that have not been announced yet."""
    settings = settings or get_settings()
    if not settings.start:
        return []
    if not frappe.get_meta("Shipment").has_field(SENT_AT_FIELD):
        # Without the marker every run would mail every parcel again.
        frappe.log_error(
            title="Shipping notification: marker field missing",
            message=f"Shipment.{SENT_AT_FIELD} is not installed; no notifications sent.",
        )
        return []
    return frappe.get_all(
        "Shipment",
        filters={
            "docstatus": 1,
            "awb_number": ("is", "set"),
            "delivery_to_type": "Customer",
            "creation": (">=", settings.start),
            SENT_AT_FIELD: ("is", "not set"),
        },
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
            "delivery_address_name",
        ],
        order_by="creation asc",
    )


def group_by_recipient(shipments: list[frappe._dict]) -> list[frappe._dict]:
    """One group per recipient address (case-insensitive), in the order the
    first parcel of each was labelled. Shipments without an address drop out."""
    groups: dict[str, frappe._dict] = {}
    for row in shipments:
        recipient = _recipient(row)
        if not recipient:
            continue
        key = recipient.strip().lower()
        if key not in groups:
            groups[key] = frappe._dict(recipient=recipient.strip(), language=_language(row), shipments=[])
        groups[key].shipments.append(row)
    return list(groups.values())


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------


def send_notification(group: frappe._dict, settings: frappe._dict | None = None) -> str:
    """Mail one recipient about all their parcels and stamp the markers."""
    settings = settings or get_settings()
    context = render_context(group, settings)
    attachments = []
    if settings.attach_delivery_note:
        attachments = delivery_note_attachments(context["delivery_notes"], settings, group.language)
    # Rendered after the PDFs: the mail only mentions an attachment it has.
    context["has_attachment"] = bool(attachments)
    subject, message = _render_email(settings, group.language, context)

    kwargs: dict[str, Any] = {
        "recipients": [group.recipient],
        "subject": subject,
        "message": message,
        "reference_doctype": "Shipment",
        "reference_name": group.shipments[0].name,
    }
    if settings.sender:
        kwargs["sender"] = settings.sender
    if attachments:
        kwargs["attachments"] = attachments
    frappe.sendmail(**kwargs)

    sent_at = now_datetime()
    for row in group.shipments:
        frappe.db.set_value("Shipment", row.name, SENT_AT_FIELD, sent_at, update_modified=False)
    names = ", ".join(row.name for row in group.shipments)
    frappe.logger().info(f"[shipping-notification] {names} -> {group.recipient} ({group.language})")
    return group.recipient


def render_context(group: frappe._dict, settings: frappe._dict | None = None) -> dict[str, Any]:
    """Template context.

    ``shipments`` is the list the mail loops over; each entry carries
    ``carrier``, ``tracking_number``, ``orders``, ``items`` (``item_name``,
    ``item_code``, ``qty``, ``uom``), ``address_lines``, ``is_parcel_shop``
    and ``tracking_url`` (our shop page, or None). ``orders`` and
    ``tracking_links`` (``order``, ``url``) span the whole mail. ``doc`` is
    the first Shipment, for templates that want a document. ``has_attachment``
    is added by ``send_notification`` once the PDFs are rendered.
    """
    settings = settings or get_settings()
    language = group.language
    links: dict[str, str] = {}
    shipments = []
    delivery_notes: list[str] = []

    for row in group.shipments:
        notes = _delivery_notes(row.name)
        orders = sales_orders_by_delivery_note(notes)
        for order in orders:
            if order not in links:
                url = tracking_page_url(order, language, settings)
                if url:
                    links[order] = url
        carrier_identity = resolve_carrier(row)
        address = _address(row.get("delivery_address_name"), language)
        shipments.append(
            {
                "name": row.name,
                "carrier": CARRIER_LABELS.get(carrier_identity, row.carrier or carrier_identity),
                "tracking_number": row.awb_number,
                "orders": orders,
                "items": parcel_items(row.name, notes),
                "address_lines": address.lines,
                "is_parcel_shop": address.is_parcel_shop or row.get("delivery_type") == PARCELSHOP_DELIVERY_TYPE,
                "tracking_url": next((links[order] for order in orders if order in links), None),
            }
        )
        delivery_notes.extend(note for note in notes if note not in delivery_notes)

    first = group.shipments[0]
    all_orders = sorted({order for shipment in shipments for order in shipment["orders"]})
    return {
        "doc": frappe.get_doc("Shipment", first.name),
        "customer_name": _customer_name(first),
        "company": frappe.defaults.get_global_default("company") or "",
        "shipments": shipments,
        "parcel_count": len(shipments),
        "orders": all_orders,
        "tracking_links": [{"order": order, "url": links[order]} for order in all_orders if order in links],
        "delivery_notes": delivery_notes,
        "has_attachment": False,
        "language": language,
    }


def _delivery_notes(shipment_name: str) -> list[str]:
    notes = frappe.get_all(
        "Shipment Delivery Note",
        filters={"parent": shipment_name},
        pluck="delivery_note",
        order_by="idx asc",
    )
    # The parcel station can list the same Delivery Note twice on a Shipment.
    return list(dict.fromkeys(note for note in notes if note))


def sales_orders_by_delivery_note(delivery_notes: list[str]) -> list[str]:
    if not delivery_notes:
        return []
    orders = frappe.get_all(
        "Delivery Note Item",
        filters={"parent": ("in", delivery_notes), "against_sales_order": ("is", "set")},
        pluck="against_sales_order",
        distinct=True,
    )
    return sorted(set(orders))


def parcel_items(shipment_name: str, delivery_notes: list[str]) -> list[dict[str, Any]]:
    """What the customer finds in this parcel.

    The rows scanned at the station when there are any — a split delivery
    then lists per parcel what is actually in it — otherwise the goods lines
    of the Delivery Note. Charges (freight, import duty) are not contents.
    """
    rows: list[Any] = []
    if frappe.db.table_exists("Parcel Shipment Item"):
        rows = frappe.db.sql(
            """
            select item_code, item_name, qty, uom
            from `tabParcel Shipment Item`
            where shipment = %s
            order by idx
            """,
            shipment_name,
            as_dict=True,
        )
    if not rows and delivery_notes:
        lines = frappe.get_all(
            "Delivery Note Item",
            filters={"parent": ("in", delivery_notes)},
            fields=["name", "parent", "item_code", "item_name", "qty", "uom"],
            order_by="parent asc, idx asc",
        )
        charges = _charge_item_codes({line.item_code for line in lines})
        rows = [line for line in lines if line.item_code not in charges]
        # A repair line names the shoe, not the service (hook).
        from ..shipment_contents import apply_line_overrides, line_overrides

        for note in delivery_notes:
            apply_line_overrides(rows, line_overrides(note, rows), key="name")

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (cstr(row.get("item_code")), cstr(row.get("uom")))
        if key not in merged:
            merged[key] = {
                "item_code": row.get("item_code"),
                "item_name": row.get("item_name") or row.get("item_code"),
                "qty": 0.0,
                "uom": row.get("uom"),
            }
        merged[key]["qty"] += flt(row.get("qty"))
    for item in merged.values():
        qty = item["qty"]
        item["qty"] = int(qty) if qty == int(qty) else qty
    return list(merged.values())


def _charge_item_codes(item_codes: set[str]) -> set[str]:
    """Non-stock lines that are money, not goods. A Product Bundle is
    non-stock too, but it is what the customer ordered — it stays."""
    codes = {code for code in item_codes if code}
    if not codes:
        return set()
    from ..shipment_contents import packable_non_stock_groups

    goods_groups = packable_non_stock_groups()
    non_stock = {
        row["name"]
        for row in frappe.get_all(
            "Item",
            filters={"name": ("in", list(codes)), "is_stock_item": 0},
            fields=["name", "item_group"],
        )
        # A repair line is a service, but the customer gets a shoe back:
        # it belongs in the "what is in the parcel" list of the mail.
        if row.get("item_group") not in goods_groups
    }
    if not non_stock:
        return set()
    bundles = set(
        frappe.get_all("Product Bundle", filters={"new_item_code": ("in", list(non_stock))}, pluck="new_item_code")
    )
    return non_stock - bundles


def _address(address_name: str | None, language: str) -> frappe._dict:
    """Delivery address as plain lines — built from the fields rather than
    taken from the rendered display, so the template can escape each line."""
    empty = frappe._dict(lines=[], is_parcel_shop=False)
    if not address_name:
        return empty
    fields = ["address_title", "address_line1", "address_line2", "pincode", "city", "country"]
    has_shop_flag = frappe.get_meta("Address").has_field("is_parcel_shop")
    if has_shop_flag:
        fields.append("is_parcel_shop")
    address = frappe.db.get_value("Address", address_name, fields, as_dict=True)
    if not address:
        return empty
    country = frappe._(address.country, lang=language) if address.country else ""
    lines = [
        address.address_title,
        address.address_line1,
        address.address_line2,
        " ".join(part for part in (address.pincode, address.city) if part),
        country,
    ]
    return frappe._dict(
        lines=[cstr(line).strip() for line in lines if cstr(line).strip()],
        is_parcel_shop=bool(has_shop_flag and cint(address.get("is_parcel_shop"))),
    )


# ---------------------------------------------------------------------------
# tracking page link
# ---------------------------------------------------------------------------


def tracking_page_url(sales_order: str, language: str, settings: frappe._dict | None = None) -> str | None:
    """Link to the shop's tracking page for this order, or None when no page
    URL is configured. Placeholders: ``{token}`` and ``{language}``."""
    settings = settings or get_settings()
    template = settings.tracking_page_url
    if not template or "{token}" not in template:
        return None
    token = ensure_tracking_token(sales_order)
    if not token:
        return None
    return template.replace("{token}", token).replace("{language}", language)


def ensure_tracking_token(sales_order: str) -> str | None:
    """The order's tracking-page token, created on first use.

    Random and unguessable: whoever holds the link sees the order's parcels,
    so the order number itself must not be enough to build it.
    """
    if not frappe.get_meta("Sales Order").has_field(TOKEN_FIELD):
        return None
    if not frappe.db.exists("Sales Order", sales_order):
        return None
    token = frappe.db.get_value("Sales Order", sales_order, TOKEN_FIELD)
    if not token:
        token = secrets.token_urlsafe(24)
        frappe.db.set_value("Sales Order", sales_order, TOKEN_FIELD, token, update_modified=False)
    return token


# ---------------------------------------------------------------------------
# attachment and wording
# ---------------------------------------------------------------------------


def delivery_note_attachments(delivery_notes: list[str], settings: frappe._dict, language: str) -> list[dict[str, Any]]:
    """Delivery Note PDFs, each in the language of its document. A note that
    fails to render is logged and left out; the mail goes without it."""
    attachments = []
    for note in delivery_notes:
        try:
            lang = frappe.db.get_value("Delivery Note", note, "language") or language
            attachments.append(
                frappe.attach_print("Delivery Note", note, print_format=settings.print_format, lang=lang)
            )
        except Exception:
            frappe.log_error(
                title=f"Shipping notification: PDF failed for {note}",
                message=frappe.get_traceback(),
            )
    return attachments


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
    german = language == "de"
    name = escape_html(context.get("customer_name") or "")
    company = escape_html(context.get("company") or "")
    orders = ", ".join(context.get("orders") or [])

    if german:
        subject = f"Ihre Bestellung ist unterwegs{(' – ' + orders) if orders else ''}"
        lines = [f"Guten Tag {name},", "wir haben Ihre Bestellung an den Zusteller übergeben."]
        labels = ("Zusteller", "Sendungsnummer", "Bestellung", "Inhalt", "Lieferadresse", "Paketshop", "Sendung verfolgen")
        closing = f"Herzliche Grüße<br>{company}"
    else:
        subject = f"Your order is on its way{(' – ' + orders) if orders else ''}"
        lines = [f"Hi {name},", "we have handed your order over to the carrier."]
        labels = ("Carrier", "Tracking number", "Order", "Contents", "Delivery address", "Parcel shop", "Track your parcel")
        closing = f"Best regards<br>{company}"
    carrier, tracking, order, contents, address, shop, track = labels

    for shipment in context["shipments"]:
        block = [
            f"{carrier}: {escape_html(shipment['carrier'])}",
            f"{tracking}: <b>{escape_html(shipment['tracking_number'])}</b>",
        ]
        if shipment["orders"]:
            block.append(f"{order}: {escape_html(', '.join(shipment['orders']))}")
        if shipment["items"]:
            block.append(
                f"{contents}: "
                + ", ".join(f"{item['qty']} × {escape_html(cstr(item['item_name']))}" for item in shipment["items"])
            )
        if shipment["address_lines"]:
            block.append(
                f"{shop if shipment['is_parcel_shop'] else address}: "
                + ", ".join(escape_html(line) for line in shipment["address_lines"])
            )
        if shipment["tracking_url"]:
            block.append(f'<a href="{escape_html(shipment["tracking_url"])}">{track}</a>')
        lines.append("<br>".join(block))

    lines.append(closing)
    return subject, "<br><br>".join(lines)


def _commit_if_available() -> None:
    if getattr(frappe, "db", None) and hasattr(frappe.db, "commit"):
        frappe.db.commit()
