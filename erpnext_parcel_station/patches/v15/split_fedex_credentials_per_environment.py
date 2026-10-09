"""Move the single FedEx credential set into per-environment fields.

The first cut of ``FedEx Settings`` had one API Key / Secret Key / Account
Number, with ``API Mode`` only switching the host. That makes going live a
destructive edit: production credentials overwrite the sandbox ones, and
development against the test environment is no longer possible without typing
the old values back in. FedEx issues genuinely separate credentials and
shipping accounts per environment, so the settings now mirror that.

Reading the old values needs raw SQL. ``bench migrate`` syncs DocTypes BEFORE
it runs patches, so by the time this executes the legacy fieldnames no longer
exist on the DocType and ``frappe.db.get_single_value`` refuses them with
"Field client_key does not exist". Their rows do survive in ``tabSingles``
(dropping a field from a Single does not delete its stored value), so the
values are still there to be read directly — and to be cleaned up afterwards.
"""

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

LEGACY_FIELDS = ("client_key", "account_number", "client_secret")


def _legacy_value(fieldname: str) -> str:
    row = frappe.db.sql(
        "select value from tabSingles where doctype=%s and field=%s",
        ("FedEx Settings", fieldname),
    )
    return (row[0][0] or "").strip() if row else ""


def _current_value(fieldname: str) -> str:
    return (frappe.db.get_single_value("FedEx Settings", fieldname) or "").strip()


def execute():
    if not frappe.db.exists("DocType", "FedEx Settings"):
        return

    mode = _legacy_value("api_mode") or "Sandbox"
    prefix = "production" if mode == "Production" else "sandbox"

    for suffix in ("client_key", "account_number"):
        value = _legacy_value(suffix)
        target = f"{prefix}_{suffix}"
        if value and not _current_value(target):
            frappe.db.set_single_value("FedEx Settings", target, value)
            frappe.logger().info(f"[patch] FedEx Settings.{suffix} -> {target}")

    # The secret lives encrypted in the Password store keyed by fieldname, not
    # in tabSingles, so it is re-keyed rather than copied as plain text.
    try:
        secret = get_decrypted_password(
            "FedEx Settings", "FedEx Settings", "client_secret", raise_exception=False
        )
    except Exception:
        secret = None
    if secret:
        target = f"{prefix}_client_secret"
        try:
            already = get_decrypted_password(
                "FedEx Settings", "FedEx Settings", target, raise_exception=False
            )
        except Exception:
            already = None
        if not already:
            set_encrypted_password("FedEx Settings", "FedEx Settings", secret, target)
            frappe.logger().info(f"[patch] FedEx Settings.client_secret -> {target}")

    # Drop the orphaned rows so a stale key cannot be recovered from the DB
    # after it has been rotated, and so the Single stops carrying dead fields.
    for fieldname in LEGACY_FIELDS:
        frappe.db.sql(
            "delete from tabSingles where doctype=%s and field=%s",
            ("FedEx Settings", fieldname),
        )
    frappe.db.sql(
        "delete from `__Auth` where doctype=%s and name=%s and fieldname=%s",
        ("FedEx Settings", "FedEx Settings", "client_secret"),
    )
