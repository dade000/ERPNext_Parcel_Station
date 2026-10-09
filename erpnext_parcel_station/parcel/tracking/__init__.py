# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Carrier tracking ingestion.

Three carriers, two transport shapes:

* **Austrian Post** pushes XML event files (``POSTTRACK_*.xml``) to their SFTP
  server every hour; :mod:`post_sftp` checks hourly (06:00-22:00) and imports
  new files.
* **FedEx** and **GLS** are poll-only APIs; :mod:`fedex_track` and
  :mod:`gls_track` query open shipments hourly (06:00-22:00).

All three paths converge in :mod:`events`, which stores every raw carrier
event as a ``Parcel Tracking Event`` (deduplicated by ``event_id``), matches
events to Shipments via ``awb_number``, and maintains the canonical
``Shipment.tracking_status``.

The poll registry below mirrors ``_LABEL_ADAPTERS`` in
``parcel/infra/gls_integration.py``: carriers are keyed by the identities from
``parcel/carrier_routing.py``, and a carrier without a poll adapter simply is
not polled (Austrian Post: events arrive by file, not by poll).
"""
