"""Remove the short-lived ``Import Charge Rule`` DocType.

Import duty and tax rates were briefly modelled as their own country-keyed
DocType. That was wrong for this system: a Carrier Service already covers
exactly one destination country — "Paket Plus Schweiz", "Paket Plus
Deutschland", "GLS Österreich", each with its own shipping item — so a separate
country master introduced a second country dimension alongside the one the
Carrier Service already carried, and the parcel station had to consult two
masters to answer one question ("is this line a charge or cargo?").

The rates now live on Carrier Service. This patch removes the orphaned DocType;
deleting the DocType drops its table and records with it. It never shipped
beyond a development site, so there is nothing to carry over.
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "Import Charge Rule"):
        return

    frappe.delete_doc("DocType", "Import Charge Rule", force=True, ignore_missing=True)
    frappe.logger().info("[patch] removed DocType Import Charge Rule (rates moved to Carrier Service)")
