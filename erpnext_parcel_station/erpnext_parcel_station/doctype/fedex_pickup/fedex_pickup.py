# Copyright (c) 2026, Devich Daniel and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FedExPickup(Document):
    """One courier pickup requested at FedEx (Pickup Request API).

    Written only by ``parcel.infra.fedex_pickup``; the form is a read-only
    record of what was sent and what FedEx answered. Shipments covered by a
    pickup point at it through ``Shipment.fedex_pickup``.
    """
