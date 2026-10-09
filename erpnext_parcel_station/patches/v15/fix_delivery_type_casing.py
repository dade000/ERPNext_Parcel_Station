"""Standardize the ``delivery_type`` Select field's options to lowercase.

The two Custom Fields ``Sales Order-delivery_type`` and
``Shipment-delivery_type`` originally shipped with options::

    Home Delivery
    ParcelShop Delivery       ← capital S

But every downstream consumer (parcel-station GLS routing, devich's
``normalize_delivery_type`` hook, the webshop checkout UI copy) treats
the canonical value as the lowercase variant ``Parcelshop Delivery``.

The mismatch caused a subtle data-loss bug: when our SO
``before_validate`` hook stored ``ParcelShop Delivery`` (matching the
field options at the time) and devich's normalizer immediately rewrote
it to ``Parcelshop Delivery``, ERPNext's Select validator saw a value
that wasn't in the options list and silently dropped it back to the
first option (``Home Delivery``). The customer's parcel-shop pick was
lost on save.

Fix: update the field options to the lowercase form so the canonical
value is a valid option. The fixture file is already updated; this
patch propagates the change to any environment where the field is
already created in the DB and heals stored values stuck on the legacy
capital-S form.

Idempotent — re-running is a no-op once the options match.
"""
from __future__ import annotations

import frappe


_CANONICAL_OPTIONS = "Home Delivery\nParcelshop Delivery"

_TARGETS = [
    "Sales Order-delivery_type",
    "Shipment-delivery_type",
]

_LEGACY_VALUE = "ParcelShop Delivery"
_CANONICAL_VALUE = "Parcelshop Delivery"


def execute() -> None:
    # 1. Refresh Custom Field options on the two doctypes.
    for cf_name in _TARGETS:
        if not frappe.db.exists("Custom Field", cf_name):
            continue
        current = frappe.db.get_value("Custom Field", cf_name, "options")
        if current == _CANONICAL_OPTIONS:
            continue
        frappe.db.set_value("Custom Field", cf_name, "options", _CANONICAL_OPTIONS)
        frappe.logger().info(
            f"[fix_delivery_type_casing] Updated {cf_name} options → "
            f"{_CANONICAL_OPTIONS!r}"
        )

    # 2. Heal existing stored values: any document whose delivery_type is
    #    stuck on the legacy capital-S variant gets rewritten in place.
    for doctype in ("Sales Order", "Delivery Note", "Shipment"):
        if not frappe.db.has_column(doctype, "delivery_type"):
            continue
        affected = frappe.db.count(
            doctype, filters={"delivery_type": _LEGACY_VALUE}
        )
        if not affected:
            continue
        frappe.db.sql(
            f"UPDATE `tab{doctype}` SET delivery_type=%s WHERE delivery_type=%s",
            (_CANONICAL_VALUE, _LEGACY_VALUE),
        )
        frappe.logger().info(
            f"[fix_delivery_type_casing] Migrated {affected} {doctype} "
            f"row(s) from {_LEGACY_VALUE!r} → {_CANONICAL_VALUE!r}"
        )

    # 3. Clear the meta cache so the new options take effect immediately
    #    rather than after the next bench restart.
    frappe.clear_cache()
