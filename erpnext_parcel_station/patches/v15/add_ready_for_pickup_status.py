"""Re-apply the Shipment.tracking_status option list after adding
"Ready for Pickup" (deposited at post office / GLS ParcelShop / FedEx Hold
at Location — waiting for the customer, distinct from Delivered).

The option list itself lives in
``extend_shipment_tracking_status_options.TRACKING_STATUS_OPTIONS`` (one
source of truth, guarded by ``test_status_mapping.TestVocabularyAlignment``);
this patch exists because the original one is already in the Patch Log and
would not re-run. ``make_property_setter`` updates the existing row, so this
is idempotent.
"""
from __future__ import annotations

from erpnext_parcel_station.patches.v15.extend_shipment_tracking_status_options import (
    execute as reapply_options,
)


def execute() -> None:
    reapply_options()
