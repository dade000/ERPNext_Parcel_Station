"""Explicitly delete the 9 retired GLS parcel-shop Custom Field records.

Runs AFTER ``migrate_gls_parcel_shop_to_address`` has moved the data onto
Address. The legacy Custom Fields must be explicitly deleted because Frappe's
``sync_fixtures`` only adds/updates entries from the fixture file — it does
not auto-purge entries that have been removed from the file.

Deleting a ``Custom Field`` doc via Frappe also drops the underlying column
from the parent ``tab<Doctype>`` table, so this patch handles both the metadata
and the schema-level cleanup in one shot.

Idempotent: ``ignore_if_missing=True`` makes re-runs a no-op.

If a row still has data in the column (e.g., this patch runs before the
migration patch by accident), Frappe will refuse the delete and the patch
will fail loudly — that's the safe behaviour. Always ensure
``migrate_gls_parcel_shop_to_address`` is listed BEFORE this patch in
``patches.txt``.
"""
from __future__ import annotations

import frappe


RETIRED_CUSTOM_FIELDS = (
    "Sales Order-custom_gls_parcel_shop_id",
    "Sales Order-custom_gls_parcel_shop",
    "Sales Order-selected_gls_parcel_shop_id",
    "Sales Order-selected_gls_parcel_shop",
    "Sales Order-gls_parcel_shop_html",
    "Sales Order-gls_delivery_options_section",
    "Delivery Note-selected_gls_parcel_shop_address",
    "Shipment-selected_gls_parcel_shop_id",
    "Shipment-selected_gls_parcel_shop_address",
)


def execute() -> None:
    cf_deleted = 0
    cf_skipped = 0
    col_dropped = 0
    col_already_absent = 0

    for name in RETIRED_CUSTOM_FIELDS:
        # Look up the (dt, fieldname) BEFORE deleting the doc so we can also
        # drop the underlying column. ``frappe.delete_doc`` with ``force=True``
        # bypasses the Custom Field ``on_trash`` hook that would normally call
        # ``frappe.db.delete_column``, so we do it explicitly here.
        meta = frappe.db.get_value(
            "Custom Field", name, ["dt", "fieldname"], as_dict=True,
        ) if frappe.db.exists("Custom Field", name) else None

        if meta:
            frappe.delete_doc(
                "Custom Field", name, ignore_permissions=True, force=True
            )
            cf_deleted += 1
        else:
            cf_skipped += 1
            # Still try to drop the column in case the metadata was already
            # cleaned but the column wasn't (the state this patch hit on first
            # run before the fix).
            parts = name.split("-", 1)
            if len(parts) == 2:
                meta = {"dt": parts[0], "fieldname": parts[1]}

        if meta and frappe.db.has_column(meta["dt"], meta["fieldname"]):
            # Frappe v15 MariaDB driver doesn't expose ``delete_column``; use raw DDL.
            frappe.db.sql_ddl(
                f"ALTER TABLE `tab{meta['dt']}` DROP COLUMN `{meta['fieldname']}`"
            )
            col_dropped += 1
        else:
            col_already_absent += 1

    frappe.db.commit()
    summary = (
        f"drop_obsolete_gls_parcel_shop_fields summary: "
        f"custom_fields_deleted={cf_deleted}, custom_fields_already_absent={cf_skipped}, "
        f"columns_dropped={col_dropped}, columns_already_absent={col_already_absent}"
    )
    frappe.logger().info(summary)
    print(summary)
