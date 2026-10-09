"""Migrate scattered GLS parcel-shop fields onto Address.

Step 8 of the v3 cleanup. Walks every Sales Order, Delivery Note, and Shipment
that has a value in one of the legacy parcel-shop custom fields, then:

  1. Looks up (or creates) an ``Address`` record carrying
     ``is_parcel_shop=1``, ``parcel_shop_id=<shop id>``,
     ``parcel_shop_carrier="GLS"``.
  2. Re-links the SO / DN / Shipment to that Address via the standard
     ``shipping_address_name`` (SO + DN) or ``delivery_address_name`` (Shipment).
  3. Reuses Addresses across multiple records for the same shop — never
     creates duplicates.

After this patch runs, the old SO/DN/Shipment custom fields can be safely
deleted (Task 6 of Step 8 retires them from the fixture).

Idempotent: re-running finds existing parcel-shop Addresses by
``(parcel_shop_carrier, parcel_shop_id)`` and skips creation.

Safe-by-default: the patch only updates ``shipping_address_name`` /
``delivery_address_name`` when an old parcel-shop field is populated.
Records without any GLS parcel-shop data are left untouched.
"""
from __future__ import annotations

import frappe
from frappe.utils import cstr


CARRIER = "GLS"

# Old fields, by parent DocType — the patch reads these directly via SQL so
# Frappe's per-doc hooks don't re-fire during migration.
SO_ID_FIELDS = ["custom_gls_parcel_shop_id", "selected_gls_parcel_shop_id"]
SO_TEXT_FIELD = "selected_gls_parcel_shop"

DN_ID_FIELDS = ["custom_selected_gls_parcel_shop_id"]
DN_TEXT_FIELD = "selected_gls_parcel_shop_address"

SHIP_ID_FIELDS = ["selected_gls_parcel_shop_id"]
SHIP_TEXT_FIELD = "selected_gls_parcel_shop_address"


def execute() -> None:
    if not frappe.db.exists("DocType", "Address"):
        return

    if not frappe.db.exists("Carrier", CARRIER):
        frappe.logger().warning(
            f"migrate_gls_parcel_shop_to_address: Carrier {CARRIER!r} not found; "
            "skipping migration (operator should create it then re-run via "
            "`bench --site <site> execute "
            "erpnext_parcel_station.patches.v15.migrate_gls_parcel_shop_to_address.execute`)"
        )
        return

    counts = {
        "sales_orders": 0,
        "delivery_notes": 0,
        "shipments": 0,
        "addresses_created": 0,
        "addresses_reused": 0,
    }
    address_cache: dict[tuple[str, str], str] = {}

    def ensure_address(parcel_shop_id: str, address_text: str | None) -> str | None:
        """Return an Address.name for this parcel shop; create one if missing."""
        psid = cstr(parcel_shop_id).strip()
        if not psid:
            return None

        key = (CARRIER, psid)
        if key in address_cache:
            counts["addresses_reused"] += 1
            return address_cache[key]

        existing = frappe.db.get_value(
            "Address",
            {"parcel_shop_carrier": CARRIER, "parcel_shop_id": psid},
            "name",
        )
        if existing:
            address_cache[key] = existing
            counts["addresses_reused"] += 1
            return existing

        text = cstr(address_text).strip()
        addr = frappe.get_doc({
            "doctype": "Address",
            "address_title": psid,
            "address_type": "Shipping",
            "address_line1": text[:240] if text else f"Parcel Shop {psid}",
            "country": "Austria",
            "is_parcel_shop": 1,
            "parcel_shop_id": psid,
            "parcel_shop_carrier": CARRIER,
        })
        addr.insert(ignore_permissions=True, ignore_mandatory=True)
        address_cache[key] = addr.name
        counts["addresses_created"] += 1
        return addr.name

    def migrate_parent(doctype: str, id_fields: list[str], text_field: str,
                       link_target_field: str, counter_key: str) -> None:
        """Walk every record of `doctype` with any old parcel-shop field set,
        attach a parcel-shop Address, and link via `link_target_field`."""
        existing_id_cols = [f for f in id_fields if frappe.db.has_column(doctype, f)]
        if not existing_id_cols:
            return

        select_cols = ["name"] + existing_id_cols
        if frappe.db.has_column(doctype, text_field):
            select_cols.append(text_field)

        # OR-together the non-empty conditions on the existing id columns
        conds = " OR ".join(f"(`{c}` IS NOT NULL AND `{c}` != '')" for c in existing_id_cols)
        rows = frappe.db.sql(
            f"SELECT {', '.join('`'+c+'`' for c in select_cols)} "
            f"FROM `tab{doctype}` WHERE {conds}",
            as_dict=True,
        )

        for r in rows:
            psid = None
            for c in existing_id_cols:
                if r.get(c):
                    psid = r[c]
                    break
            if not psid:
                continue
            text = r.get(text_field) if text_field in r else None
            addr_name = ensure_address(psid, text)
            if not addr_name:
                continue
            frappe.db.set_value(
                doctype, r["name"], link_target_field, addr_name,
                update_modified=False,
            )
            counts[counter_key] += 1

    # ---- Walk SO, DN, Shipment ----
    migrate_parent("Sales Order",   SO_ID_FIELDS,   SO_TEXT_FIELD,
                   "shipping_address_name", "sales_orders")
    migrate_parent("Delivery Note", DN_ID_FIELDS,   DN_TEXT_FIELD,
                   "shipping_address_name", "delivery_notes")
    migrate_parent("Shipment",      SHIP_ID_FIELDS, SHIP_TEXT_FIELD,
                   "delivery_address_name", "shipments")

    frappe.db.commit()

    summary = (
        f"migrate_gls_parcel_shop_to_address summary: "
        f"SO={counts['sales_orders']}, DN={counts['delivery_notes']}, "
        f"Shipment={counts['shipments']}, "
        f"addresses_created={counts['addresses_created']}, "
        f"addresses_reused={counts['addresses_reused']}"
    )
    frappe.logger().info(summary)
    print(summary)
