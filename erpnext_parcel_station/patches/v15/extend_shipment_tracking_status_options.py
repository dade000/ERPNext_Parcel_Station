"""Extend Shipment.tracking_status with the tracking-ingestion vocabulary.

Core ERPNext ships the Select with ``\\nIn Progress\\nDelivered\\nReturned\\nLost``
— four states, of which only the terminal three carry information. The carrier
tracking import (parcel/tracking/) distinguishes announced / in transit / out
for delivery / problem as well, so the option list is widened. All four core
options are preserved (existing documents keep valid values); four are added.

The canonical vocabulary lives in
``parcel/tracking/status_mapping.py`` — keep both in sync (guarded by
``test_status_mapping.TestVocabularyAlignment``).

Idempotent: ``make_property_setter`` updates the existing row if one was
created by a previous run.
"""
from __future__ import annotations

from frappe.custom.doctype.property_setter.property_setter import make_property_setter

TRACKING_STATUS_OPTIONS = (
    "\nIn Progress"
    "\nAnnounced"
    "\nIn Transit"
    "\nOut for Delivery"
    "\nReady for Pickup"
    "\nDelivered"
    "\nProblem"
    "\nReturned"
    "\nLost"
)


def execute() -> None:
    make_property_setter(
        doctype="Shipment",
        fieldname="tracking_status",
        property="options",
        value=TRACKING_STATUS_OPTIONS,
        property_type="Text",
        for_doctype=False,
    )
