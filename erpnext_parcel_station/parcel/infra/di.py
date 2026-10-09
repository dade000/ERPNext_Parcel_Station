from __future__ import annotations
import frappe
from dataclasses import dataclass
from .clients.austrian_post import AustrianPostClient
from ..domain.ports import ICarrierClient
from .clients.gls import GlsClient


# Austrian Post auth is NOT a per-carrier api_key/api_secret pair; the actual
# SOAP ImportShipment request is identified by client_id + org_unit_id +
# org_unit_guid (see parcel/api/carrier.py). Those live under the
# "Organisation Unit & Service" section of Austrian Post Settings and are
# read directly there, not threaded through CarrierConfig.
@dataclass(frozen=True)
class CarrierConfig:
    base_url: str


def resolve_client(carrier_code: str) -> ICarrierClient:
    settings = frappe.get_single("Parcel Station Settings")
    code = (carrier_code or settings.default_carrier or "").strip().lower()

    if code == "austrian_post":
        base_url = frappe.db.get_single_value("Austrian Post Settings", "austrian_post_base_url")
        cfg = CarrierConfig(base_url=base_url)
        return AustrianPostClient(cfg)

    if code == "gls":
        return GlsClient()

    raise ValueError(f"Unsupported carrier '{carrier_code}'. Configure default_carrier or pass a known code.")