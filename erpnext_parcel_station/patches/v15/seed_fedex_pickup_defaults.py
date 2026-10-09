"""Seed the courier-pickup defaults in FedEx Settings.

Same situation as ``seed_fedex_label_defaults``: FedEx Settings is a Single
that was saved long before the pickup fields existed, so their DocType
defaults never reach ``tabSingles`` and the 12:00 run would read an empty
window and an empty remark. Seeds only fields that were never set, so a value
somebody cleared on purpose stays cleared. ``enable_auto_pickup`` is
deliberately NOT seeded: the automatic run is switched on by hand.
"""

import frappe

DEFAULTS = {
    "pickup_ready_time": "12:30:00",
    "pickup_close_time": "16:00:00",
    "pickup_carrier_code": "FDXE",
    "pickup_package_location": "FRONT",
    "pickup_remarks": "Haupteingang Verkaufsraum",
}


def execute():
    if not frappe.db.exists("DocType", "FedEx Settings"):
        return

    for fieldname, value in DEFAULTS.items():
        if frappe.db.exists("Singles", {"doctype": "FedEx Settings", "field": fieldname}):
            continue
        frappe.db.set_single_value("FedEx Settings", fieldname, value)
        frappe.logger().info(f"[patch] FedEx Settings.{fieldname} seeded to {value} (was never set)")
