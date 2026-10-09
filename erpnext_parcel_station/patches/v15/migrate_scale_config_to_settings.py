"""Copy legacy scale credentials from ``site_config.json`` into the
Scale Settings singleton DocType.

Mirrors ``migrate_site_config_to_parcel_station_settings``: only fills
fields that are currently empty, so re-runs (or running against a site
that already configured Scale Settings via the desk) are no-ops.
"""
from __future__ import annotations

import frappe


CONF_TO_FIELD = {
    "scale_url": "scale_url",
    "scale_username": "scale_username",
    "scale_password": "scale_password",
}


def execute():
    if not frappe.db.exists("DocType", "Scale Settings"):
        return

    settings = frappe.get_single("Scale Settings")
    conf = frappe.local.conf or {}

    migrated: list[str] = []
    skipped_existing: list[str] = []

    for conf_key, field in CONF_TO_FIELD.items():
        conf_value = conf.get(conf_key)
        if conf_value in (None, ""):
            continue

        # Password field requires the encrypted-read helper to compare;
        # for the URL/username fields a plain getattr is enough.
        if field == "scale_password":
            current = settings.get_password(field, raise_exception=False)
        else:
            current = settings.get(field)

        if current:
            skipped_existing.append(field)
            continue

        settings.set(field, str(conf_value))
        migrated.append(field)

    if migrated:
        settings.flags.ignore_permissions = True
        settings.save()
        frappe.db.commit()
        print(
            f"[parcel_station] migrated site_config -> Scale Settings: {', '.join(migrated)}"
        )

    if skipped_existing:
        print(
            f"[parcel_station] kept existing Scale Settings values "
            f"(not overwritten): {', '.join(skipped_existing)}"
        )
