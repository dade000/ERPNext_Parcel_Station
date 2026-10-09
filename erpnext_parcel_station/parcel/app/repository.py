from __future__ import annotations
import frappe
from frappe.utils.file_manager import save_file

def create_shipment_doc(values: dict) -> str:
    doc = frappe.new_doc("Parcel Shipment")
    for k, v in values.items():
        if k in {"customer_name","address","weight_kg","carrier_code","service_code"}:
            doc.set(k, v)
    doc.insert(ignore_permissions=True)
    return doc.name

def attach_label(docname: str, filename: str, content: bytes, mime: str) -> str:
    file_doc = save_file(filename, content, "Parcel Shipment", docname, is_private=1, df="label_file")
    frappe.db.set_value("Parcel Shipment", docname, "label_file", file_doc.file_url)
    return file_doc.file_url

def set_tracking_and_status(docname: str, tracking: str, status: str):
    frappe.db.set_value("Parcel Shipment", docname, "tracking_number", tracking)
    frappe.db.set_value("Parcel Shipment", docname, "status", status)
