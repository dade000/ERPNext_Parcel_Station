from __future__ import annotations
import frappe


CONF_TO_FIELD = {
    "austrian_post_wsdl_url": "endpoint_url",
    "austrian_post_client_id": "client_id",
    "austrian_post_org_unit_guid": "org_unit_guid",
    "austrian_post_org_unit_id": "org_unit_id",
    "austrian_post_delivery_service_id": "delivery_service_third_party_id",
}


def execute():
    """Copy legacy Austrian Post values from site_config.json into Parcel Station Settings.

    Only fills DocType fields that are currently empty so re-running the patch
    or running it on a site that has already been configured via the UI is a
    no-op. site_config keys that are unset are skipped.
    """

    if not frappe.db.exists("DocType", "Parcel Station Settings"):
        return

    settings = frappe.get_single("Parcel Station Settings")
    conf = frappe.local.conf or {}

    migrated: list[str] = []
    skipped_existing: list[str] = []

    for conf_key, field in CONF_TO_FIELD.items():
        conf_value = conf.get(conf_key)
        if conf_value in (None, ""):
            continue

        if settings.get(field):
            skipped_existing.append(field)
            continue

        settings.set(field, str(conf_value))
        migrated.append(field)

    if migrated:
        settings.flags.ignore_permissions = True
        settings.save()
        frappe.db.commit()
        print(
            f"[parcel_station] migrated site_config -> Parcel Station Settings: {', '.join(migrated)}"
        )

    if skipped_existing:
        print(
            f"[parcel_station] kept existing values (not overwritten): {', '.join(skipped_existing)}"
        )
