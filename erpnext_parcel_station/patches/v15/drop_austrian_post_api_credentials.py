"""Wipe orphan ``tabSingles`` rows for the retired Parcel Station Settings
fields ``austrian_post_api_key`` and ``austrian_post_api_secret``.

Both fields were dead — they were validated at ``AustrianPostClient``
construction time but never sent to the carrier. The real SOAP auth is
``client_id`` + ``org_unit_id`` + ``org_unit_guid`` (the Organisation Unit
& Service section). Daniel asked for the API key/secret rows to be removed
from the UI; this patch deletes the now-orphan row data so the values don't
linger as encrypted blobs in the database.

Idempotent: ``DELETE`` on a missing row is a no-op.
"""
from __future__ import annotations

import frappe


RETIRED_SINGLE_FIELDS = (
    ("Parcel Station Settings", "austrian_post_api_key"),
    ("Parcel Station Settings", "austrian_post_api_secret"),
)


def execute() -> None:
    deleted = 0
    for doctype, field in RETIRED_SINGLE_FIELDS:
        n = frappe.db.sql(
            "DELETE FROM `tabSingles` WHERE doctype=%s AND field=%s",
            (doctype, field),
        )
        if n:
            deleted += 1
    frappe.db.commit()
    msg = (
        f"drop_austrian_post_api_credentials: removed orphan rows for "
        f"{deleted}/{len(RETIRED_SINGLE_FIELDS)} retired fields from tabSingles."
    )
    frappe.logger().info(msg)
    print(msg)
