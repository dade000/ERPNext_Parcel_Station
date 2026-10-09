"""FedEx courier pickup (Pickup Request API).

There is no standing pickup agreement with FedEx: a courier only comes when
one is booked. This module books one pickup per pickup address for every
submitted FedEx shipment that carries a label and is not yet covered by a
pickup, records it as a ``FedEx Pickup`` document and links the shipments to
it through ``Shipment.fedex_pickup``.

Two entry points:

* ``request_pending_pickups`` — scheduler, 12:00 Mon-Fri (hooks.py). Books
  today's pickup for the configured window (default 12:30-16:00) when the
  switch in FedEx Settings is on.
* ``request_fedex_pickup`` / ``cancel_fedex_pickup`` — the Parcel Station
  buttons. A manual request late in the day rolls over to the next business
  day, because FedEx wants the ready time in the future and before its cutoff.

Failure handling follows the Saferpay-capture rule "retry instead of block":
a failed request leaves the shipments unlinked, so the next run picks them up
again. Every failure is written to the Error Log and, when an address is
configured, mailed.

Field names follow the FedEx Pickup Request API v1 (``countryRelationships``
is plural on create and singular on availability — that is FedEx, not a typo).

Sandbox (verified 2026-09-24): the sandbox is virtualised and only answers
canned scenarios. Exactly this request body is accepted with
``pickupDateType: FUTURE_DAY`` (confirmation code + location come back, cancel
works) but gets ``503 SERVICE.UNAVAILABLE.ERROR`` with ``SAME_DAY`` — the
same body, only the date type differs. SAME_DAY therefore cannot be proven in
the sandbox; the first production run proves it, and a failure there is logged,
mailed and retried like any other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, get_url_to_form, now_datetime, nowdate

from ..api.addresses import _country_code
from ..api.carrier import _resolve_label_weight
from ..carrier_routing import FEDEX, resolve_carrier
from ..tracking.status_mapping import STATUS_ANNOUNCED, STATUS_IN_PROGRESS
from .fedex_api import (
    FedExAPIClient,
    FedExAPIError,
    FedExNotConfiguredError,
    _address_doc,
    _ascii_field,
    _fedex_address,
    _fedex_contact,
    _get_credentials,
)

DEFAULT_CARRIER_CODE = "FDXE"
DEFAULT_READY_TIME = time(12, 30)
DEFAULT_CLOSE_TIME = time(16, 0)
DEFAULT_PACKAGE_LOCATION = "FRONT"
REMARKS_MAX_LEN = 60

#: A shipment labelled longer ago than this without a pickup is assumed to have
#: left the building some other way (it predates the feature, or was dropped
#: off) and is not booked into today's pickup. Four days spans Friday afternoon
#: to Monday noon with a day to spare.
OPEN_SHIPMENT_MAX_AGE_DAYS = 4

#: Tracking statuses that mean "no physical scan yet". Anything else (In
#: Transit, Delivered, Problem, ...) proves FedEx already has the parcel, so it
#: must not be booked into another pickup — regardless of its age. "In Progress"
#: is the value before any event, "Announced" is FedEx's label-created event.
NOT_YET_SCANNED = frozenset({"", STATUS_IN_PROGRESS, STATUS_ANNOUNCED})

#: FedEx wants the ready time in the future. A manual request at 14:58 books
#: 15:15: now plus this lead, rounded up to the next quarter hour.
MANUAL_READY_LEAD = timedelta(minutes=15)
READY_TIME_STEP_MINUTES = 15

#: Same-day only when at least this much of the window is left after the ready
#: time; otherwise the request rolls over to the next business day.
MIN_WINDOW = timedelta(minutes=45)

_SHIPMENT_FIELDS = [
    "name",
    "docstatus",
    "carrier",
    "carrier_service",
    "custom_carrier_service",
    "delivery_type",
    "awb_number",
    "pickup_address_name",
    "fedex_pickup",
    "tracking_status",
    "creation",
]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PickupSettings:
    enabled: bool
    ready_time: time
    close_time: time
    carrier_code: str
    package_location: str
    remarks: str
    notification_email: str


def _to_time(value: Any, default: time) -> time:
    """Frappe hands a Time field back as ``timedelta`` (DB) or ``str`` (form)."""
    if not value:
        return default
    if isinstance(value, timedelta):
        return (datetime.min + value).time()
    if isinstance(value, time):
        return value
    try:
        return datetime.strptime(str(value)[:8], "%H:%M:%S").time()
    except ValueError:
        try:
            return datetime.strptime(str(value)[:5], "%H:%M").time()
        except ValueError:
            return default


def get_pickup_settings() -> PickupSettings:
    settings = frappe.get_single("FedEx Settings")
    return PickupSettings(
        enabled=bool(cint(settings.get("enable_auto_pickup"))),
        ready_time=_to_time(settings.get("pickup_ready_time"), DEFAULT_READY_TIME),
        close_time=_to_time(settings.get("pickup_close_time"), DEFAULT_CLOSE_TIME),
        carrier_code=cstr(settings.get("pickup_carrier_code")).strip() or DEFAULT_CARRIER_CODE,
        package_location=cstr(settings.get("pickup_package_location")).strip()
        or DEFAULT_PACKAGE_LOCATION,
        remarks=_ascii_field(settings.get("pickup_remarks"))[:REMARKS_MAX_LEN],
        notification_email=cstr(settings.get("pickup_notification_email")).strip(),
    )


# ---------------------------------------------------------------------------
# Pickup window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PickupWindow:
    pickup_date: date
    ready_time: time
    close_time: time
    date_type: str  # SAME_DAY | FUTURE_DAY

    @property
    def ready_timestamp(self) -> str:
        # FedEx documents the trailing "Z" but reads the value as local time at
        # the pickup address; sending 12:30Z books 12:30 local time.
        return f"{self.pickup_date.isoformat()}T{self.ready_time.strftime('%H:%M:%S')}Z"


def next_business_day(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _round_up(moment: datetime, step_minutes: int) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    remainder = moment.minute % step_minutes
    if remainder:
        moment += timedelta(minutes=step_minutes - remainder)
    return moment


def pickup_window(settings: PickupSettings, now: datetime) -> PickupWindow:
    """Today if enough of the window is left, else the next business day.

    The 12:00 run lands on the configured ready time (12:30). A manual request
    later in the day moves the ready time forward to "now plus a quarter hour"
    so FedEx does not reject a ready time in the past, and once fewer than
    ``MIN_WINDOW`` remain before closing it books the next business day at the
    configured ready time instead.
    """
    today = now.date()
    if today.weekday() < 5:
        close_dt = datetime.combine(today, settings.close_time)
        ready_dt = max(
            datetime.combine(today, settings.ready_time),
            _round_up(now + MANUAL_READY_LEAD, READY_TIME_STEP_MINUTES),
        )
        if ready_dt + MIN_WINDOW <= close_dt:
            return PickupWindow(today, ready_dt.time(), settings.close_time, "SAME_DAY")

    return PickupWindow(
        next_business_day(today), settings.ready_time, settings.close_time, "FUTURE_DAY"
    )


# ---------------------------------------------------------------------------
# Which shipments
# ---------------------------------------------------------------------------


def not_yet_scanned(row: dict) -> bool:
    """True while the carrier has not physically handled the parcel."""
    return cstr(row.get("tracking_status")).strip() in NOT_YET_SCANNED


def open_fedex_shipments(max_age_days: int = OPEN_SHIPMENT_MAX_AGE_DAYS) -> list[dict]:
    """Submitted FedEx shipments with a label, no pickup and no carrier scan,
    newest last.

    The tracking status is the second guard next to the age cutoff: a parcel
    that was dropped off (or collected by an earlier pickup after being
    labelled late) shows a FedEx scan within hours and drops out here; the age
    cutoff covers parcels whose tracking never updated.
    """
    since = now_datetime() - timedelta(days=max_age_days)
    rows = frappe.get_all(
        "Shipment",
        filters={
            "docstatus": 1,
            "awb_number": ["is", "set"],
            "fedex_pickup": ["is", "not set"],
            "creation": [">=", since],
        },
        fields=_SHIPMENT_FIELDS,
        order_by="creation asc",
    )
    return [row for row in rows if not_yet_scanned(row) and resolve_carrier(row) == FEDEX]


def _selected_shipments(names: Iterable[str]) -> list[dict]:
    """Validate an explicit selection the same way the automatic run filters."""
    names = [cstr(n).strip() for n in names if cstr(n).strip()]
    if not names:
        return []
    rows = frappe.get_all(
        "Shipment", filters={"name": ["in", names]}, fields=_SHIPMENT_FIELDS, order_by="creation asc"
    )
    found = {row["name"]: row for row in rows}
    for name in names:
        row = found.get(name)
        if not row:
            frappe.throw(_("Shipment {0} was not found.").format(name))
        if cint(row.get("docstatus")) != 1:
            frappe.throw(_("Shipment {0} is not submitted.").format(name))
        if not row.get("awb_number"):
            frappe.throw(_("Shipment {0} has no FedEx label yet.").format(name))
        if resolve_carrier(row) != FEDEX:
            frappe.throw(_("Shipment {0} is not a FedEx shipment.").format(name))
        if row.get("fedex_pickup"):
            frappe.throw(
                _("Shipment {0} is already covered by pickup {1}.").format(name, row["fedex_pickup"])
            )
        if not not_yet_scanned(row):
            frappe.throw(
                _("Shipment {0} is already with FedEx (tracking status {1}).").format(
                    name, row.get("tracking_status")
                )
            )
    return [found[name] for name in names]


def _group_by_pickup_address(rows: list[dict]) -> Dict[str, list[dict]]:
    groups: Dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(cstr(row.get("pickup_address_name")), []).append(row)
    return groups


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def _delivery_country(doc: Document) -> str:
    address = _address_doc(
        doc.get("delivery_address_name")
        or doc.get("shipping_address_name")
        or doc.get("customer_address")
    )
    return _country_code(address.get("country")) if address else ""


def country_relationship(origin_country: str, destination_countries: Iterable[str]) -> str:
    """INTERNATIONAL as soon as one parcel leaves the origin country."""
    return (
        "INTERNATIONAL"
        if any(c and c != origin_country for c in destination_countries)
        else "DOMESTIC"
    )


def build_pickup_payload(
    shipments: list[Document],
    pickup_address: Document,
    window: PickupWindow,
    settings: PickupSettings,
    account_number: str,
    destination_countries: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Assemble the Create Pickup body for one pickup address."""
    first = shipments[0]
    contact = _fedex_contact(
        person_name=first.get("pickup") or "",
        company_name=first.get("pickup_company") or "",
        address=pickup_address,
        contact_name=first.get("pickup_contact_person"),
        email=first.get("pickup_contact_email"),
        role="shipper",
    )
    # The pickup contact takes name, company and phone; the e-mail belongs to
    # the (optional) pickupNotificationDetail block, not here.
    contact.pop("emailAddress", None)

    total_weight = sum(_resolve_label_weight(doc.as_dict()) for doc in shipments)
    origin_country = _country_code(pickup_address.get("country"))
    if destination_countries is None:
        destination_countries = [_delivery_country(doc) for doc in shipments]

    payload: Dict[str, Any] = {
        "associatedAccountNumber": {"value": account_number},
        "originDetail": {
            "pickupLocation": {
                "contact": contact,
                "address": _fedex_address(pickup_address),
            },
            "readyDateTimestamp": window.ready_timestamp,
            "customerCloseTime": window.close_time.strftime("%H:%M:%S"),
            "pickupDateType": window.date_type,
            "packageLocation": settings.package_location,
        },
        "carrierCode": settings.carrier_code,
        "packageCount": len(shipments),
        "totalWeight": {"units": "KG", "value": round(total_weight, 2)},
        "countryRelationships": country_relationship(origin_country, destination_countries),
    }
    if settings.remarks:
        payload["remarks"] = settings.remarks
    return payload


