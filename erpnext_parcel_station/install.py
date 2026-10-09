from __future__ import annotations
import frappe

def _doctype_exists(name: str) -> bool:
    return bool(frappe.db.exists("DocType", name))

def _make_single_doctype(name: str, fields: list[dict]):
    dt = frappe.new_doc("DocType")
    dt.name = name
    dt.module = "Erpnext Parcel Station"
    dt.custom = 1
    dt.istable = 0
    dt.issingle = 1
    dt.allow_import = 0
    for f in fields:
        dt.append("fields", f)                       # child rows, not raw list
    dt.append("permissions", {"role": "System Manager"})
    dt.insert(ignore_permissions=True)

def _make_table_doctype(name: str, fields: list[dict], permissions: list[str], autoname: str | None = None):
    dt = frappe.new_doc("DocType")
    dt.name = name
    dt.module = "Erpnext Parcel Station"
    dt.custom = 1
    dt.istable = 0
    dt.issingle = 0
    if autoname:
        dt.autoname = autoname
    for f in fields:
        dt.append("fields", f)                       # child rows
    for role in permissions:
        dt.append("permissions", {"role": role})
    dt.insert(ignore_permissions=True)

def _ensure_custom_field(doctype: str, fieldname: str, definition: dict) -> None:
    """Idempotently install a Custom Field (Devich-owned fields shadowed locally)."""
    full_name = f"{doctype}-{fieldname}"
    if frappe.db.exists("Custom Field", full_name):
        return
    cf = frappe.new_doc("Custom Field")
    cf.dt = doctype
    cf.fieldname = fieldname
    cf.label = definition.get("label") or fieldname
    cf.fieldtype = definition.get("fieldtype", "Data")
    if definition.get("options"):
        cf.options = definition["options"]
    if definition.get("insert_after"):
        cf.insert_after = definition["insert_after"]
    cf.read_only = definition.get("read_only", 0)
    cf.translatable = definition.get("translatable", 0)
    cf.insert(ignore_permissions=True)


def _bootstrap_devich_shadow_fields() -> None:
    """Custom fields needed by the webhook/serial flow that originated on
    Devich's app. Carrier Service on Delivery Note is now owned by Parcel
    Station's fixtures (see ``fixtures/custom_field.json``); we keep an
    idempotent install-time fallback only for ``barcode_number``, which is
    not in fixtures.

    custom_delivery_type / custom_parcelshop_address / custom_selected_gls_parcel_shop_id
    were retired in the GLS parcel-shop cleanup; parcel-shop identity now
    lives on the linked Address record (is_parcel_shop / parcel_shop_id /
    parcel_shop_carrier).
    """
    fields = [
        ("Delivery Note", "barcode_number",
         {"label": "Barcode Number", "fieldtype": "Data", "insert_after": "naming_series"}),
    ]
    for dt, fname, definition in fields:
        _ensure_custom_field(dt, fname, definition)


def after_install():
    # Core settings used by Austrian Post SOAP integration
    if not _doctype_exists("Parcel Station Settings"):
        _make_single_doctype("Parcel Station Settings", [
            {"label": "Endpoint URL", "fieldname": "endpoint_url", "fieldtype": "Data", "options": "URL", "reqd": 1},
            {"label": "Client ID", "fieldname": "client_id", "fieldtype": "Data", "reqd": 1},
            {"label": "OrgUnit ID", "fieldname": "org_unit_id", "fieldtype": "Data", "reqd": 1},
            {"label": "OrgUnit GUID", "fieldname": "org_unit_guid", "fieldtype": "Data", "reqd": 1},
            {"label": "Label Format ID", "fieldname": "label_format_id", "fieldtype": "Data", "default": "100x150"},
            {"label": "Label Language", "fieldname": "label_language", "fieldtype": "Data", "default": "ZPL2"},
        ])

    if not _doctype_exists("Parcel Shipment"):
        _make_table_doctype(
            "Parcel Shipment",
            [
                {"label": "Customer Name", "fieldname": "customer_name", "fieldtype": "Data", "reqd": 1},
                {"label": "Address", "fieldname": "address", "fieldtype": "Small Text", "reqd": 1},
                {"label": "Weight (kg)", "fieldname": "weight_kg", "fieldtype": "Float", "reqd": 1},
                {"label": "Carrier Code", "fieldname": "carrier_code", "fieldtype": "Data", "reqd": 1},
                {"label": "Service Code", "fieldname": "service_code", "fieldtype": "Data"},
                {"label": "Tracking Number", "fieldname": "tracking_number", "fieldtype": "Data", "read_only": 1},
                {"label": "Label File", "fieldname": "label_file", "fieldtype": "Attach", "read_only": 1},
                {"label": "Status", "fieldname": "status", "fieldtype": "Select",
                 "options": "\nCreated\nLabeled\nFailed", "default": "Created", "read_only": 1},
            ],
            permissions=["System Manager", "Stock User", "Sales User"],
            autoname="PAR-.YYYY.-.#####",
        )

    _bootstrap_devich_shadow_fields()
    frappe.db.commit()
