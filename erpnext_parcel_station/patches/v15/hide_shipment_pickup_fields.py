"""Hide Shipment pickup date/time fields from the form UI.

The pickup window — ``pickup_date``, ``pickup_from``, ``pickup_to`` — is
not operationally tracked on holzschuhe.at. Stock ERPNext shows them in
the Pickup section and marks ``pickup_date`` mandatory, which clutters
the form and blocks save when ops can't or doesn't fill them in.

We override via Property Setter so the change survives ERPNext upgrades.
For each of the three fields we set:

- ``hidden = 1`` — never rendered on the form.
- ``reqd   = 0`` — save never fails because the field is empty.

The underlying database column stays, so any historical pickup values
aren't dropped and the auto-create flow in ``parcel/events/delivery_note.py``
that pre-fills ``pickup_date`` keeps working harmlessly.

Idempotent: ``make_property_setter`` updates the existing row if one was
created by a previous run.

Replaces the earlier ``relax_shipment_pickup_mandatory`` patch (which
only flipped ``reqd`` on ``pickup_from`` / ``pickup_to``) — superseded
by full hiding on all three fields.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute() -> None:
    for fieldname in ("pickup_date", "pickup_from", "pickup_to"):
        for prop, value in (("hidden", "1"), ("reqd", "0")):
            make_property_setter(
                doctype="Shipment",
                fieldname=fieldname,
                property=prop,
                value=value,
                property_type="Check",
                for_doctype=False,
            )
