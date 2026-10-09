from __future__ import annotations
import frappe
from ..domain.dtos import ShipmentRequest
from ..domain.errors import ParcelError
from ..infra.di import resolve_client
from ..infra.logging import get_logger
from .repository import create_shipment_doc, attach_label, set_tracking_and_status

logger = get_logger("parcel_station.service")

class ParcelService:
    """Application service (Facade). SRP: orchestrate shipment creation."""
    def __init__(self):  # DI boundary can be widened if you inject repositories too
        pass

    def create_and_label(self, req: ShipmentRequest) -> dict:
        docname = create_shipment_doc(req.__dict__)
        try:
            client = resolve_client(req.carrier_code)
            res = client.create_label(req)
            file_url = attach_label(docname, res.label_filename, res.label_bytes, res.label_mime)
            set_tracking_and_status(docname, res.tracking_number, "Labeled")
            frappe.db.commit()
            logger.info("Shipment labeled | doc=%s tracking=%s", docname, res.tracking_number)
            return {"name": docname, "tracking_number": res.tracking_number, "label_file_url": file_url}
        except ParcelError as e:
            set_tracking_and_status(docname, "", "Failed")
            frappe.db.commit()
            logger.error("Shipment failed | doc=%s error=%s", docname, str(e))
            frappe.throw(title="Parcel Error", msg=str(e))  # propagates as 417 HTTP in REST calls
