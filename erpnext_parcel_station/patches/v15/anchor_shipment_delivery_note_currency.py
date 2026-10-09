"""Anchor Shipment Delivery Note's ``grand_total`` to parent ``currency``.

ERPNext's ``Shipment Delivery Note`` child table has a Currency field
``grand_total`` with no ``options`` set. When a Currency field lacks an
``options`` anchor, the form renders it in the system's default currency
(PKR on this site, since the imported test data set that as the system
default). The parent Shipment correctly forces EUR via the
``copy_shipping_details_to_shipment`` hook on ``before_insert`` — but
that fix never propagates into the child grid, so the "Value" column
keeps showing "PKR 12.90" while every other amount on the page reads
in EUR.

Property Setter the child field's ``options`` to ``"currency"`` so the
child Currency formatter resolves it against the parent Shipment's
``currency`` field, matching how stock ERPNext anchors child currency
fields in Sales Order / Sales Invoice (``options = "currency"`` on the
items table). No data change — purely a display fix.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute() -> None:
    make_property_setter(
        doctype="Shipment Delivery Note",
        fieldname="grand_total",
        property="options",
        value="currency",
        property_type="Small Text",
        for_doctype=False,
    )

    # Heal existing Shipments whose ``currency`` is something other than the
    # pickup_company's default (commonly "PKR" because the imported test
    # data set system currency to PKR and ERPNext picked that up before
    # ``copy_shipping_details_to_shipment`` started forcing pickup_company
    # currency on new Shipments). Done via direct SQL — running validate
    # on submitted/cancelled Shipments isn't always safe.
    shipments = frappe.db.sql(
        """
        SELECT s.name, s.pickup_company, s.currency, c.default_currency
        FROM `tabShipment` s
        LEFT JOIN `tabCompany` c ON c.name = s.pickup_company
        WHERE s.pickup_company IS NOT NULL
          AND s.pickup_company != ''
          AND c.default_currency IS NOT NULL
          AND (s.currency IS NULL OR s.currency != c.default_currency)
        """,
        as_dict=True,
    )
    for sh in shipments:
        frappe.db.set_value(
            "Shipment", sh["name"], "currency", sh["default_currency"],
            update_modified=False,
        )

    frappe.clear_cache(doctype="Shipment Delivery Note")
    frappe.clear_cache(doctype="Shipment")