def build_cancel_payload(
    pickup: Document, account_number: str, pickup_address: Optional[Document], reason: str = ""
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "associatedAccountNumber": {"value": account_number},
        "pickupConfirmationCode": cstr(pickup.confirmation_code),
        "carrierCode": cstr(pickup.carrier_code) or DEFAULT_CARRIER_CODE,
        "scheduledDate": cstr(pickup.pickup_date)[:10],
        "remarks": _ascii_field(reason or "Cancelled from ERPNext")[:REMARKS_MAX_LEN],
    }
    if pickup.location_code:
        payload["location"] = cstr(pickup.location_code)
    if pickup_address:
        payload["accountAddressOfRecord"] = _fedex_address(pickup_address)
    return payload


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------


def _shipment_link(name: str) -> str:
    return f'<a href="{get_url_to_form("Shipment", name)}">{name}</a>'


def _notify_failure(
    settings: PickupSettings, title: str, detail: str, shipment_names: list[str]
) -> None:
    """Error Log always; mail when FedEx Settings names an address."""
    listing = ", ".join(shipment_names) or "-"
    frappe.log_error(title=title[:140], message=f"{detail}\n\nShipments: {listing}")
    if not settings.notification_email:
        return
    try:
        frappe.sendmail(
            recipients=[settings.notification_email],
            subject=title,
            message=(
                f"<p>{frappe.utils.escape_html(detail)}</p>"
                f"<p>Shipments: {', '.join(_shipment_link(n) for n in shipment_names) or '-'}</p>"
                "<p>The shipments stay open and are retried by the next 12:00 run, "
                "or request the pickup manually in the Parcel Station.</p>"
            ),
        )
    except Exception:
        frappe.log_error(title="FedEx pickup failure mail not sent", message=frappe.get_traceback())


