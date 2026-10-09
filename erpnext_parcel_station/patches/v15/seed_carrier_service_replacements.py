"""Seed the In-Store Pickup Carrier Service.

The webshop's in-store-pickup flow needs a Carrier Service with a
zero-rate ``shipping_item`` to drive the Sales Order's shipping line.
This patch creates that record so a fresh install works without manual
Desk setup.

(Originally this patch also seeded a ``GLS Parcel-Shop`` Carrier Service
for the parcel-shop flow, but Daniel decided that flow is not used at
holzschuhe.at; the entry was removed and a follow-up patch
``remove_gls_parcelshop_carrier_service`` deletes existing rows from any
environment where the seed already ran.)

Naming + service codes:

    Name             service_code              Item code              Rate
    ---------------  ------------------------  ---------------------  -----
    In-Store Pickup  service_instore_pickup    SHIP-INSTORE-PICKUP    0

Idempotent: each row is only created if it doesn't already exist by name.
Re-running the patch is a no-op once the seed data is in place.
"""
from __future__ import annotations

import frappe


SEED_ITEMS = [
    {
        "item_code": "SHIP-INSTORE-PICKUP",
        "item_name": "In-Store Pickup",
        "description": "Customer picks up the order in-store. No shipping "
                       "charge.",
        "standard_rate": 0,
    },
]


SEED_CARRIER_SERVICES = [
    {
        "name": "In-Store Pickup",
        "service_name": "In-Store Pickup",
        "service_code": "service_instore_pickup",
        "shipping_item": "SHIP-INSTORE-PICKUP",
        "carrier": None,
    },
]


def execute() -> None:
    _ensure_item_group()
    _ensure_pickup_carrier()
    for spec in SEED_ITEMS:
        _ensure_item(spec)
    for spec in SEED_CARRIER_SERVICES:
        _ensure_carrier_service(spec)


def _ensure_item_group() -> None:
    """Make sure the Services item group exists for the shipping Items.

    Most fresh ERPNext installs already have a "Services" group; if not, we
    fall back to creating one so the Item create() call below doesn't fail
    on a missing parent group.
    """
    if frappe.db.exists("Item Group", "Services"):
        return
    frappe.get_doc({
        "doctype": "Item Group",
        "item_group_name": "Services",
        "parent_item_group": "All Item Groups",
        "is_group": 0,
    }).insert(ignore_permissions=True)


def _ensure_pickup_carrier() -> None:
    """In-Store Pickup isn't tied to a real carrier. Create a "Pickup"
    Carrier row so the Carrier Service's mandatory carrier link can point
    somewhere. Skipped if Carrier doctype isn't installed yet."""
    if not frappe.db.exists("DocType", "Carrier"):
        return
    SEED_CARRIER_SERVICES[1]["carrier"] = "Pickup"
    if frappe.db.exists("Carrier", "Pickup"):
        return
    frappe.get_doc({
        "doctype": "Carrier",
        "carrier_name": "Pickup",
    }).insert(ignore_permissions=True)


def _ensure_item(spec: dict) -> None:
    if frappe.db.exists("Item", spec["item_code"]):
        return
    frappe.get_doc({
        "doctype": "Item",
        "item_code": spec["item_code"],
        "item_name": spec["item_name"],
        "item_group": "Services",
        "stock_uom": "Nos",
        "is_stock_item": 0,
        "include_item_in_manufacturing": 0,
        "description": spec["description"],
        "standard_rate": spec["standard_rate"],
    }).insert(ignore_permissions=True)


def _ensure_carrier_service(spec: dict) -> None:
    if frappe.db.exists("Carrier Service", spec["name"]):
        return
    doc_dict = {
        "doctype": "Carrier Service",
        "service_name": spec["service_name"],
        "service_code": spec["service_code"],
        "shipping_item": spec["shipping_item"],
    }
    if spec.get("carrier"):
        doc_dict["carrier"] = spec["carrier"]
    frappe.get_doc(doc_dict).insert(ignore_permissions=True)
