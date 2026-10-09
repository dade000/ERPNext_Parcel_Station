"""Drop the Products/Shipping split mirror fields and the print format using them.

``custom_products_subtotal`` / ``custom_shipping_cost`` existed on Sales Order
and Delivery Note only to render a ``Products | Shipping | Grand Total`` block.
They had no remaining consumer:

* The only print format that read them (``Sales Order Clean``) is unused —
  Devich prints via ``DEVICH Sales Order`` / ``DEVICH Sales Invoice``, which
  render ``templates/print_formats/devich_document.html`` and reference
  neither field.
* ``Shipment.shipment_amount`` was fed from ``DN.custom_shipping_cost``, but
  the GLS label payload carries no amount at all (see
  ``gls_api._build_payload``), so the value never left ERPNext. It is now
  derived from the DN's shipping line item at submit time
  (``events.delivery_note._shipping_charge``).

They were also never in the ``fixtures`` whitelist, so they only ever existed
on sites where someone created them by hand. On sites without them the Sales
Order / Delivery Note client scripts threw ``Field custom_products_subtotal
not found.`` out of ``frm.set_value`` during ``validate``, which aborted the
submit outright. Removing the fields removes that failure mode for good.

Idempotent: missing Custom Fields, missing columns and a missing Print Format
are all treated as already-done.
"""
from __future__ import annotations

import frappe


RETIRED_CUSTOM_FIELDS = (
    "Sales Order-custom_products_subtotal",
    "Sales Order-custom_shipping_cost",
    "Delivery Note-custom_products_subtotal",
    "Delivery Note-custom_shipping_cost",
)

RETIRED_PRINT_FORMAT = "Sales Order Clean"


def execute() -> None:
    cf_deleted = 0
    cf_absent = 0
    col_dropped = 0
    col_absent = 0

    for name in RETIRED_CUSTOM_FIELDS:
        dt, fieldname = name.split("-", 1)

        if frappe.db.exists("Custom Field", name):
            frappe.delete_doc(
                "Custom Field", name, ignore_permissions=True, force=True
            )
            cf_deleted += 1
        else:
            cf_absent += 1

        # Drop the column separately: deleting the Custom Field leaves the
        # physical column behind, and these columns hold stale mirrored
        # amounts that must not survive as a half-truth on old documents.
        if frappe.db.has_column(dt, fieldname):
            frappe.db.sql_ddl(f"ALTER TABLE `tab{dt}` DROP COLUMN `{fieldname}`")
            col_dropped += 1
        else:
            col_absent += 1

    pf_deleted = 0
    if frappe.db.exists("Print Format", RETIRED_PRINT_FORMAT):
        # force=True: the record is `standard = Yes` and its module folder is
        # already gone from the app, so the normal "standard doc" guard would
        # otherwise refuse the delete on a live site.
        frappe.delete_doc(
            "Print Format",
            RETIRED_PRINT_FORMAT,
            ignore_permissions=True,
            force=True,
        )
        pf_deleted = 1
        # Any Property Setter pinning it as a doctype default would leave the
        # Sales Order print view pointing at a record that no longer exists.
        frappe.db.delete(
            "Property Setter",
            {"property": "default_print_format", "value": RETIRED_PRINT_FORMAT},
        )

    frappe.db.commit()
    summary = (
        f"drop_shipping_split_fields summary: "
        f"custom_fields_deleted={cf_deleted}, custom_fields_already_absent={cf_absent}, "
        f"columns_dropped={col_dropped}, columns_already_absent={col_absent}, "
        f"print_format_deleted={pf_deleted}"
    )
    frappe.logger().info(summary)
    print(summary)
