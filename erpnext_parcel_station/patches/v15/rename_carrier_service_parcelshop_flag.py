"""Rename ``Carrier Service.isParcelShop_Pickup`` → ``is_parcelshop_pickup``.

The Check field was originally landed with a camelCase fieldname. Renaming
to snake_case for consistency with the rest of the doctype and Frappe's
field-naming convention.

Runs in ``post_model_sync``: doctype sync has already added the new
snake_case column (from the updated JSON), so by the time we get here both
columns coexist. We copy any flagged values from the camelCase column to
the snake_case one, then drop the camelCase column. Frappe's stock
``rename_field`` helper handles the value copy and any property-setter /
report references — we still issue an explicit DROP COLUMN afterwards since
``rename_field`` leaves the source column in place.

Idempotent: a re-run after success finds the old column already gone and
bails. Safe to leave in the patch list permanently.
"""
from __future__ import annotations

import frappe
from frappe.model.utils.rename_field import rename_field


def execute() -> None:
    table = "Carrier Service"
    old = "isParcelShop_Pickup"
    new = "is_parcelshop_pickup"

    if not frappe.db.has_column(table, old):
        return

    if frappe.db.has_column(table, new):
        rename_field(table, old, new)
    else:
        # Defensive: if sync hasn't added the new column yet, rename_field
        # would no-op. Fall back to a straight column rename.
        frappe.db.sql_ddl(
            f"ALTER TABLE `tab{table}` "
            f"CHANGE `{old}` `{new}` INT(1) NOT NULL DEFAULT 0"
        )
        return

    frappe.db.sql_ddl(f"ALTER TABLE `tab{table}` DROP COLUMN `{old}`")
