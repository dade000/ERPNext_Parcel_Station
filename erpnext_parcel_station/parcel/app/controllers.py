from __future__ import annotations
import frappe
from ..domain.dtos import ShipmentRequest
from .services import ParcelService

def on_submit_shipment(doc, method=None):
    # Optional: if you want auto-label on submit via UI
    req = ShipmentRequest(
        customer_name=doc.customer_name,
        address=doc.address,
        weight_kg=doc.weight_kg,
        carrier_code=doc.carrier_code,
        service_code=doc.service_code or None,
    )
    ParcelService().create_and_label(req)
