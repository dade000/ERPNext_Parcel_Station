"""Move the Austrian Post configuration into its own Settings single.

Historically the Post was the odd one out: GLS and FedEx each have a Settings
single, while the Post's SOAP config (endpoint, SOAPActions, ClientID/Org
Unit, customs option) and the new tracking-SFTP block lived inside Parcel
Station Settings. This patch carries the stored values over to the new
**Austrian Post Settings** single; the fields themselves are gone from the
Parcel Station Settings doctype JSON as of this release.

Details that matter:

* Values are read straight from ``tabSingles`` (``get_singles_dict``) — the
  old fields are no longer in the meta at patch time (post_model_sync), so a
  ``get_single()`` read would not see them.
* ``sftp_password`` is a Password field: its value lives encrypted in
  ``__Auth``, keyed by doctype — it is re-keyed via the password utils, not
  copied as a Singles row.
* Writes use ``db.set_value(..., update_modified=False)`` and only fill
  fields that are still empty on the target, so re-running the patch (or
  running it after someone already configured the new single) never
  overwrites newer values.
* The migrated ``tabSingles`` rows of the old doctype are deleted afterwards
  so stale copies of credentials do not linger.
"""
from __future__ import annotations

import frappe
from frappe.utils.password import get_decrypted_password, remove_encrypted_password, set_encrypted_password

OLD = "Parcel Station Settings"
NEW = "Austrian Post Settings"

#: Plain fields carried over 1:1 (same fieldnames in the new single).
FIELDS = (
    "enable_austrian_post_integration",
    "endpoint_url",
    "austrian_post_base_url",
    "ap_import_soapaction",
    "ap_cancel_soapaction",
    "client_id",
    "org_unit_guid",
    "org_unit_id",
    "customs_option_id",
    "enable_post_tracking",
    "sftp_host",
    "sftp_port",
    "sftp_username",
    "sftp_directory",
    "sftp_filename_prefix",
    "sftp_move_processed",
    "sftp_processed_dir",
    "sftp_failed_dir",
)


def execute() -> None:
    # Both sides are read as raw Singles rows on purpose.
    #
    # NOT frappe.db.get_single_value for the target: it casts through the
    # fieldtype, so a Check/Int field with no stored row comes back as 0 rather
    # than None and would wrongly count as "already configured", silently
    # skipping the migration of an enabled flag.
    #
    # NOT frappe.db.get_value("Singles", ...) either: get_value appends a
    # default ORDER BY `modified`, and tabSingles has no such column — the query
    # dies with "Unknown column 'modified' in 'ORDER BY'". get_singles_dict is
    # the supported way to read a Single's raw rows, and it costs one query
    # instead of one per field.
    old_values = frappe.db.get_singles_dict(OLD)
    new_values = frappe.db.get_singles_dict(NEW)

    for fieldname in FIELDS:
        value = old_values.get(fieldname)
        if value in (None, ""):
            continue
        if new_values.get(fieldname) in (None, ""):
            frappe.db.set_value(NEW, NEW, fieldname, value, update_modified=False)

    password = get_decrypted_password(OLD, OLD, "sftp_password", raise_exception=False)
    if password and not get_decrypted_password(NEW, NEW, "sftp_password", raise_exception=False):
        set_encrypted_password(NEW, NEW, password, "sftp_password")

    # Drop the migrated rows from the old single so credentials and endpoints
    # exist in exactly one place.
    frappe.db.delete("Singles", {"doctype": OLD, "field": ("in", list(FIELDS) + ["sftp_password"])})
    remove_encrypted_password(OLD, OLD, "sftp_password")
