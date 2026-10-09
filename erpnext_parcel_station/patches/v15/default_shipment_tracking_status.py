"""Default Shipment.tracking_status to "In Progress".

The Select field ships with no default, which leaves freshly-created
Shipments with an empty Tracking Status. Ops wants the first state
("In Progress") pre-filled so the column is never blank in list views
and so downstream label/tracking flows have something to read.

"In Progress" is already one of the field's existing Select options
(see core ERPNext: ``\\nIn Progress\\nDelivered\\nReturned\\nLost``).

Idempotent: ``make_property_setter`` updates the existing row if one
was created by a previous run.
"""
from __future__ import annotations

from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute() -> None:
    make_property_setter(
        doctype="Shipment",
        fieldname="tracking_status",
        property="default",
        value="In Progress",
        property_type="Text",
        for_doctype=False,
    )
