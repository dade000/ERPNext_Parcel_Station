"""Drop the obsolete Devich callback fields from existing Parcel Station Settings installations.

Step 5 of the v3 cleanup retired the HTTP webhook bridge between parcel-station
and Devich. Three Single-doctype fields powered that bridge:

  * ``devich_callback_url``       — URL Devich expected label POSTs at
  * ``devich_webhook_secret``     — shared HMAC secret with Devich
  * ``devich_callback_timeout``   — HTTP timeout for the outbound POST

The fields have already been removed from the ``Parcel Station Settings`` JSON,
but existing installations may still have stored values in ``tabSingles``.
This patch deletes those rows. Safe to run multiple times: if no rows match,
the DELETE is a no-op.

``Parcel Station Settings`` is a Single doctype (``issingle=1``), so its values
live as ``(doctype, field, value)`` rows in ``tabSingles`` — there is no
``tabParcel Station Settings`` table to drop columns from. The patch reflects
that storage model.
"""
from __future__ import annotations

import frappe


OBSOLETE_FIELDS = (
    "devich_callback_url",
    "devich_webhook_secret",
    "devich_callback_timeout",
)


def execute() -> None:
    if not frappe.db.exists("DocType", "Parcel Station Settings"):
        return

    frappe.db.sql(
        """
        DELETE FROM `tabSingles`
        WHERE doctype = %s
          AND field IN %s
        """,
        ("Parcel Station Settings", OBSOLETE_FIELDS),
    )
    frappe.db.commit()
