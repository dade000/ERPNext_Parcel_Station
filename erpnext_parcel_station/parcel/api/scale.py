"""Server-side proxy for the warehouse weight scale.

The scale exposes a small HTTP service at ``scale_url``. A successful read
returns::

    {"raw": "+     1.56kg", "status": "success", "weight_kg": 1.56}

When the scale's serial device isn't present (cable unplugged, USB hub off)
the server returns HTTP 500 with ``{"status": "error", "message": "..."}``.

Why proxy on the Frappe side instead of calling the scale from the browser:
the scale's vhost doesn't emit CORS headers, so a direct ``fetch()`` from the
desk gets blocked. Routing through Frappe also keeps the Basic-auth creds off
the client.

Credentials live in the **Scale Settings** singleton DocType (admin-editable
from the desk). Legacy ``site_config.json`` values are migrated forward on
upgrade by ``patches/v15/migrate_scale_config_to_settings.py``.
"""
from __future__ import annotations

from typing import Any, Dict

import frappe
import requests
from requests.auth import HTTPBasicAuth


_DEFAULT_TIMEOUT_S = 5


def _load_scale_credentials() -> tuple[str | None, str | None, str | None]:
    """Return ``(url, username, password)`` from the Scale Settings singleton.

    Any of the three may be ``None`` if unset. The Password field is
    decrypted via ``get_password`` so the returned value is the cleartext
    secret usable as Basic-auth credentials. Returns ``(None, None, None)``
    if the doctype hasn't been migrated in yet (fresh sites before the
    Scale Settings patch lands).
    """
    if not frappe.db.exists("DocType", "Scale Settings"):
        return None, None, None

    settings = frappe.get_cached_doc("Scale Settings")
    url = (settings.scale_url or "").strip() or None
    username = (settings.scale_username or "").strip() or None
    password = settings.get_password("scale_password", raise_exception=False) or None
    return url, username, password


@frappe.whitelist()
def read_scale_weight() -> Dict[str, Any]:
    """Read the live package weight from the warehouse scale.

    Always returns a JSON-serialisable dict; never raises through. The desk
    caller can map each ``code`` to a distinct toast/copy:

      not_configured — scale_url / username / password missing in Scale Settings
      scale_offline  — server returned 500 with {"status": "error"} (serial device absent)
      auth           — server returned 401 (wrong creds)
      network        — could not reach the scale host
      timeout        — server didn't respond within ``_DEFAULT_TIMEOUT_S``
      bad_response   — body wasn't valid JSON / didn't have ``weight_kg``
      unexpected     — anything else

    Success shape::

        {"status": "success", "weight_kg": 1.56, "raw": "+     1.56kg"}
    """
    url, username, password = _load_scale_credentials()

    if not url or not username or not password:
        return {
            "status": "error",
            "code": "not_configured",
            "message": (
                "Scale endpoint not configured. Open 'Scale Settings' "
                "in the desk and fill in URL, username and password."
            ),
        }

    try:
        resp = requests.get(
            url,
            auth=HTTPBasicAuth(username, password),
            timeout=_DEFAULT_TIMEOUT_S,
        )
    except requests.exceptions.Timeout:
        return {
            "status": "error",
            "code": "timeout",
            "message": "Scale server did not respond in time.",
        }
    except requests.exceptions.RequestException as exc:
        return {
            "status": "error",
            "code": "network",
            "message": f"Could not reach scale server: {exc}",
        }

    if resp.status_code == 401:
        return {
            "status": "error",
            "code": "auth",
            "message": "Scale server rejected the credentials in site_config.json.",
        }

    try:
        payload = resp.json()
    except ValueError:
        return {
            "status": "error",
            "code": "bad_response",
            "message": (
                f"Scale server returned non-JSON (HTTP {resp.status_code})."
            ),
        }

    # Happy path: 200 + status=success + numeric weight_kg.
    if (
        isinstance(payload, dict)
        and payload.get("status") == "success"
        and isinstance(payload.get("weight_kg"), (int, float))
    ):
        return {
            "status": "success",
            "weight_kg": float(payload["weight_kg"]),
            "raw": payload.get("raw"),
        }

    # Scale-offline path: server emits HTTP 500 with {"status":"error",...}.
    # Surface it as a distinct code so the UI can say "plug in the scale"
    # rather than "the scale server is broken".
    if isinstance(payload, dict) and payload.get("status") == "error":
        return {
            "status": "error",
            "code": "scale_offline" if resp.status_code == 500 else "unexpected",
            "message": payload.get("message") or "Scale returned an error.",
        }

    return {
        "status": "error",
        "code": "unexpected",
        "message": f"Unexpected scale response (HTTP {resp.status_code}).",
    }
