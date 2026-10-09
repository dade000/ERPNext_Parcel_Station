# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Read API for the webshop: tracking status + events per Sales Order.

The shop (static Next.js) calls this server-side with its ERP API key — the
same pattern as its other ERP calls — so the endpoints are whitelisted without
``allow_guest``. Contract documented in ``docs/tracking_api.md``; the shop
builds its own customer-facing overview from canonical status + events.
**No free-text event descriptions are exposed**: carrier remark fields carry
recipient/neighbour names (PII).

Two endpoints, two audiences:

* ``get_order_tracking`` — by Sales Order name, for shop code that already
  knows which order it is dealing with. No carrier links.
* ``get_tracking_page`` — by the secret token from the shipping notification
  mail (``shipping_notification.ensure_tracking_token``), for the customer's
  tracking page. The token is the only credential, so an order number alone
  never opens the page. This one carries the carrier's public tracking link:
  redirecting a parcel or choosing a delivery day is only possible there.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from ..carrier_routing import resolve_carrier
from .events import tracking_url_for
from .pickup_reminder import CARRIER_LABELS
from .shipping_notification import TOKEN_FIELD

MIN_TOKEN_LENGTH = 24


@frappe.whitelist(methods=["GET"])
def get_order_tracking(sales_order: str) -> dict[str, Any]:
    """Tracking state of every shipment belonging to one Sales Order."""
    if not frappe.db.exists("Sales Order", sales_order):
        frappe.throw(f"Sales Order {sales_order} not found", frappe.DoesNotExistError)

    return {
        "sales_order": sales_order,
        "shipments": [_shipment_payload(row) for row in _order_shipments(sales_order)],
    }


@frappe.whitelist(methods=["GET"])
def get_tracking_page(token: str) -> dict[str, Any]:
    """Everything the customer's tracking page shows, looked up by the token
    from the shipping notification mail."""
    token = cstr(token).strip()
    # Tokens are 32 url-safe characters; anything short is a guess or a typo
    # and must not turn into a query for "token is empty".
    order = None
    if len(token) >= MIN_TOKEN_LENGTH and frappe.get_meta("Sales Order").has_field(TOKEN_FIELD):
        order = frappe.db.get_value(
            "Sales Order", {TOKEN_FIELD: token}, ["name", "transaction_date"], as_dict=True
        )
    if not order:
        frappe.throw("Tracking page not found", frappe.DoesNotExistError)

    return {
        "sales_order": order.name,
        "order_date": str(order.transaction_date) if order.transaction_date else None,
        "shipments": [
            _shipment_payload(row, for_customer_page=True) for row in _order_shipments(order.name)
        ],
    }


def _order_shipments(sales_order: str) -> list[Any]:
    """Submitted Shipments behind a Sales Order, oldest first."""
    delivery_notes = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_order": sales_order, "docstatus": 1},
        pluck="parent",
        distinct=True,
    )
    if not delivery_notes:
        return []

    shipment_names = frappe.get_all(
        "Shipment Delivery Note",
        filters={"delivery_note": ("in", delivery_notes)},
        pluck="parent",
        distinct=True,
    )
    rows = []
    for name in shipment_names:
        row = frappe.db.get_value(
            "Shipment",
            name,
            [
                "name",
                "docstatus",
                "creation",
                "awb_number",
                "tracking_status",
                "carrier",
                "carrier_service",
                "custom_carrier_service",
                "delivery_type",
            ],
            as_dict=True,
        )
        if not row or row.docstatus != 1:
            continue
        rows.append(row)
    return sorted(rows, key=lambda row: str(row.creation))


def _shipment_payload(row, for_customer_page: bool = False) -> dict[str, Any]:
    event_rows = frappe.get_all(
        "Parcel Tracking Event",
        filters={"shipment": row.name},
        fields=["event_timestamp", "canonical_status", "event_code", "location_city", "location_country"],
        order_by="event_timestamp desc, creation desc",
    )
    carrier = resolve_carrier(row)
    payload = {
        "shipment": row.name,
        "carrier": carrier,
        "tracking_number": row.awb_number,
        "tracking_status": row.tracking_status,
        "last_event_at": str(event_rows[0].event_timestamp) if event_rows else None,
        "events": [
            {
                "timestamp": str(event.event_timestamp),
                "status": event.canonical_status,
                "code": event.event_code,
                "city": event.location_city or None,
                "country": event.location_country or None,
            }
            for event in event_rows
        ],
    }
    if for_customer_page:
        payload["carrier_name"] = CARRIER_LABELS.get(carrier, carrier)
        payload["carrier_tracking_url"] = tracking_url_for(carrier, row.awb_number)
        payload["shipped_at"] = str(row.creation) if row.creation else None
        payload["delivery_type"] = row.delivery_type or None
    return payload
