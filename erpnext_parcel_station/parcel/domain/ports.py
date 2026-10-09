from __future__ import annotations
from typing import Protocol
from .dtos import ShipmentRequest, LabelResponse

class ICarrierClient(Protocol):
    """Strategy interface for carrier integrations."""
    def create_label(self, req: ShipmentRequest) -> LabelResponse: ...
