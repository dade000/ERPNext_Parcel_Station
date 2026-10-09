# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Fetch POSTTRACK tracking files from the Austrian Post SFTP server.

The Post drops one XML per hour into our directory on *their* server; this
module polls it via paramiko hourly (06:00-22:00), importing every file not yet
logged — the night's files arrive with the first run.
The file state machine: every remote file is processed inside its own
try/except with rollback -> Failed log row -> ``frappe.log_error`` -> commit,
so one broken file never poisons the batch. The state
lives in the **Post Tracking File** doctype (name = file name, plus a SHA-256
content hash), which makes re-reading an already-imported remote file a no-op
— the Post server may well be read-only for us, so files are left in place by
default (``sftp_move_processed`` turns on remote archiving where permitted).
"""

from __future__ import annotations

import hashlib
import posixpath
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime

from . import events, post_xml


class PostTrackingNotConfiguredError(Exception):
    """Post tracking disabled or SFTP settings incomplete.

    Distinct from real failures so the scheduler skips an unconfigured site
    quietly — same convention as ``FedExNotConfiguredError``.
    """


def _get_settings():
    settings = frappe.get_single("Austrian Post Settings")
    if not cint(settings.get("enable_post_tracking")):
        raise PostTrackingNotConfiguredError("Post tracking import is disabled.")
    host = cstr(settings.get("sftp_host")).strip()
    username = cstr(settings.get("sftp_username")).strip()
    password = cstr(settings.get_password("sftp_password", raise_exception=False) or "").strip()
    missing = [
        label
        for label, value in (("SFTP Host", host), ("SFTP Username", username), ("SFTP Password", password))
        if not value
    ]
    if missing:
        raise PostTrackingNotConfiguredError(
            f"Post tracking SFTP settings incomplete: missing {', '.join(missing)}."
        )
    return settings


def _connect(settings):
    """Open an SFTP session. Returns (transport, sftp) — caller closes both."""
    import paramiko

    transport = paramiko.Transport((cstr(settings.get("sftp_host")).strip(), cint(settings.get("sftp_port")) or 22))
    transport.connect(
        username=cstr(settings.get("sftp_username")).strip(),
        password=cstr(settings.get_password("sftp_password", raise_exception=False) or "").strip(),
    )
    return transport, paramiko.SFTPClient.from_transport(transport)


def _list_tracking_files(sftp, settings) -> list[str]:
    directory = cstr(settings.get("sftp_directory")).strip() or "."
    prefix = cstr(settings.get("sftp_filename_prefix")).strip() or "POSTTRACK_"
    names = [
        name
        for name in sftp.listdir(directory)
        if name.startswith(prefix) and name.lower().endswith(".xml")
    ]
    # File names embed the creation timestamp, so name order = chronological
    # order — events then arrive oldest-first, which keeps status derivation
    # simple.
    return sorted(names)


def _move_remote(sftp, settings, name: str, ok: bool) -> None:
    if not cint(settings.get("sftp_move_processed")):
        return
    target_dir = cstr(settings.get("sftp_processed_dir" if ok else "sftp_failed_dir")).strip()
    if not target_dir:
        return
    directory = cstr(settings.get("sftp_directory")).strip() or "."
    try:
        sftp.rename(posixpath.join(directory, name), posixpath.join(target_dir, name))
    except Exception:
        # Archiving is best-effort — dedup already protects against re-reads.
        frappe.log_error(
            title=f"Post tracking: could not move {name} on SFTP server",
            message=frappe.get_traceback(),
        )


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def fetch_post_tracking_files() -> dict[str, Any] | None:
    """Scheduler entry point: import every new POSTTRACK file on the server."""
    try:
        settings = _get_settings()
    except PostTrackingNotConfiguredError as exc:
        frappe.logger().info(f"[post-tracking] skipped: {exc}")
        return None

    summary: dict[str, Any] = {"files": 0, "imported": 0, "duplicates": 0, "failed": [], "shipments": set()}

    transport, sftp = _connect(settings)
    try:
        directory = cstr(settings.get("sftp_directory")).strip() or "."
        for name in _list_tracking_files(sftp, settings):
            if frappe.db.exists("Post Tracking File", name):
                continue  # already fetched in an earlier run
            summary["files"] += 1
            try:
                with sftp.open(posixpath.join(directory, name), "rb") as handle:
                    content = handle.read()
                ok = _process_file(name, posixpath.join(directory, name), content, summary)
                _move_remote(sftp, settings, name, ok)
                frappe.db.commit()
            except Exception:
                frappe.db.rollback()
                summary["failed"].append(name)
                frappe.log_error(
                    title=f"Post tracking: import failed for {name}",
                    message=frappe.get_traceback(),
                )
                _log_failure(name, posixpath.join(directory, name))
                frappe.db.commit()
    finally:
        try:
            sftp.close()
        finally:
            transport.close()

    # Orphans from earlier files may be matchable now (label created after the
    # first event arrived, multi-colli codes backfilled, ...).
    summary["shipments"] |= events.rematch_orphan_events()
    events.update_shipment_statuses(summary["shipments"])
    frappe.db.commit()

    summary["shipments"] = sorted(summary["shipments"])
    frappe.logger().info(f"[post-tracking] run done: {summary}")
    return summary


def _process_file(name: str, remote_path: str, content: bytes, summary: dict[str, Any]) -> bool:
    """Parse + import one file, writing its Post Tracking File log row."""
    content_hash = hashlib.sha256(content).hexdigest()

    if frappe.db.exists("Post Tracking File", {"content_hash": content_hash}):
        # Same bytes under a new name (re-delivery) — record it and move on.
        _create_log(name, remote_path, content, content_hash, status="Duplicate")
        summary["duplicates"] += 1
        return True

    try:
        parsed = post_xml.parse(content)
    except post_xml.PostTrackingParseError as exc:
        _create_log(name, remote_path, content, content_hash, status="Failed", error=str(exc))
        summary["failed"].append(name)
        return False

    log = _create_log(name, remote_path, content, content_hash, status="Imported")
    result = events.upsert_events(
        [{**event, "source_file": log.name} for event in parsed["events"]]
    )
    log.db_set(
        {
            "event_count": len(parsed["events"]),
            "matched_count": result["matched"],
            "orphan_count": result["orphans"],
            "duplicate_event_count": result["duplicates"],
        },
        update_modified=False,
    )
    summary["imported"] += 1
    summary["shipments"] |= result["shipments"]
    return True


def _create_log(name, remote_path, content, content_hash, status="Imported", error=None):
    log = frappe.get_doc(
        {
            "doctype": "Post Tracking File",
            "file_name": name,
            "remote_path": remote_path,
            "content_hash": content_hash,
            "status": status,
            "fetched_at": now_datetime(),
            "error_message": error,
        }
    )
    log.insert(ignore_permissions=True)

    frappe.get_doc(
        {
            "doctype": "File",
            "file_name": name,
            "attached_to_doctype": "Post Tracking File",
            "attached_to_name": log.name,
            "content": content,
            "is_private": 1,
        }
    ).insert(ignore_permissions=True)
    return log


def _log_failure(name: str, remote_path: str) -> None:
    """Failed log row after a rollback — best-effort, never raises."""
    try:
        if not frappe.db.exists("Post Tracking File", name):
            frappe.get_doc(
                {
                    "doctype": "Post Tracking File",
                    "file_name": name,
                    "remote_path": remote_path,
                    "status": "Failed",
                    "fetched_at": now_datetime(),
                    "error_message": "Import failed — see Error Log.",
                }
            ).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(
            title=f"Post tracking: could not write failure log for {name}",
            message=frappe.get_traceback(),
        )


@frappe.whitelist()
def fetch_now() -> dict[str, Any]:
    """Manual trigger (Parcel Station Settings form / bench execute)."""
    frappe.only_for("System Manager")
    summary = fetch_post_tracking_files()
    if summary is None:
        frappe.throw("Post tracking import is disabled or not configured (Austrian Post Settings).")
    return summary