def _record(pickup: Document, **values: Any) -> Document:
    pickup.update(values)
    pickup.insert(ignore_permissions=True)
    return pickup


def _request_one(
    client: FedExAPIClient,
    creds,
    settings: PickupSettings,
    window: PickupWindow,
    address_name: str,
    rows: list[dict],
    trigger: str,
) -> dict:
    names = [row["name"] for row in rows]
    pickup = frappe.new_doc("FedEx Pickup")
    pickup.update(
        {
            "pickup_date": window.pickup_date,
            "ready_time": window.ready_time.strftime("%H:%M:%S"),
            "close_time": window.close_time.strftime("%H:%M:%S"),
            "pickup_date_type": window.date_type,
            "carrier_code": settings.carrier_code,
            "mode": creds.mode,
            "trigger": trigger,
            "pickup_address": address_name or None,
            "package_count": len(rows),
            "remarks": settings.remarks,
            "shipment_names": "\n".join(names),
        }
    )

    try:
        pickup_address = _address_doc(address_name)
        if not pickup_address:
            raise FedExAPIError(
                _("Shipments {0} have no pickup address; FedEx needs one to send a courier.").format(
                    ", ".join(names)
                )
            )
        docs = [frappe.get_doc("Shipment", name) for name in names]
        payload = build_pickup_payload(docs, pickup_address, window, settings, creds.account_number)
        pickup.total_weight_kg = payload["totalWeight"]["value"]
        pickup.country_relationship = payload["countryRelationships"]
        pickup.request_payload = json.dumps(payload, indent=2, ensure_ascii=False)
        response = client.create_pickup(payload)
    except Exception as exc:
        # frappe.throw inside the payload builder (missing phone) lands here too;
        # its message is already on the message log for an interactive caller.
        error = cstr(exc)
        frappe.logger().warning(f"[fedex-pickup] request failed for {names}: {error}")
        _record(pickup, status="Failed", error_message=error[:4000])
        _notify_failure(
            settings,
            f"FedEx pickup request failed ({pickup.name})",
            f"FedEx did not accept the pickup for {window.pickup_date} "
            f"{window.ready_time:%H:%M}-{window.close_time:%H:%M}: {error}",
            names,
        )
        return {"pickup": pickup.name, "status": "Failed", "shipments": names, "error": error}

    output = response.get("output") or {}
    _record(
        pickup,
        status="Requested",
        confirmation_code=cstr(output.get("pickupConfirmationCode")).strip(),
        location_code=cstr(output.get("location")).strip(),
        response_payload=json.dumps(response, indent=2, ensure_ascii=False),
    )
    note = _("FedEx pickup {0} requested for {1}, {2}-{3}. Confirmation {4}.").format(
        pickup.name,
        frappe.utils.formatdate(window.pickup_date),
        window.ready_time.strftime("%H:%M"),
        window.close_time.strftime("%H:%M"),
        pickup.confirmation_code or "-",
    )
    for doc in docs:
        frappe.db.set_value("Shipment", doc.name, "fedex_pickup", pickup.name, update_modified=False)
        try:
            doc.add_comment("Info", note)
        except Exception:
            frappe.log_error(title="FedEx pickup comment failed", message=frappe.get_traceback())

    frappe.logger().info(
        f"[fedex-pickup] {pickup.name}: {len(names)} shipment(s), {window.pickup_date} "
        f"{window.ready_time:%H:%M}-{window.close_time:%H:%M}, confirmation "
        f"{pickup.confirmation_code or '-'} ({trigger})"
    )
    return {
        "pickup": pickup.name,
        "status": "Requested",
        "shipments": names,
        "confirmation_code": pickup.confirmation_code,
        "location_code": pickup.location_code,
    }


