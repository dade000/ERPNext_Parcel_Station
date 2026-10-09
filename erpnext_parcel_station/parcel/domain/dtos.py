from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ShipmentRequest:
    """Request to create a shipment with a carrier and obtain a label.

    Carries both the simple recipient fields used by the lightweight REST flow
    and the Frappe Address-doctype names required by the Austrian Post SOAP
    flow. Either set is sufficient depending on the carrier client.
    """
    customer_name: str = ""
    address: str = ""
    weight_kg: float = 0.0
    carrier_code: str = ""
    service_code: Optional[str] = None

    # SOAP-flow fields (Austrian Post). Names of Frappe Address records.
    delivery_address_name: Optional[str] = None
    pickup_address_name: Optional[str] = None
    pickup_name: Optional[str] = None
    shipment_name: Optional[str] = None


@dataclass(frozen=True)
class LabelResponse:
    """Carrier response carrying both the created shipment identity and the label."""
    tracking_number: str
    label_bytes: bytes
    label_filename: str
    label_mime: str
    shipment_name: Optional[str] = None
    raw_response: Optional[str] = None


@dataclass(frozen=True)
class ShipmentResponse:
    """Standalone shipment-creation result (used where label is fetched separately)."""
    tracking_number: str
    carrier: str = ""
    label_data: Optional[bytes] = None
