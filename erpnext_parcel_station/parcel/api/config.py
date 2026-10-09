from __future__ import annotations
import frappe


# Step 12: `_get_conf` was a thin wrapper around `frappe.conf.get` used by
# the legacy site_config-driven path. All callers have been migrated to
# read directly from doctypes (GLS Settings, Parcel Station Settings) so
# the helper is gone. If you need it back, prefer
# `frappe.db.get_single_value("<Settings Doctype>", "<field>")`.


def _get_ps_settings():
    """Return cached Parcel Station Settings (Single) or None if missing.

    Carrier-independent config only (general, sender, tracking monitoring) —
    the Austrian Post config moved to its own Single, see
    :func:`_get_ap_settings`.
    """
    try:
        return frappe.get_cached_doc("Parcel Station Settings")
    except Exception:
        return None


def _get_ap_settings():
    """Return cached Austrian Post Settings (Single) or None if missing.

    SOAP endpoint/SOAPActions, ClientID/Org Unit and the tracking SFTP access
    — split out of Parcel Station Settings (patch
    ``v15/split_austrian_post_settings``) to mirror the GLS/FedEx Settings
    pattern.
    """
    try:
        return frappe.get_cached_doc("Austrian Post Settings")
    except Exception:
        return None
