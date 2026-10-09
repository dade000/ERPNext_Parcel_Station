from __future__ import annotations
import frappe, json
from ..domain.dtos import ShipmentRequest
from ..app.services import ParcelService

@frappe.whitelist(allow_guest=False, methods=["POST"])
def create_shipment():
    data = frappe.local.form_dict.data or frappe.request.data
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    payload = json.loads(data) if isinstance(data, str) else data

    req = ShipmentRequest(
        customer_name=str(payload["customer_name"]).strip(),
        address=str(payload["address"]).strip(),
        weight_kg=float(payload["weight_kg"]),
        carrier_code=str(payload.get("carrier_code") or "").strip(),
        service_code=(payload.get("service_code") or None),
    )
    out = ParcelService().create_and_label(req)
    return out
