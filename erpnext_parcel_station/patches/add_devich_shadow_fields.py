"""Mirror Devich-owned custom fields on Delivery Note for the webhook receiver."""
from __future__ import annotations

import frappe

from erpnext_parcel_station.install import _bootstrap_devich_shadow_fields


def execute():
    if not frappe.db.exists("DocType", "Delivery Note"):
        # ERPNext not installed on this site (unlikely, but bail safely)
        return
    _bootstrap_devich_shadow_fields()
    frappe.db.commit()
