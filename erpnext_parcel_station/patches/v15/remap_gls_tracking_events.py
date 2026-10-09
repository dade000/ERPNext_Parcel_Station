# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Re-read stored GLS tracking events with the corrected status mapping.

GLS history entries were misread in four ways: DATA_RECEIVED (GLS has the
data, not the parcel) was stored as "In Transit", so the customer's tracking
page said "on its way" as soon as the label was printed; IN_DELIVERY never
became "Out for Delivery"; a parcel deposited at a ParcelShop read as
"Delivered", which also stopped its polling; and no event carried a place,
because GLS reports it as ``Location`` rather than an address.

Stored events are never fetched again, so all of that is applied to them
here. Shipments reopened this way are polled once right after the migrate,
so that anything that happened while they counted as delivered (the
collection at the ParcelShop) arrives before the next reminder run. Also
rewrites the stored ``Shipment.tracking_url`` of GLS shipments: the link
pointed at a page that no longer exists.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Parcel Tracking Event"):
        return
    from erpnext_parcel_station.parcel.tracking.gls_track import remap_stored_events

    summary = remap_stored_events()
    if summary.get("shipments"):
        # After the commit, in the background: a carrier that does not answer
        # must not hold up or fail the migrate.
        frappe.enqueue(
            "erpnext_parcel_station.parcel.tracking.gls_track.poll_gls_tracking",
            queue="long",
            enqueue_after_commit=True,
        )

    # The link shown in the Desk; the customer page builds its own on each call.
    old_prefix = "https://gls-group.eu/AT/de/paketverfolgung?match="
    new_prefix = "https://gls-group.com/AT/de/paket-verfolgen?match="
    frappe.db.sql(
        """
        update `tabShipment`
        set tracking_url = replace(tracking_url, %s, %s)
        where tracking_url like %s
        """,
        (old_prefix, new_prefix, old_prefix + "%"),
    )
