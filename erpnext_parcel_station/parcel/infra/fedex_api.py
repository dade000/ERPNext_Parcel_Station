"""FedEx Ship API integration (OAuth2 + REST/JSON).

Shape of the integration
------------------------
One POST to ``/ship/v1/shipments`` creates the parcel AND returns the label, so
this mirrors the Austrian Post ``ImportShipment`` flow rather than GLS's
create-then-fetch. Labels are requested as ZPLII so they feed the existing raw
ZPL CUPS print path unchanged.

Three things about this API drive the code below and are easy to get wrong:

1. **Auth is a bearer token with a one-hour life.** Unlike GLS (basic auth) and
   Austrian Post (none), every call needs a token fetched from ``/oauth/token``.
   It is cached in Redis keyed by mode+client key, because minting one per label
   burns the token endpoint's rate limit for no reason.

2. **Creating a shipment is NOT idempotent.** A retry after a timeout registers
   a SECOND parcel and bills for it. So the create call is never retried, and a
   Shipment that already carries a tracking number never calls out again — same
   ``SELECT ... FOR UPDATE`` guard the GLS and Austrian Post paths use.

3. **Errors are fail-fast and single.** FedEx returns ONE error per response
   with a machine-readable code (``PHONENUMBER.EMPTY``), not a list of
   everything wrong. The operator fixes one field, retries, meets the next one —
   which is why the payload builder validates what it can up front.

4. **Text must be ASCII.** The docs say so, and non-ASCII input comes back as
   garbage on the label (see ``_ascii_field``), so every text field is
   transliterated before it is sent.

Customs
-------
``customsClearanceDetail`` is sent on EVERY shipment, intra-EU included:
verified against the sandbox, FedEx evaluates the country pair itself and
answers AT->DE with ``requiredDocuments: ["AIR_WAYBILL"]`` — the data is
accepted without triggering a clearance. Electronic Trade Documents and the
commercial invoice are requested only when the shipment actually leaves the EU
customs union, because ETD without a declared document is rejected outright
(``SHIPPING.DOCUMENT.REQUIRED``).
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

from ..api.addresses import _country_code
from ..api.carrier import _clean_field, _parcel_dimensions, _resolve_label_weight
from ..customs import crosses_customs_border
from ..shipment_contents import collect_parcel_contents


class FedExAPIError(frappe.ValidationError):
    """Raised when FedEx rejects a request or the transport fails."""


class FedExNotConfiguredError(Exception):
    """Raised when FedEx Settings are absent/disabled.

    Distinct from ``FedExAPIError`` so the on_submit dispatcher can skip the
    carrier quietly on a site where FedEx simply isn't set up, instead of
    reporting a label failure.
    """


TOKEN_PATH = "/oauth/token"
SHIP_PATH = "/ship/v1/shipments"
PICKUP_PATH = "/pickup/v1/pickups"
PICKUP_CANCEL_PATH = "/pickup/v1/pickups/cancel"
CANCEL_PATH = "/ship/v1/shipments/cancel"
TRACK_PATH = "/track/v1/trackingnumbers"

#: FedEx caps a single track request at 30 tracking numbers.
TRACK_BATCH_SIZE = 30

#: Seconds shaved off the token's advertised lifetime before it is considered
#: stale, so a token never expires mid-request.
TOKEN_EXPIRY_SAFETY_MARGIN = 120

#: ERPNext UOM -> FedEx commodity quantity unit. FedEx rejects unknown codes, so
#: anything unmapped falls back to PCS rather than passing the raw UOM through.
_QUANTITY_UNITS_BY_UOM = {
    "nos": "PCS",
    "no": "PCS",
    "stk": "PCS",
    "stück": "PCS",
    "piece": "PCS",
    "pieces": "PCS",
    "unit": "PCS",
    "pair": "PRS",
    "paar": "PRS",
    "kg": "KG",
    "gram": "G",
    "g": "G",
    "meter": "M",
    "m": "M",
    "litre": "L",
    "liter": "L",
}
_DEFAULT_QUANTITY_UNIT = "PCS"

#: Smallest weight a customs commodity may declare. FedEx rejects a zero or
#: absent commodity weight outright; one gram is the floor for an item whose
#: master carries no weight and for which no share of the parcel weight is left
#: to distribute (all of it already accounted for by weighed items).
MIN_COMMODITY_WEIGHT_KG = 0.01

#: FedEx defaults when the Carrier Service master leaves them blank.
_DEFAULT_PACKAGING_TYPE = "YOUR_PACKAGING"

#: Carrier Service "Duties & Taxes Paid By" -> FedEx dutiesPayment.paymentType.
_DUTIES_PAYMENT_TYPES = {
    "Recipient (DAP)": "RECIPIENT",
    "Sender (DDP)": "SENDER",
}
_DEFAULT_DUTIES_PAYMENT = "RECIPIENT"


@dataclass(frozen=True)
class FedExCredentials:
    base_url: str
    client_key: str
    client_secret: str
    account_number: str
    mode: str
    timeout: int = 30
    label_image_type: str = "ZPLII"
    label_stock_type: str = "STOCK_4X6"
    label_resolution: int = 203
    request_auxiliary_label: bool = True
    pickup_type: str = "USE_SCHEDULED_PICKUP"
    default_service_type: str = "FEDEX_INTERNATIONAL_PRIORITY"
    shipment_purpose: str = "SOLD"
    terms_of_sale: str = "DAP"


def _get_credentials(for_tracking: bool = False) -> FedExCredentials:
    """Read FedEx Settings, or raise ``FedExNotConfiguredError``.

    Raises the *not configured* error (not an API error) for a disabled or
    incomplete configuration, so a site without FedEx skips the carrier instead
    of reporting failures.

    ``for_tracking`` selects the Track API key pair: the FedEx Developer
    Portal does not allow the Track API inside a Ship project — it must be its
    own project with its own credentials (same shipping account, same base
    URL). Calling the Track endpoint with the Ship project's key answers 403
    ``FORBIDDEN.ERROR`` despite a perfectly valid OAuth token.
    """
    settings = frappe.get_single("FedEx Settings")

    if not cint(settings.enable_fedex_integration):
        raise FedExNotConfiguredError(
            "FedEx integration is disabled (FedEx Settings -> Enable FedEx Integration)."
        )

    # Test and production are entirely separate at FedEx: different keys,
    # different shipping accounts, different host. Reading the set that matches
    # the selected mode means switching environments is one field, and going
    # live never overwrites the sandbox config still used for development.
    mode = (settings.api_mode or "Sandbox").strip()
    prefix = "production" if mode == "Production" else "sandbox"
    key_prefix = f"{prefix}_tracking" if for_tracking else prefix

    base_url = cstr(
        settings.production_base_url if mode == "Production" else settings.sandbox_base_url
    ).strip().rstrip("/")
    client_key = cstr(settings.get(f"{key_prefix}_client_key")).strip()
    client_secret = cstr(
        settings.get_password(f"{key_prefix}_client_secret", raise_exception=False)
    ).strip()
    # The Track project is tied to the same FedEx shipping account.
    account_number = cstr(settings.get(f"{prefix}_account_number")).strip()

    key_label = f"{mode} Tracking" if for_tracking else mode
    missing = [
        label
        for label, value in (
            ("Base URL", base_url),
            (f"{key_label} API Key", client_key),
            (f"{key_label} Secret Key", client_secret),
            (f"{mode} Account Number", account_number),
        )
        if not value
    ]
    if missing:
        raise FedExNotConfiguredError(
            f"FedEx Settings incomplete for {mode} mode: missing {', '.join(missing)}."
        )

    return FedExCredentials(
        base_url=base_url,
        client_key=client_key,
        client_secret=client_secret,
        account_number=account_number,
        mode=mode,
        timeout=cint(settings.api_timeout) or 30,
        label_image_type=cstr(settings.label_image_type).strip() or "ZPLII",
        label_stock_type=cstr(settings.label_stock_type).strip() or "STOCK_4X6",
        label_resolution=cint(settings.label_resolution) or 203,
        request_auxiliary_label=bool(cint(settings.request_auxiliary_label)),
        pickup_type=cstr(settings.pickup_type).strip() or "USE_SCHEDULED_PICKUP",
        default_service_type=cstr(settings.default_service_type).strip()
        or "FEDEX_INTERNATIONAL_PRIORITY",
        shipment_purpose=cstr(settings.shipment_purpose).strip() or "SOLD",
        terms_of_sale=cstr(settings.terms_of_sale).strip() or "DAP",
    )


def _error_message(response: requests.Response) -> str:
    """Turn a FedEx error response into one concise operator-facing line.

    FedEx answers with ``{"errors": [{"code", "message"}]}`` — normally exactly
    one entry. The code is worth keeping: it is stable and searchable
    (``PHONENUMBER.EMPTY``), while the message is the human half.
    """
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:250] or f"HTTP {response.status_code} {response.reason}"

    errors = payload.get("errors") or []
    parts = []
    for err in errors:
        code = (err.get("code") or "").strip()
        message = " ".join((err.get("message") or "").split())
        parts.append(f"{code}: {message}" if code and message else code or message)
    detail = " | ".join(p for p in parts if p)
    if not detail:
        detail = f"HTTP {response.status_code} {response.reason}"
    return detail[:250]


class FedExAPIClient:
    """Thin transport for the two Ship endpoints we use."""

    def __init__(self, credentials: FedExCredentials):
        self.creds = credentials
        self.session = requests.Session()

    @classmethod
    def from_settings(cls) -> "FedExAPIClient":
        return cls(_get_credentials())

    @classmethod
    def from_tracking_settings(cls) -> "FedExAPIClient":
        """Client using the Track API project's own key pair (see
        ``_get_credentials``). The token cache is keyed by client key, so ship
        and tracking tokens never mix."""
        return cls(_get_credentials(for_tracking=True))

    # -- auth ---------------------------------------------------------------

    @property
    def _token_cache_key(self) -> str:
        # Keyed by mode AND client key so switching Sandbox/Production, or
        # rotating the key, can never serve a token minted for the other one.
        return f"fedex_access_token::{self.creds.mode}::{self.creds.client_key}"

    def _access_token(self) -> str:
        cached = frappe.cache().get_value(self._token_cache_key)
        if cached:
            return cstr(cached)

        try:
            response = self.session.post(
                f"{self.creds.base_url}{TOKEN_PATH}",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.creds.client_key,
                    "client_secret": self.creds.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.creds.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise FedExAPIError(_("Could not reach FedEx to authenticate: {0}").format(exc)) from exc

        if response.status_code != 200:
            raise FedExAPIError(
                _("FedEx authentication failed: {0}").format(_error_message(response))
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise FedExAPIError(_("FedEx returned no access token."))

        ttl = cint(payload.get("expires_in")) - TOKEN_EXPIRY_SAFETY_MARGIN
        if ttl > 0:
            frappe.cache().set_value(self._token_cache_key, token, expires_in_sec=ttl)
        return token

    def invalidate_token(self) -> None:
        frappe.cache().delete_value(self._token_cache_key)

    # -- requests -----------------------------------------------------------

    def _request(self, method: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Call FedEx once. Deliberately NOT retried — see module docstring."""
        url = f"{self.creds.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "X-locale": "en_US",
        }

        try:
            response = self.session.request(
                method,
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=self.creds.timeout,
            )
        except requests.exceptions.RequestException as exc:
            # A timeout is NOT proof the parcel was not created. Say so, so
            # nobody "just tries again" and books a second parcel.
            raise FedExAPIError(
                _(
                    "FedEx did not answer ({0}). The shipment may still have been "
                    "created — check the FedEx portal before retrying."
                ).format(exc)
            ) from exc

        if response.status_code == 401:
            # Token rejected (rotated key, or a cached token FedEx no longer
            # honours). Drop it so the next attempt mints a fresh one.
            self.invalidate_token()
            raise FedExAPIError(_("FedEx rejected the access token: {0}").format(_error_message(response)))

        if response.status_code >= 400:
            raise FedExAPIError(_error_message(response))

        try:
            return response.json()
        except ValueError as exc:
            raise FedExAPIError(_("FedEx returned a non-JSON response.")) from exc

    def create_shipment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", SHIP_PATH, payload)

    # Pickup Request API — same OAuth project as Ship. Neither call is retried:
    # a create that timed out may still have booked a courier.
    def create_pickup(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", PICKUP_PATH, payload)

    def cancel_pickup(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", PICKUP_CANCEL_PATH, payload)

    def cancel_shipment(self, tracking_number: str, sender_country_code: str | None = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "accountNumber": {"value": self.creds.account_number},
            "trackingNumber": tracking_number,
            "deletionControl": "DELETE_ALL_PACKAGES",
        }
        if sender_country_code:
            payload["senderCountryCode"] = sender_country_code
        return self._request("PUT", CANCEL_PATH, payload)

    def track_shipments(self, tracking_numbers: list[str]) -> Dict[str, Any]:
        """Track up to ``TRACK_BATCH_SIZE`` numbers in one call.

        Read-only and therefore safe to repeat — unlike shipment creation
        there is no double-booking risk. Uses the same OAuth credentials and
        base URL as label creation; callers chunk to the batch limit
        (``parcel/tracking/fedex_track.py``).
        """
        if len(tracking_numbers) > TRACK_BATCH_SIZE:
            raise ValueError(f"FedEx tracks at most {TRACK_BATCH_SIZE} numbers per request.")
        payload = {
            "includeDetailedScans": True,
            "trackingInfo": [
                {"trackingNumberInfo": {"trackingNumber": number}} for number in tracking_numbers
            ],
        }
        return self._request("POST", TRACK_PATH, payload)


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------


# FedEx Ship API docs, "Create Shipment": "In order to avoid entering bad data
# on the label and in to the FedEx systems, do not feed non ASCII characters in
# to your Create Shipment request." They mean it: umlauts in the request come
# back on the ZPL label as Latin-1 bytes under a ^CI13 (code page 850)
# declaration, so the printer renders "Kässpätzle" as "Kõsspõtzle", and the
# 2D barcode drops them entirely. Every text field is therefore transliterated
# to plain ASCII before it is sent. German umlauts use the ae/oe/ue/ss
# convention (Müller -> Mueller), everything else loses its diacritics, and
# whatever has no ASCII form is dropped.
_UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "ẞ": "SS", "æ": "ae", "œ": "oe", "ø": "oe"}
_SIMPLE_TRANSLITERATIONS = str.maketrans(
    {
        "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
        "€": "EUR", "–": "-", "—": "-", "‘": "'", "’": "'", "‚": "'",
        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    }
)


def _transliterate_umlaut(text: str, index: int) -> str:
    """Return the ASCII form of the umlaut at ``index``, matching the case of
    its surroundings: "Müller" -> "Mueller" but "MÜLLER" -> "MUELLER"."""
    ch = text[index]
    replacement = _UMLAUTS[ch.lower()]
    if not ch.isupper():
        return replacement
    neighbours = text[index - 1 : index] + text[index + 1 : index + 2]
    if any(c.isalpha() and c.islower() for c in neighbours):
        return replacement.capitalize()
    return replacement.upper()


def _ascii_field(value: Any) -> str:
    """Clean a text value (see ``_clean_field``) and reduce it to ASCII.

    Applied BEFORE any length cap, because "ü" -> "ue" makes the text longer.
    """
    text = _clean_field(value)
    if text.isascii():
        return text
    text = "".join(
        _transliterate_umlaut(text, i) if ch.lower() in _UMLAUTS else ch
        for i, ch in enumerate(text)
    )
    text = text.translate(_SIMPLE_TRANSLITERATIONS)
    # NFKD splits "é" into "e" + combining accent; dropping the accent leaves "e".
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


def _address_doc(address_name: str | None) -> Optional[Document]:
    if not address_name or not frappe.db.exists("Address", address_name):
        return None
    return frappe.get_doc("Address", address_name)


def _street_lines(address: Document) -> list[str]:
    """FedEx accepts up to 3 street lines, 35 characters each."""
    lines = [
        _ascii_field(address.get("address_line1"))[:35],
        _ascii_field(address.get("address_line2"))[:35],
    ]
    return [line for line in lines if line][:3]


def _fedex_address(address: Document, residential: bool | None = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "streetLines": _street_lines(address),
        "city": _ascii_field(address.get("city"))[:35],
        "postalCode": _ascii_field(address.get("pincode"))[:10],
        "countryCode": _country_code(address.get("country")),
    }
    # State is mandatory for US/CA/PR and meaningless elsewhere; send it only
    # when we actually have one.
    state = _ascii_field(address.get("state"))
    if state:
        payload["stateOrProvinceCode"] = state
    if residential is not None:
        payload["residential"] = bool(residential)
    return payload


def _phone_from_contact(contact_name: str | None) -> str:
    if not contact_name or not frappe.db.exists("Contact", contact_name):
        return ""
    contact = frappe.db.get_value(
        "Contact", contact_name, ["mobile_no", "phone"], as_dict=True
    ) or {}
    return _ascii_field(contact.get("mobile_no") or contact.get("phone"))


def _fedex_contact(
    *,
    person_name: str,
    company_name: str,
    address: Document,
    contact_name: str | None,
    email: str | None,
    role: str,
) -> Dict[str, Any]:
    """Build a FedEx contact block.

    FedEx rejects a shipment outright with ``PHONENUMBER.EMPTY`` when either
    party has no phone number — unlike GLS and Austrian Post, which accept the
    shipment without one. Rather than inventing a number (FedEx calls it for
    delivery problems, so a wrong one is worse than none) we fail with a message
    that names the record to fix.
    """
    phone = _ascii_field(address.get("phone")) or _phone_from_contact(contact_name)
    if not phone:
        frappe.throw(
            _(
                "FedEx requires a {0} phone number. Add one to address "
                "<a href='/app/address/{1}'>{1}</a> (or its contact) and try again."
            ).format(role, address.name),
            title=_("Phone number missing"),
            exc=FedExAPIError,
        )

    contact: Dict[str, Any] = {"phoneNumber": phone[:15]}
    person = _ascii_field(person_name)[:35]
    company = _ascii_field(company_name)[:35]
    # FedEx needs at least one of personName / companyName.
    if person:
        contact["personName"] = person
    if company:
        contact["companyName"] = company
    if not person and not company:
        contact["personName"] = _ascii_field(_("Recipient") if role == "recipient" else _("Shipper"))

    mail = _ascii_field(email or address.get("email_id"))[:80]
    if mail:
        contact["emailAddress"] = mail
    return contact


def _resolve_duties_payment(service: Optional[Document], destination_country: str | None) -> str:
    """Who pays duty and tax: us (SENDER/DDP) or the recipient (RECIPIENT/DAP).

    Read straight off the Carrier Service, because a Carrier Service in this
    system covers exactly one destination country ("Paket Plus Schweiz",
    "Paket Plus Deutschland"). The service therefore already encodes the
    destination, and a service that pre-collects import charges is required to
    be set to "Sender (DDP)" — enforced when the Carrier Service is saved, so
    a customer can never be charged at checkout and billed again at the border.
    """
    return _DUTIES_PAYMENT_TYPES.get(
        (service.get("duties_payment") if service else None) or "", _DEFAULT_DUTIES_PAYMENT
    )


def _quantity_units(uom: str | None) -> str:
    return _QUANTITY_UNITS_BY_UOM.get((uom or "").strip().lower(), _DEFAULT_QUANTITY_UNIT)


def _carrier_service_doc(doc: Document) -> Optional[Document]:
    link = doc.get("carrier_service") or doc.get("custom_carrier_service")
    if link and frappe.db.exists("Carrier Service", link):
        return frappe.get_doc("Carrier Service", link)
    return None


def _weight_shares(parcel_weight: float, weighed: list[float], count: int) -> list[float]:
    """Split what is left of the parcel weight over ``count`` items without a
    master weight, so that all commodity weights add up to the package weight
    EXACTLY as it is sent (two decimals).

    Counted in hundredths of a kilogram, not divided as a float: 3.47 kg over
    four items is 0.8675 each, which rounds to 0.87 and adds up to 3.48 — one
    hundredth more than the package, and FedEx rejects the whole shipment with
    COMMODITYWEIGHT.GREATERTHAN.PACKAGEWEIGHT. The odd hundredths go to the
    first items instead.

    Returns an empty list when there is nothing to distribute (no such items,
    or the weighed ones already account for the parcel).
    """
    if count <= 0:
        return []
    package = int(round(round(flt(parcel_weight), 2) * 100))
    floor = int(round(MIN_COMMODITY_WEIGHT_KG * 100))
    taken = sum(max(int(round(round(weight, 2) * 100)), floor) for weight in weighed)
    left = package - taken
    if left < count * floor:
        return []
    base, extra = divmod(left, count)
    return [(base + (1 if index < extra else 0)) / 100 for index in range(count)]


def _commodities(
    sh: dict, creds: FedExCredentials, *, customs_destination: bool = False, parcel_weight: float = 0.0
) -> tuple[list[dict], float, str]:
    """Return (commodities, total customs value, currency).

    Falls back to a single line built from the Shipment's own
    description/value when there are neither scanned rows nor a Delivery Note,
    because ``customsClearanceDetail`` requires at least one commodity.

    ``customs_destination`` tightens the rules: a parcel actually crossing a
    customs border must declare each commodity's country of manufacture and
    weight, and FedEx rejects it otherwise (COMMODITIES.COUNTRYOFMANUFACTURE.
    REQUIRED, WEIGHT.NONNUMERIC.ERROR, PRODUCT.INFO.REQUIRED). Intra-EU parcels
    carry the same block without those constraints, so nothing changes there.
    """
    contents = collect_parcel_contents(sh)
    currency = cstr(sh.get("currency")) or "EUR"
    commodities: list[dict] = []
    total_value = 0.0

    # A customs declaration must account for the parcel's weight. Items whose
    # master carries no weight get an even share of whatever the scale (or the
    # fallback) says the parcel weighs — a distribution of a measured total, not
    # an invented number, so the declared total stays truthful.
    unweighed = [row for row in contents if not flt(row.get("weight_per_unit"))]
    weighed = [
        flt(row.get("weight_per_unit")) * (flt(row.get("qty")) or 1.0)
        for row in contents
        if flt(row.get("weight_per_unit"))
    ]
    shares = _weight_shares(parcel_weight, weighed, len(unweighed)) if customs_destination else []

    missing_origin: list[str] = []
    for row in contents:
        qty = flt(row.get("qty")) or 1.0
        rate = flt(row.get("rate"))
        line_value = rate * qty
        total_value += line_value
        if row.get("currency"):
            currency = cstr(row["currency"])

        commodity: Dict[str, Any] = {
            "description": _ascii_field(row.get("item_name") or row.get("item_code") or "Goods")[:450],
            "quantity": int(qty) if float(qty).is_integer() else qty,
            "quantityUnits": _quantity_units(row.get("uom")),
            "unitPrice": {"amount": rate, "currency": currency},
            "customsValue": {"amount": line_value, "currency": currency},
        }
        if row.get("item_code"):
            commodity["partNumber"] = _ascii_field(row["item_code"])[:50]
        if row.get("origin_country"):
            commodity["countryOfManufacture"] = _country_code(row["origin_country"])
        elif customs_destination:
            missing_origin.append(row.get("item_code") or row.get("item_name") or "?")
        if row.get("tariff"):
            commodity["harmonizedCode"] = _ascii_field(row["tariff"])
        # Commodity weight unit MUST match the package weight unit or FedEx
        # rejects the shipment; both are KG here.
        net_weight = flt(row.get("weight_per_unit")) * qty
        if not net_weight and customs_destination:
            # No master weight: take a share of the measured parcel weight, or
            # the floor when the weighed items already account for all of it.
            net_weight = shares.pop(0) if shares else MIN_COMMODITY_WEIGHT_KG
        if net_weight:
            commodity["weight"] = {
                "units": "KG",
                "value": max(round(net_weight, 2), MIN_COMMODITY_WEIGHT_KG),
            }
        commodities.append(commodity)

    if missing_origin:
        # Country of origin is a legal declaration, so it is never guessed — the
        # operator is told exactly which Item masters to complete instead of
        # being handed FedEx's error code.
        frappe.throw(
            _(
                "FedEx needs a country of origin for every item on a customs shipment. "
                "Set 'Country of Origin' on these Items and try again: {0}"
            ).format(", ".join(sorted(set(missing_origin)))),
            title=_("Customs data missing"),
            exc=FedExAPIError,
        )

    if not commodities:
        value = flt(sh.get("value_of_goods"))
        total_value = value
        commodities = [
            {
                "description": _ascii_field(sh.get("description_of_content") or "Goods")[:450],
                "quantity": 1,
                "quantityUnits": _DEFAULT_QUANTITY_UNIT,
                "unitPrice": {"amount": value, "currency": currency},
                "customsValue": {"amount": value, "currency": currency},
            }
        ]

    return commodities, round(total_value, 2), currency


def _build_payload(doc: Document, creds: FedExCredentials) -> Dict[str, Any]:
    """Assemble the Create Shipment request body for one Shipment."""
    sh = doc.as_dict()

    pickup_address = _address_doc(doc.get("pickup_address_name"))
    if not pickup_address:
        frappe.throw(
            _("Shipment {0} has no pickup address; FedEx needs a shipper address.").format(doc.name),
            exc=FedExAPIError,
        )

    delivery_address = _address_doc(
        doc.get("delivery_address_name")
        or doc.get("shipping_address_name")
        or doc.get("customer_address")
    )
    if not delivery_address:
        frappe.throw(
            _("Shipment {0} has no delivery address.").format(doc.name), exc=FedExAPIError
        )

    service = _carrier_service_doc(doc)
    service_type = (
        _ascii_field(service.get("service_code")) if service else ""
    ) or creds.default_service_type
    packaging_type = (
        _ascii_field(service.get("packaging_type")) if service else ""
    ) or _DEFAULT_PACKAGING_TYPE
    duties_payment_type = _resolve_duties_payment(service, delivery_address.get("country"))

    weight_kg = _resolve_label_weight(sh)
    dimensions = _parcel_dimensions(sh)

    package: Dict[str, Any] = {"weight": {"units": "KG", "value": round(weight_kg, 2)}}
    # FedEx wants all three dimensions or none, as integers.
    if all(dimensions.get(k) for k in ("length", "width", "height")):
        package["dimensions"] = {
            "length": dimensions["length"],
            "width": dimensions["width"],
            "height": dimensions["height"],
            "units": "CM",
        }

    origin_country = _country_code(pickup_address.get("country"))
    destination_country = _country_code(delivery_address.get("country"))
    customs_destination = crosses_customs_border(origin_country, destination_country)
    commodities, total_customs_value, currency = _commodities(
        sh, creds, customs_destination=customs_destination, parcel_weight=weight_kg
    )

    label_specification: Dict[str, Any] = {
        "imageType": creds.label_image_type,
        "labelStockType": creds.label_stock_type,
    }
    # 203 dpi is FedEx's default, so the field is only sent when it differs.
    # The printer MUST run at the same resolution: ZPL coordinates are in dots,
    # so a 300 dpi label (^PW1200) fed to a 203 dpi printer comes out ~1.5x
    # oversized and clipped.
    if creds.label_resolution and creds.label_resolution != 203:
        label_specification["resolution"] = creds.label_resolution

    # International shipments get the auxiliary/secondary Air Waybill label that
    # FedEx requires alongside the main label — and that its label-evaluation
    # team requires in the approval submission. It arrives as a SECOND ^XA block
    # inside the same ZPL stream, so the raw print path prints both without any
    # extra handling. Domestic shipments neither need nor get it.
    if creds.request_auxiliary_label and origin_country != destination_country:
        label_specification["customerSpecifiedDetail"] = {
            "additionalLabels": [{"type": "CONSIGNEE", "count": 1}]
        }

    requested_shipment: Dict[str, Any] = {
        "shipper": {
            "address": _fedex_address(pickup_address),
            "contact": _fedex_contact(
                person_name=doc.get("pickup") or "",
                company_name=doc.get("pickup_company") or "",
                address=pickup_address,
                contact_name=doc.get("pickup_contact_person"),
                email=doc.get("pickup_contact_email"),
                role="shipper",
            ),
        },
        "recipients": [
            {
                "address": _fedex_address(
                    delivery_address,
                    residential=not bool(doc.get("delivery_company")),
                ),
                "contact": _fedex_contact(
                    person_name=doc.get("delivery_to") or doc.get("delivery_customer") or "",
                    company_name=doc.get("delivery_company") or "",
                    address=delivery_address,
                    contact_name=doc.get("delivery_contact_name"),
                    email=doc.get("delivery_contact_email"),
                    role="recipient",
                ),
            }
        ],
        "serviceType": service_type,
        "packagingType": packaging_type,
        "pickupType": creds.pickup_type,
        "blockInsightVisibility": False,
        "shippingChargesPayment": {"paymentType": "SENDER"},
        "labelSpecification": label_specification,
        "totalWeight": round(weight_kg, 2),
        "requestedPackageLineItems": [package],
        # Sent for every destination: FedEx evaluates the country pair itself and
        # does not start a clearance for intra-EU parcels (verified: AT->DE asks
        # only for an AIR_WAYBILL).
        "customsClearanceDetail": {
            "dutiesPayment": {"paymentType": duties_payment_type},
            "commodities": commodities,
            "totalCustomsValue": {"amount": total_customs_value, "currency": currency},
            "commercialInvoice": {
                "shipmentPurpose": creds.shipment_purpose,
                "termsOfSale": creds.terms_of_sale,
            },
        },
    }

    if customs_destination:
        # Electronic Trade Documents: FedEx generates the commercial invoice and
        # transmits it electronically, so nothing has to be printed and taped to
        # the parcel. Declaring the document is mandatory — ETD on its own is
        # rejected with SHIPPING.DOCUMENT.REQUIRED.
        requested_shipment["shipmentSpecialServices"] = {
            "specialServiceTypes": ["ELECTRONIC_TRADE_DOCUMENTS"],
            "etdDetail": {"requestedDocumentTypes": ["COMMERCIAL_INVOICE"]},
        }
        requested_shipment["shippingDocumentSpecification"] = {
            "shippingDocumentTypes": ["COMMERCIAL_INVOICE"],
            "commercialInvoiceDetail": {
                "documentFormat": {"stockType": "PAPER_LETTER", "docType": "PDF"}
            },
        }

    return {
        "labelResponseOptions": "LABEL",
        "accountNumber": {"value": creds.account_number},
        "requestedShipment": requested_shipment,
    }


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


def _extract_label_and_tracking(
    response: Dict[str, Any], image_type: str = "ZPLII"
) -> tuple[str, bytes, Optional[bytes]]:
    """Return (tracking number, label bytes, commercial invoice PDF or None)."""
    output = response.get("output") or {}
    shipments = output.get("transactionShipments") or []
    if not shipments:
        raise FedExAPIError(_("FedEx returned no shipment in the response."))

    shipment = shipments[0]
    tracking = cstr(shipment.get("masterTrackingNumber")).strip()
    if not tracking:
        raise FedExAPIError(_("FedEx returned no tracking number."))

    # Collect EVERY label document, not just the first. A multi-piece shipment
    # returns one per package, and taking only the first would silently drop the
    # labels for the remaining parcels.
    encoded_labels: list[str] = []
    for piece in shipment.get("pieceResponses") or []:
        for document in piece.get("packageDocuments") or []:
            if document.get("encodedLabel"):
                encoded_labels.append(document["encodedLabel"])

    if not encoded_labels:
        raise FedExAPIError(
            _("FedEx returned tracking number {0} but no label data.").format(tracking)
        )

    try:
        decoded = [base64.b64decode(chunk, validate=True) for chunk in encoded_labels]
    except Exception as exc:
        raise FedExAPIError(_("FedEx label data could not be decoded.")) from exc

    if len(decoded) == 1:
        label_bytes = decoded[0]
    elif (image_type or "").upper() == "ZPLII":
        # ZPL streams concatenate: each ^XA…^XZ block is its own label, so the
        # printer emits one physical label per package from a single job.
        label_bytes = b"".join(decoded)
    else:
        # PDF/PNG cannot simply be concatenated. Keep the first and say so
        # rather than writing a corrupt file.
        label_bytes = decoded[0]
        frappe.log_error(
            title=f"FedEx returned {len(decoded)} {image_type} labels",
            message=(
                f"Tracking {tracking}: only the first label was attached because "
                f"{image_type} documents cannot be concatenated. Use ZPLII for "
                f"multi-piece shipments."
            ),
        )

    # The commercial invoice comes back even though ETD already transmitted it.
    # Keep it as an archive copy; it is deliberately NOT named like the customs
    # papers that the A4 print button picks up, because nothing needs printing.
    invoice_bytes = None
    for document in shipment.get("shipmentDocuments") or []:
        if document.get("contentType") == "COMMERCIAL_INVOICE" and document.get("encodedLabel"):
            try:
                invoice_bytes = base64.b64decode(document["encodedLabel"], validate=True)
            except Exception:
                frappe.log_error(
                    title="FedEx commercial invoice undecodable",
                    message=frappe.get_traceback(),
                )
            break

    return tracking, label_bytes, invoice_bytes


def _label_extension(image_type: str) -> str:
    return "pdf" if (image_type or "").upper() == "PDF" else "zpl"


def _attach(doc: Document, filename: str, content: bytes) -> str:
    """Attach (or replace) a private file on the Shipment, returning its URL."""
    existing = frappe.db.exists(
        "File",
        {
            "attached_to_doctype": doc.doctype,
            "attached_to_name": doc.name,
            "file_name": filename,
        },
    )
    file_doc = frappe.get_doc("File", existing) if existing else frappe.new_doc("File")
    file_doc.file_name = filename
    file_doc.attached_to_doctype = doc.doctype
    file_doc.attached_to_name = doc.name
    file_doc.is_private = 1
    file_doc.content = content
    file_doc.save(ignore_permissions=True)
    return file_doc.file_url


def _get_shipment_doc(doc_or_name: Any) -> Document:
    if isinstance(doc_or_name, str):
        return frappe.get_doc("Shipment", doc_or_name)
    return doc_or_name


def _credentials_for_format(creds: FedExCredentials, label_format: Optional[str]) -> FedExCredentials:
    """Label settings for an explicitly requested format.

    ``None`` keeps FedEx Settings as they are (labels created without a
    station). ``"pdf"`` asks for the label on a page of its own size — FedEx
    returns 4x6 inch pages for STOCK_4X6, a letter sheet for PAPER_4X6 — and
    ``"zpl"`` forces the printer format even when the settings say PDF.
    Verified against the sandbox.
    """
    if not label_format:
        return creds
    if label_format == "pdf":
        return replace(creds, label_image_type="PDF", label_stock_type="STOCK_4X6", label_resolution=203)
    if (creds.label_image_type or "").upper() != "ZPLII":
        return replace(creds, label_image_type="ZPLII", label_stock_type="STOCK_4X6")
    return creds


def create_label(doc_or_name: Any, label_format: Optional[str] = None) -> Dict[str, Any]:
    """Create the FedEx parcel and attach its label to the Shipment.

    Returns ``{tracking_number, file_url, invoice_file_url, existing}``.
    """
    doc = _get_shipment_doc(doc_or_name)
    creds = _credentials_for_format(_get_credentials(), label_format)

    # Idempotency + concurrency guard, identical in intent to the GLS and
    # Austrian Post paths: FedEx charges for every created parcel, so a second
    # call for the same Shipment must never reach the API. The row lock
    # serialises concurrent triggers (on_submit hook AND the label button).
    existing_tracking = cstr(
        frappe.db.get_value("Shipment", doc.name, "awb_number", for_update=True)
    ).strip()
    if existing_tracking:
        frappe.logger().info(
            f"[fedex] Shipment {doc.name} already has tracking {existing_tracking}; "
            f"skipping FedEx createShipment."
        )
        file_url = frappe.db.get_value(
            "File",
            {
                "attached_to_doctype": doc.doctype,
                "attached_to_name": doc.name,
                "file_name": ("like", "%-FedEx-Label.%"),
            },
            "file_url",
            order_by="creation desc",
        )
        return {"tracking_number": existing_tracking, "file_url": file_url, "existing": True}

    payload = _build_payload(doc, creds)
    client = FedExAPIClient(creds)
    response = client.create_shipment(payload)
    tracking, label_bytes, invoice_bytes = _extract_label_and_tracking(
        response, creds.label_image_type
    )

    filename = f"{doc.name}-FedEx-Label.{_label_extension(creds.label_image_type)}"
    file_url = _attach(doc, filename, label_bytes)

    invoice_url = None
    if invoice_bytes:
        invoice_url = _attach(doc, f"{doc.name}-FedEx-Commercial-Invoice.pdf", invoice_bytes)

    frappe.db.set_value("Shipment", doc.name, "awb_number", tracking, update_modified=False)
    doc.awb_number = tracking

    try:
        doc.add_comment("Info", _("FedEx label created. Tracking: {0}").format(tracking))
    except Exception:
        frappe.logger().warning(
            f"[fedex] failed to add label-created activity entry for {doc.name}", exc_info=True
        )

    frappe.logger().info(
        f"[fedex] Shipment {doc.name}: tracking={tracking} label={len(label_bytes)}B "
        f"invoice={'yes' if invoice_bytes else 'no'}"
    )
    return {
        "tracking_number": tracking,
        "file_url": file_url,
        "invoice_file_url": invoice_url,
        "existing": False,
    }


def cancel_label(doc_or_name: Any) -> Dict[str, Any]:
    """Cancel the FedEx parcel for a Shipment. Returns the FedEx output block."""
    doc = _get_shipment_doc(doc_or_name)
    tracking = cstr(doc.get("awb_number")).strip()
    if not tracking:
        return {}

    creds = _get_credentials()
    pickup_address = _address_doc(doc.get("pickup_address_name"))
    sender_country = _country_code(pickup_address.get("country")) if pickup_address else None

    response = FedExAPIClient(creds).cancel_shipment(tracking, sender_country)
    return response.get("output") or {}


@frappe.whitelist()
def preview_fedex_payload(shipment: str) -> Dict[str, Any]:
    """Return the exact JSON body that would be POSTed for ``shipment``.

    Read-only — never calls FedEx. Mirrors ``gls_api.preview_gls_payload`` so the
    parcel station can log the outgoing payload while debugging.
    """
    doc = _get_shipment_doc(shipment)
    creds = _get_credentials()
    payload = _build_payload(doc, creds)
    # The account number identifies the billing account; keep it out of debug logs.
    redacted = json.loads(json.dumps(payload))
    redacted["accountNumber"] = {"value": "***"}
    return {"shipment": doc.name, "mode": creds.mode, "payload": redacted}


@frappe.whitelist()
def cancel_fedex_label_on_cancel(doc, method=None):
    """On Shipment cancel, cancel the parcel at FedEx too, and record the audit
    trail on the Activity/Timeline.

    Non-blocking, matching the GLS and Austrian Post handlers: the local ERPNext
    cancellation always proceeds and the FedEx outcome is recorded, so a carrier
    outage cannot leave a Shipment stuck in submitted state. FedEx only accepts a
    cancellation before the parcel is tendered; afterwards it answers with an
    error, which is logged for manual reconciliation.
    """
    from ..carrier_routing import FEDEX, resolve_carrier

    if getattr(doc, "doctype", None) != "Shipment":
        return

    if resolve_carrier(doc) != FEDEX:
        return

    tracking = cstr(getattr(doc, "awb_number", None)).strip()
    if not tracking:
        frappe.logger().info(
            f"[fedex-cancel] Shipment={doc.name}: no tracking number; nothing to cancel."
        )
        return

    def _activity(text: str) -> None:
        try:
            doc.add_comment("Info", text)
        except Exception:
            frappe.logger().warning(
                f"[fedex-cancel] failed to add activity entry for {doc.name}", exc_info=True
            )

    try:
        creds = _get_credentials()
    except FedExNotConfiguredError as exc:
        frappe.log_error(
            title="FedEx cancellation skipped (config missing)",
            message=f"Shipment {doc.name} (tracking {tracking}): {exc}",
        )
        _activity(
            _(
                "FedEx cancellation skipped — FedEx is not configured. Parcel {0} "
                "must be cancelled manually in the FedEx portal."
            ).format(tracking)
        )
        return

    _activity(_("FedEx cancellation request sent (tracking {0})").format(tracking))

    try:
        pickup_address = _address_doc(doc.get("pickup_address_name"))
        sender_country = _country_code(pickup_address.get("country")) if pickup_address else None
        output = FedExAPIClient(creds).cancel_shipment(tracking, sender_country) or {}
        result = (output.get("output") or output) if isinstance(output, dict) else {}
    except Exception as exc:
        frappe.log_error(
            title="FedEx cancellation failed",
            message=f"Shipment {doc.name} (tracking {tracking}): {exc}\n{frappe.get_traceback()}",
        )
        _activity(_("FedEx cancellation failed. Response: {0}").format(str(exc)[:250]))
        frappe.msgprint(
            _(
                "Parcel {0} was NOT cancelled at FedEx ({1}). The ERPNext cancellation "
                "proceeds; cancel the parcel manually in the FedEx portal."
            ).format(tracking, str(exc)[:150]),
            title=_("FedEx cancellation failed"),
            indicator="orange",
        )
        return

    message = cstr(result.get("message")) or _("Shipment cancelled at FedEx.")
    _activity(_("FedEx cancellation succeeded. Response: {0}").format(message))
    frappe.logger().info(f"[fedex-cancel] Shipment={doc.name} tracking={tracking}: {message}")