def request_pickup(
    shipment_names: Optional[Iterable[str]] = None,
    trigger: str = "manual",
    now: Optional[datetime] = None,
) -> dict:
    """Book pickups for the given shipments (or every open FedEx shipment).

    One FedEx call per pickup address. Raises ``FedExNotConfiguredError`` when
    the integration is off; per-address failures are recorded, not raised.
    """
    creds = _get_credentials()
    settings = get_pickup_settings()
    now = now or now_datetime()

    rows = _selected_shipments(shipment_names) if shipment_names is not None else open_fedex_shipments()
    window = pickup_window(settings, now)
    result: dict = {
        "shipments": [row["name"] for row in rows],
        "pickups": [],
        "window": {
            "pickup_date": window.pickup_date.isoformat(),
            "ready_time": window.ready_time.strftime("%H:%M"),
            "close_time": window.close_time.strftime("%H:%M"),
            "date_type": window.date_type,
        },
    }
    if not rows:
        frappe.logger().info(f"[fedex-pickup] nothing to book ({trigger}).")
        return result

    client = FedExAPIClient(creds)
    for address_name, group in _group_by_pickup_address(rows).items():
        result["pickups"].append(
            _request_one(client, creds, settings, window, address_name, group, trigger)
        )
    return result


def cancel_pickup(pickup_name: str, reason: str = "") -> dict:
    """Cancel a requested pickup at FedEx and release its shipments."""
    pickup = frappe.get_doc("FedEx Pickup", pickup_name)
    if pickup.status != "Requested":
        frappe.throw(_("Pickup {0} is {1} and cannot be cancelled.").format(pickup.name, pickup.status))
    if not pickup.confirmation_code:
        frappe.throw(_("Pickup {0} has no confirmation code to cancel.").format(pickup.name))

    creds = _get_credentials()
    payload = build_cancel_payload(pickup, creds.account_number, _address_doc(pickup.pickup_address), reason)
    response = FedExAPIClient(creds).cancel_pickup(payload)
    output = response.get("output") or {}

    pickup.db_set(
        {
            "status": "Cancelled",
            "cancelled_at": now_datetime(),
            "cancel_message": cstr(output.get("cancelConfirmationMessage"))[:1000],
        },
        update_modified=True,
    )
    released = frappe.get_all("Shipment", filters={"fedex_pickup": pickup.name}, pluck="name")
    note = _("FedEx pickup {0} (confirmation {1}) cancelled.").format(pickup.name, pickup.confirmation_code)
    for name in released:
        frappe.db.set_value("Shipment", name, "fedex_pickup", None, update_modified=False)
        try:
            frappe.get_doc("Shipment", name).add_comment("Info", note)
        except Exception:
            frappe.log_error(title="FedEx pickup comment failed", message=frappe.get_traceback())

    frappe.logger().info(f"[fedex-pickup] {pickup.name} cancelled, released {released}")
    return {
        "pickup": pickup.name,
        "status": "Cancelled",
        "shipments": released,
        "message": cstr(output.get("cancelConfirmationMessage")),
    }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def request_pending_pickups() -> dict | None:
    """Scheduler entry point (12:00 Mon-Fri)."""
    settings = get_pickup_settings()
    if not settings.enabled:
        return None
    try:
        return request_pickup(trigger="auto")
    except FedExNotConfiguredError as exc:
        frappe.logger().info(f"[fedex-pickup] skipped: {exc}")
        return None
    except Exception:
        frappe.log_error(title="FedEx pickup run failed", message=frappe.get_traceback())
        _notify_failure(
            settings,
            "FedEx pickup run failed",
            "The 12:00 pickup run aborted before any pickup was booked; see the Error Log.",
            [],
        )
        return None


