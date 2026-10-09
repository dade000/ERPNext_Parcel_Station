"""Seed FedEx Settings defaults that the DocType default cannot reach.

A DocType ``default`` is applied when a record is first saved. For a Single
that has ALREADY been saved — which is the case on any site where FedEx was
configured before these fields existed — a newly added field simply has no row
in ``tabSingles`` and reads back as empty. For ``request_auxiliary_label`` that
means 0: no auxiliary Air Waybill label on international parcels, silently, on
exactly the sites that were already using FedEx.

One-time by design (a real patch, not a fixture): it distinguishes "never set"
from "deliberately switched off" by looking for the absence of the row, so
re-running it can never re-enable a setting somebody turned off. Fresh sites
never need it — they get the value from the DocType default on first save.
"""

import frappe

DEFAULTS = {
    "request_auxiliary_label": "1",
    "label_resolution": "203",
}


def execute():
    if not frappe.db.exists("DocType", "FedEx Settings"):
        return

    for fieldname, value in DEFAULTS.items():
        already_set = frappe.db.exists(
            "Singles", {"doctype": "FedEx Settings", "field": fieldname}
        )
        if already_set:
            continue
        frappe.db.set_single_value("FedEx Settings", fieldname, value)
        frappe.logger().info(
            f"[patch] FedEx Settings.{fieldname} seeded to {value} (was never set)"
        )
