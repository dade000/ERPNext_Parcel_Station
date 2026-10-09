# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
from frappe.model.document import Document


class ParcelTrackingEvent(Document):
    """One raw carrier tracking event, deduplicated by ``event_id``.

    Created exclusively by ``parcel/tracking/events.upsert_events`` — the
    doctype is ``in_create`` (no manual creation) and every field is
    read-only. Orphan events (no matching Shipment at import time) are legal
    and get re-matched by ``rematch_orphan_events``.
    """