# ---------------------------------------------------------------------------
# Parcel Station endpoints
# ---------------------------------------------------------------------------


def _require_permission() -> None:
    if not frappe.has_permission("Shipment", "write"):
        frappe.throw(_("Not permitted to manage FedEx pickups."), frappe.PermissionError)


def _parse_names(value: Any) -> Optional[list[str]]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    if isinstance(value, str):
        value = [value]
    return [cstr(v) for v in value]


@frappe.whitelist(methods=["GET"])
def get_fedex_pickup_overview() -> dict:
    """What the Parcel Station card shows: open shipments and booked pickups."""
    _require_permission()
    try:
        _get_credentials()
        configured = True
    except FedExNotConfiguredError:
        configured = False

    settings = get_pickup_settings()
    open_rows = open_fedex_shipments() if configured else []
    pickups = frappe.get_all(
        "FedEx Pickup",
        filters={"status": "Requested", "pickup_date": [">=", nowdate()]},
        fields=[
            "name",
            "pickup_date",
            "ready_time",
            "close_time",
            "confirmation_code",
            "package_count",
            "trigger",
            "creation",
        ],
        order_by="pickup_date asc, creation desc",
    )
    for pickup in pickups:
        pickup["ready_time"] = _to_time(pickup.get("ready_time"), settings.ready_time).strftime("%H:%M")
        pickup["close_time"] = _to_time(pickup.get("close_time"), settings.close_time).strftime("%H:%M")
        pickup["pickup_date"] = cstr(pickup.get("pickup_date"))[:10]
        pickup["shipments"] = frappe.get_all("Shipment", filters={"fedex_pickup": pickup["name"]}, pluck="name")

    window = pickup_window(settings, now_datetime())
    return {
        "configured": configured,
        "auto_enabled": settings.enabled,
        "open_shipments": [
            {"name": row["name"], "tracking_number": row.get("awb_number"), "created": cstr(row.get("creation"))[:16]}
            for row in open_rows
        ],
        "pickups": pickups,
        "next_window": {
            "pickup_date": window.pickup_date.isoformat(),
            "ready_time": window.ready_time.strftime("%H:%M"),
            "close_time": window.close_time.strftime("%H:%M"),
            "date_type": window.date_type,
        },
    }


@frappe.whitelist(methods=["POST"])
def request_fedex_pickup(shipments: Any = None) -> dict:
    """Book a pickup now for the given shipments, or for every open one."""
    _require_permission()
    return request_pickup(shipment_names=_parse_names(shipments), trigger="manual")


@frappe.whitelist(methods=["POST"])
def cancel_fedex_pickup(pickup: str | None = None, reason: str | None = None) -> dict:
    _require_permission()
    if not pickup:
        frappe.throw(_("pickup is required"))
    return cancel_pickup(pickup, cstr(reason))
