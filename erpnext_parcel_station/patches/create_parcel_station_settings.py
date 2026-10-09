from __future__ import annotations
import frappe


def execute():
    """Ensure Single DocType 'Parcel Station Settings' exists with required fields.

    Fields:
    - endpoint_url (URL)
    - client_id (Data)
    - org_unit_id (Data)
    - org_unit_guid (Data)
    """

    def ensure_field(dt, fieldname, props):
        if any(f.fieldname == fieldname for f in dt.fields):
            return
        dt.append("fields", props)

    if not frappe.db.exists("DocType", "Parcel Station Settings"):
        dt = frappe.new_doc("DocType")
        dt.name = "Parcel Station Settings"
        dt.module = "Erpnext Parcel Station"
        dt.custom = 1
        dt.istable = 0
        dt.issingle = 1
        dt.allow_import = 0
        dt.append("fields", {"label": "Endpoint URL", "fieldname": "endpoint_url", "fieldtype": "Data", "options": "URL", "reqd": 1})
        dt.append("fields", {"label": "Client ID", "fieldname": "client_id", "fieldtype": "Data", "reqd": 1})
        dt.append("fields", {"label": "OrgUnit ID", "fieldname": "org_unit_id", "fieldtype": "Data", "reqd": 1})
        dt.append("fields", {"label": "OrgUnit GUID", "fieldname": "org_unit_guid", "fieldtype": "Data", "reqd": 1})
        # Additional configuration fields for labels/services
        dt.append("fields", {"label": "Delivery Service ThirdParty ID", "fieldname": "delivery_service_third_party_id", "fieldtype": "Data", "default": "10"})
        dt.append("fields", {"label": "Label Format ID", "fieldname": "label_format_id", "fieldtype": "Data", "default": "100x150"})
        dt.append("fields", {"label": "Label Language", "fieldname": "label_language", "fieldtype": "Data", "default": "ZPL2"})
        dt.append("permissions", {"role": "System Manager"})
        dt.insert(ignore_permissions=True)
        frappe.db.commit()
        return

    # Update existing DocType to ensure fields exist
    dt = frappe.get_doc("DocType", "Parcel Station Settings")
    # If DocType exists but is not a Single, recreate it to avoid CannotChangeConstantError
    if not getattr(dt, "issingle", 0):
        frappe.delete_doc("DocType", "Parcel Station Settings", force=True, ignore_permissions=True)
        frappe.clear_cache(doctype="Parcel Station Settings")
        dt = frappe.new_doc("DocType")
        dt.name = "Parcel Station Settings"
        dt.module = "Erpnext Parcel Station"
        dt.custom = 1
        dt.istable = 0
        dt.issingle = 1
        dt.allow_import = 0
        dt.append("fields", {"label": "Endpoint URL", "fieldname": "endpoint_url", "fieldtype": "Data", "options": "URL", "reqd": 1})
        dt.append("fields", {"label": "Client ID", "fieldname": "client_id", "fieldtype": "Data", "reqd": 1})
        dt.append("fields", {"label": "OrgUnit ID", "fieldname": "org_unit_id", "fieldtype": "Data", "reqd": 1})
        dt.append("fields", {"label": "OrgUnit GUID", "fieldname": "org_unit_guid", "fieldtype": "Data", "reqd": 1})
        dt.append("fields", {"label": "Delivery Service ThirdParty ID", "fieldname": "delivery_service_third_party_id", "fieldtype": "Data", "default": "10"})
        dt.append("fields", {"label": "Label Format ID", "fieldname": "label_format_id", "fieldtype": "Data", "default": "100x150"})
        dt.append("fields", {"label": "Label Language", "fieldname": "label_language", "fieldtype": "Data", "default": "ZPL2"})
        dt.append("permissions", {"role": "System Manager"})
        dt.insert(ignore_permissions=True)
        frappe.db.commit()
        return
    if not dt.custom:
        dt.custom = 1
    ensure_field(dt, "endpoint_url", {"label": "Endpoint URL", "fieldname": "endpoint_url", "fieldtype": "Data", "options": "URL", "reqd": 1})
    ensure_field(dt, "client_id", {"label": "Client ID", "fieldname": "client_id", "fieldtype": "Data", "reqd": 1})
    ensure_field(dt, "org_unit_id", {"label": "OrgUnit ID", "fieldname": "org_unit_id", "fieldtype": "Data", "reqd": 1})
    ensure_field(dt, "org_unit_guid", {"label": "OrgUnit GUID", "fieldname": "org_unit_guid", "fieldtype": "Data", "reqd": 1})
    ensure_field(dt, "delivery_service_third_party_id", {"label": "Delivery Service ThirdParty ID", "fieldname": "delivery_service_third_party_id", "fieldtype": "Data", "default": "10"})
    ensure_field(dt, "label_format_id", {"label": "Label Format ID", "fieldname": "label_format_id", "fieldtype": "Data", "default": "100x150"})
    ensure_field(dt, "label_language", {"label": "Label Language", "fieldname": "label_language", "fieldtype": "Data", "default": "ZPL2"})
    if dt.module != "Erpnext Parcel Station":
        dt.module = "Erpnext Parcel Station"
    dt.save(ignore_permissions=True)
    frappe.db.commit()
