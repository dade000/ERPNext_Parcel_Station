"""Single source of truth for carrier-driven delivery dates.

``Carrier Service.min_delivery_days`` / ``max_delivery_days`` drive the
delivery date across the whole lifecycle — Sales Order, Delivery Note and
Shipment. The committed delivery date is ``base_date + max_delivery_days``
(falling back to ``min_delivery_days`` when only a minimum is set).

There is intentionally NO hardcoded day-count fallback here: when a
Carrier Service has neither value configured, the helpers return ``None``
and callers leave the field untouched, so a mis-configured carrier is
visible rather than silently masked by a magic number.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate


def carrier_delivery_days(carrier_service: str | None) -> tuple[int | None, int | None]:
    """Return ``(min_days, max_days)`` for a Carrier Service, or (None, None)."""
    if not carrier_service:
        return (None, None)
    row = frappe.db.get_value(
        "Carrier Service",
        carrier_service,
        ["min_delivery_days", "max_delivery_days"],
        as_dict=True,
    )
    if not row:
        return (None, None)
    mn = int(row.min_delivery_days) if row.min_delivery_days else None
    mx = int(row.max_delivery_days) if row.max_delivery_days else None
    return (mn, mx)


def committed_delivery_date(
    carrier_service: str | None, base_date: str | None = None
) -> str | None:
    """Committed delivery date = ``base_date + max`` (or + min when no max).

    ``base_date`` defaults to today; pass the document's own date
    (SO.transaction_date / DN.posting_date) so back-dated documents stay
    correct. Returns ``None`` when the carrier has no days configured.
    """
    mn, mx = carrier_delivery_days(carrier_service)
    days = mx if (mx and mx > 0) else (mn if (mn and mn > 0) else None)
    if days is None:
        return None
    return add_days(base_date or nowdate(), days)
