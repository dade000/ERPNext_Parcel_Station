"""Drop the last batch of retired GLS parcel-shop Custom Fields on Delivery Note.

Sibling patch to ``drop_obsolete_gls_parcel_shop_fields`` — that patch handled
the SO/Shipment-side legacy fields plus DN's ``selected_gls_parcel_shop_address``.
The fields removed here lived on Devich's local ``custom/delivery_note.json``
(``module: null``) and were missed by the earlier sweep. With the Address-based
parcel-shop model in place (``is_parcel_shop`` / ``parcel_shop_id`` /
``parcel_shop_carrier``), they no longer carry meaningful data and Devich code
no longer reads or writes them.

The two layout-helper fields (``custom_section_break_n7jnm``,
``custom_column_break_yfgmg``) existed only to position the retired fields
above; once the data fields are gone they're orphans.

Idempotent: re-runs are no-ops via ``ignore_if_missing`` semantics.

Safety: if a DN still has data in one of these columns (e.g. the migration
patch was skipped), Frappe will refuse the Custom Field delete and the
DDL DROP COLUMN will fail loudly. Always ensure
``migrate_gls_parcel_shop_to_address`` ran first.
"""
from __future__ import annotations

import frappe


RETIRED_CUSTOM_FIELDS = (
    "Delivery Note-custom_delivery_type",
    "Delivery Note-custom_parcelshop_address",
    "Delivery Note-custom_selected_gls_parcel_shop_id",
    "Delivery Note-custom_section_break_n7jnm",
    "Delivery Note-custom_column_break_yfgmg",
)


def execute() -> None:
    cf_deleted = 0
    cf_skipped = 0
    col_dropped = 0
    col_already_absent = 0

    for name in RETIRED_CUSTOM_FIELDS:
        meta = frappe.db.get_value(
            "Custom Field", name, ["dt", "fieldname", "fieldtype"], as_dict=True,
        ) if frappe.db.exists("Custom Field", name) else None

        if meta:
            frappe.delete_doc(
                "Custom Field", name, ignore_permissions=True, force=True
            )
            cf_deleted += 1
        else:
            cf_skipped += 1
            parts = name.split("-", 1)
            if len(parts) == 2:
                meta = {"dt": parts[0], "fieldname": parts[1], "fieldtype": None}

        # Layout-only fieldtypes (Section/Column Break) never get a column,
        # so skip the DDL step for them.
        is_layout = meta and (meta.get("fieldtype") in {"Section Break", "Column Break"})
        if meta and not is_layout and frappe.db.has_column(meta["dt"], meta["fieldname"]):
            frappe.db.sql_ddl(
                f"ALTER TABLE `tab{meta['dt']}` DROP COLUMN `{meta['fieldname']}`"
            )
            col_dropped += 1
        else:
            col_already_absent += 1

    frappe.db.commit()
    summary = (
        f"drop_devich_dn_legacy_fields summary: "
        f"custom_fields_deleted={cf_deleted}, custom_fields_already_absent={cf_skipped}, "
        f"columns_dropped={col_dropped}, columns_already_absent={col_already_absent}"
    )
    frappe.logger().info(summary)
    print(summary)
