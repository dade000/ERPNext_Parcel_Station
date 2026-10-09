from __future__ import annotations
import frappe


def execute():
    """Add missing config fields to Parcel Station Settings (if absent).

    Fields ensured:
    - delivery_service_third_party_id (Data)
    - label_format_id (Data)
    - label_language (Data)
    """

    if not frappe.db.exists("DocType", "Parcel Station Settings"):
        return

    dt = frappe.get_doc("DocType", "Parcel Station Settings")

    def ensure(fieldname: str, props: dict):
        if any(f.fieldname == fieldname for f in dt.fields):
            return
        dt.append("fields", props)

    ensure("delivery_service_third_party_id", {
        "label": "Delivery Service ThirdParty ID",
        "fieldname": "delivery_service_third_party_id",
        "fieldtype": "Data",
        "default": "10",
    })
    ensure("label_format_id", {
        "label": "Label Format ID",
        "fieldname": "label_format_id",
        "fieldtype": "Data",
        "default": "100x150",
    })
    ensure("label_language", {
        "label": "Label Language",
        "fieldname": "label_language",
        "fieldtype": "Data",
        "default": "ZPL2",
    })

    dt.save(ignore_permissions=True)
    frappe.db.commit()

