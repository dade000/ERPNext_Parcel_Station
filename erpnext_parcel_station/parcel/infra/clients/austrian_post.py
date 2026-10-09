from __future__ import annotations
from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ...domain.dtos import ShipmentRequest, LabelResponse
from ...domain.errors import (
    CarrierAuthError,
    CarrierValidationError,
    CarrierNetworkError,
    CarrierUnavailable,
)
from ..logging import get_logger
from ...api.carrier import send_import_shipment

logger = get_logger("parcel_station.austrian_post")


@dataclass(frozen=True)
class CarrierConfig:
    """Austrian Post per-carrier config.

    Only ``base_url`` is configured at the Carrier level. Request-time auth
    (Client ID + Org Unit ID + Org Unit GUID) is read straight from
    Austrian Post Settings by ``parcel/api/carrier.py``.
    """

    base_url: str


class AustrianPostClient:
    """Austrian Post adapter.

    Uses the SOAP ``ImportShipment`` operation, which creates the shipment with
    the carrier *and* returns the label data in a single call. Returns both the
    tracking number (carrier-side shipment identity) and the label bytes.
    """

    def __init__(self, cfg: CarrierConfig):
        if not cfg.base_url:
            raise CarrierAuthError("Austrian Post base_url not configured.")
        self.cfg = cfg

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(CarrierNetworkError),
    )
    def create_label(self, req: ShipmentRequest) -> LabelResponse:
        sh = {
            "delivery_address_name": req.delivery_address_name,
            "delivery_to": req.customer_name,
            "pickup_address_name": req.pickup_address_name,
            "pickup": req.pickup_name,
            "total_weight": req.weight_kg,
        }

        try:
            result = send_import_shipment(sh)
        except Exception as e:
            logger.exception("Network error talking to Austrian Post")
            raise CarrierNetworkError(str(e)) from e

        parsed = result.get("parsed", {}) or {}
        raw = result.get("raw")

        err_code = parsed.get("error_code")
        err_msg = parsed.get("error_message")
        if err_code or err_msg:
            if err_code in {"401", "403"}:
                raise CarrierAuthError(f"Austrian Post auth failed: {err_msg or err_code}")
            if err_code and err_code.startswith("5"):
                raise CarrierUnavailable(f"Carrier unavailable: {err_code} {err_msg or ''}".strip())
            raise CarrierValidationError(f"Carrier rejected shipment: {err_code} {err_msg or ''}".strip())

        codes = parsed.get("codes") or []
        if not codes:
            raise CarrierValidationError("Austrian Post returned no tracking code.")
        tracking = codes[0]

        zpl = parsed.get("zpl")
        if not zpl:
            raise CarrierValidationError("Austrian Post returned no label data.")

        label_bytes = zpl.encode("utf-8") if isinstance(zpl, str) else bytes(zpl)
        # Name the label after the Shipment (e.g. "SHIPMENT-00126-Austria-Label.zpl")
        # rather than the raw tracking number, mirroring the GLS "-GLS-Label" convention.
        filename = f"{req.shipment_name}-Austria-Label.zpl"
        mime = "application/vnd.aim.zpl"

        logger.info(
            "Created Austrian Post label | tracking=%s weight=%.3f", tracking, req.weight_kg
        )
        return LabelResponse(
            tracking_number=tracking,
            label_bytes=label_bytes,
            label_filename=filename,
            label_mime=mime,
            shipment_name=req.shipment_name,
            raw_response=raw,
        )
