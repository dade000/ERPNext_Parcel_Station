import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import frappe
import requests
from frappe import _
from frappe import whitelist
from frappe.model.document import Document
from frappe.contacts.doctype.address.address import get_company_address, get_address_display
from frappe.utils import cint, cstr, flt


import re as _re

from ..customs import EU_COUNTRY_CODES


def _clean_carrier_message(msg: Any) -> str:
    """Collapse a carrier message to a single readable line: strip HTML/markup,
    collapse whitespace, and cap the length so the operator gets a concise toast
    rather than a wall of text."""
    text = _re.sub(r"<[^>]*>", " ", str(msg or ""))
    text = _re.sub(r"\s+", " ", text).strip()
    if len(text) > 250:
        text = text[:247].rstrip() + "..."
    return text


# Generic GLS summary/boilerplate sentences that add no diagnostic value — they
# get appended alongside the real validation message ("Invalid field … is not a
# valid value.") and only clutter the operator's toast. Dropped from the
# concise message; the specific rule message is kept.
_GLS_GENERIC_SENTENCE = _re.compile(
    r"^(shipment validation failed|request validation failed|validation failed|"
    r"failed to create gls label.*)$",
    _re.IGNORECASE,
)


def _gls_concise(msg: str) -> str:
    """Reduce a GLS message to a single, non-duplicated line: drop generic
    summary sentences ("Shipment validation failed") and repeated sentences,
    keeping the specific validation detail in its original order."""
    msg = _clean_carrier_message(msg)
    if not msg:
        return msg
    # Split on sentence boundaries (". " — note "consignee.zip" has no trailing
    # space so it is NOT split). Keep order, drop generic + duplicate sentences.
    seen = set()
    kept = []
    for part in _re.split(r"(?<=\.)\s+", msg):
        part = part.strip()
        if not part:
            continue
        if _GLS_GENERIC_SENTENCE.match(part.rstrip(".")):
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(part)
    return " ".join(kept).strip() or msg


def _first_message_in_list(items: Any) -> str:
    """Return the first usable human message from a list of validation entries
    (strings or dicts with a message-like field)."""
    if not isinstance(items, (list, tuple)):
        return ""
    for item in items:
        if isinstance(item, str) and item.strip():
            return item
        if isinstance(item, dict):
            for key in ("messageText", "message", "text", "detail", "description", "errorMessage"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    return val
    return ""


def _extract_gls_user_message(response) -> str:
    """Extract ONE concise, user-friendly message from a GLS error response.

    GLS Shipit returns the validation failure either in custom response
    *headers* (``message`` / ``X-Message`` / ``error``) or in a JSON body (a
    ``messageList`` / ``errors`` array, or a flat ``message`` field). We surface
    a single line for the operator; the full request/response is still written
    to the Error Log by the caller. Falls back to the HTTP status when nothing
    more specific is available — never the raw JSON dump.
    """
    # 1. Header message — GLS Shipit puts the validation text here.
    try:
        hdrs = response.headers or {}
        hdr_msg = (
            hdrs.get("message")
            or hdrs.get("X-Message")
            or hdrs.get("error")
            or hdrs.get("X-Error")
        )
        if hdr_msg:
            return _clean_carrier_message(hdr_msg)
    except Exception:
        pass

    # 2. JSON body — pull the first usable message out of the common shapes.
    body = None
    try:
        body = response.json()
    except Exception:
        body = None

    if isinstance(body, dict):
        for key in ("messageText", "message", "exitMessage", "detail", "description", "error"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return _clean_carrier_message(val)
        for key in ("messageList", "messages", "errors", "validationErrors", "ruleViolations"):
            msg = _first_message_in_list(body.get(key))
            if msg:
                return _clean_carrier_message(msg)
    elif isinstance(body, list):
        msg = _first_message_in_list(body)
        if msg:
            return _clean_carrier_message(msg)

    # 3. Plain-text (non-JSON) body.
    try:
        text = (response.text or "").strip()
        if text and text[:1] not in ("{", "["):
            return _clean_carrier_message(text)
    except Exception:
        pass

    # 4. Last resort: HTTP status — concise, never a raw payload dump.
    try:
        return f"GLS request failed (HTTP {response.status_code} {response.reason})."
    except Exception:
        return "GLS request failed."


class GLSAPIError(frappe.ValidationError):
    """Raised when GLS API communication fails."""


class GLSNotConfiguredError(Exception):
    """Raised when a required GLS Settings value (e.g. the Cancel Endpoint) is
    not configured. Distinct from ``GLSAPIError`` so callers can skip the
    operation gracefully instead of treating it as an API failure."""


DEFAULT_PARCELSHOP_SEARCH_URL = "https://shipit-wbm-test01.gls-group.eu:8443/backend/rs/parcelshop/address"


@dataclass
class GLSCredentials:
    base_url: str
    username: str
    password: str
    customer_id: Optional[str] = None
    contact_id: Optional[str] = None
    timeout: int = 30
    content_type: str = "application/glsVersion1+json"
    accept: str = "application/glsVersion1+json, application/json"
    parcelshop_search_url: str = DEFAULT_PARCELSHOP_SEARCH_URL
    cancel_endpoint: Optional[str] = None


def _get_credentials() -> GLSCredentials:
    settings = frappe.get_single("GLS Settings")
    base_url = (settings.gls_api_endpoint or "").rstrip("/")
    username = settings.gls_username
    password = settings.get_password("gls_password")
    customer_id = settings.gls_customer_id
    contact_id = settings.gls_contact_id
    timeout = cint(settings.gls_api_timeout) or 30
    content_type = cstr(settings.gls_api_content_type).strip() or GLSCredentials.content_type
    accept = cstr(settings.gls_api_accept).strip() or GLSCredentials.accept
    parcelshop_search_url = cstr(settings.gls_parcelshop_search_url).strip() or DEFAULT_PARCELSHOP_SEARCH_URL
    cancel_endpoint = cstr(getattr(settings, "gls_cancel_endpoint", "")).strip() or None
    if not all([base_url, username, password]):
        settings_url = frappe.utils.get_url_to_form("GLS Settings", "GLS Settings")
        frappe.throw(f"GLS API credentials not configured. Check <a href='{settings_url}'>GLS Settings</a>.", title="GLS Configuration Error", exc=GLSAPIError)
    return GLSCredentials(
        base_url=base_url,
        username=username,
        password=password,
        customer_id=customer_id,
        contact_id=contact_id,
        timeout=timeout,
        content_type=content_type,
        accept=accept,
        parcelshop_search_url=parcelshop_search_url,
        cancel_endpoint=cancel_endpoint,
    )


class GLSAPIClient:
    
    PARCELSHOP_ENDPOINT = "/backend/rs/parcelshop/address"

    def __init__(self, credentials: GLSCredentials):
        self.creds = credentials
        self.session = requests.Session()
        self.session.auth = (self.creds.username, self.creds.password)
        # Holds the most recent raw ``requests.Response`` from a successful
        # call, so debug/traceability code (e.g. cancel_shipment) can read the
        # HTTP status code + headers that the parsed-body return value drops.
        self._last_response = None

        accept = self.creds.accept or "application/glsVersion1+json, application/json"
        content_type = self.creds.content_type or "application/glsVersion1+json"
        if "charset" not in content_type.lower():
            content_type = f"{content_type}; charset=UTF-8"
        self.session.headers.update({
            "Accept": accept,
            "Content-Type": content_type,
        })

    @classmethod
    def from_settings(cls) -> "GLSAPIClient":
        credentials = _get_credentials()
        return cls(credentials)

    def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> Dict:
        if endpoint.startswith(("http://", "https://")):
            full_url = endpoint
        else:
            full_url = f"{self.creds.base_url}{endpoint}"
        request_headers = self.session.headers.copy()
        if headers:
            
            for key, value in headers.items():
                if value is None:
                    request_headers.pop(key, None)
                else:
                    request_headers[key] = value

        
        data_payload = None
        if json_data:
            import json
            data_payload = json.dumps(json_data, ensure_ascii=False)

        try:
            response = self.session.request(
                method, 
                full_url, 
                params=params, 
                data=data_payload,  
                timeout=self.creds.timeout, 
                headers=request_headers
            )
            response.raise_for_status()
            self._last_response = response
            if response.status_code == 204 or not response.content: return {}
            
            
            content_type_resp = response.headers.get("Content-Type", "").lower()
            try:
                return response.json()
            except ValueError as e:
                
                accepted_types = [t.strip() for t in self.creds.accept.split(',')] + ["application/json"]
                if any(accepted in content_type_resp for accepted in accepted_types if accepted):
                    
                    frappe.log_error("GLS API JSON Decode Error", f"Failed decode for {endpoint}. Response: {response.text}")
                    raise GLSAPIError("Invalid JSON response from GLS API.") from e
                else:
                    
                    frappe.logger().warning(f"GLS API returned unexpected Content-Type ({content_type_resp}) for {endpoint}")
                    return {"_raw_content": response.text}
        except requests.exceptions.HTTPError as e:
            
            if e.response is not None and e.response.status_code == 415 and method.upper() == "POST":
                try:
                    retry_headers = request_headers.copy()
                    retry_headers["Content-Type"] = "application/json; charset=UTF-8"
                    retry_headers["Accept"] = "application/json"
                    retry_resp = self.session.request(
                        method, 
                        full_url, 
                        params=params, 
                        data=data_payload,  
                        timeout=self.creds.timeout, 
                        headers=retry_headers
                    )
                    retry_resp.raise_for_status()
                    self._last_response = retry_resp
                    if retry_resp.status_code == 204 or not retry_resp.content: return {}
                    ct_retry = retry_resp.headers.get("Content-Type", "").lower()
                    accepted_types = [t.strip() for t in self.creds.accept.split(',')] + ["application/json"]
                    if any(accepted in ct_retry for accepted in accepted_types if accepted):
                        try: return retry_resp.json()
                        except ValueError:
                            return {"_raw_content": retry_resp.text}
                    else:
                        return {"_raw_content": retry_resp.text}
                except requests.exceptions.RequestException:
                    
                    pass
            # GLS Shipit returns the validation error in custom response
            # *headers* (X-error / X-message / X-args / "message"), not the
            # body — the body is empty (Content-Length: 0). Previously this
            # code only looked at the body and ended up surfacing a useless
            # "400 Bad Request" string. Prefer the header message; fall back
            # to body, then to status reason.
            details = ""
            try:
                hdrs = e.response.headers or {}
                hdr_msg = (
                    hdrs.get("message")
                    or hdrs.get("X-Message")
                    or hdrs.get("error")
                    or hdrs.get("X-Error")
                )
                if hdr_msg:
                    details = hdr_msg
            except Exception:
                pass
            if not details:
                body_text = e.response.text or ""
                if body_text:
                    details = body_text
                    try:
                        details = frappe.as_json(e.response.json())
                    except Exception:
                        pass
            if not details:
                try:
                    details = f"{e.response.status_code} {e.response.reason}"
                except Exception:
                    details = f"{e}"
            resp_headers = {}
            try:
                resp_headers = dict(e.response.headers or {})
            except Exception:
                resp_headers = {}
            
            if e.response and e.response.status_code == 400:
                print(f"\n=== GLS 400 ERROR DEBUG ===")
                print(f"URL: {full_url}")
                print(f"Method: {method}")
                print(f"Headers: {frappe.as_json(request_headers, indent=2)}")
                print(f"Response: {details}")
                if json_data:
                    print(f"Payload (JSON): {frappe.as_json(json_data, indent=2)}")
                if data_payload:
                    print(f"Payload (Raw): {data_payload}")
                print("=== END DEBUG ===\n")
            
            frappe.log_error(
                f"GLS API HTTP Error ({e.response.status_code})",
                f"URL: {full_url}\nMethod: {method}\nHeaders Sent: {request_headers}\nResponse Headers: {frappe.as_json(resp_headers)}\nResponse: {details}\nBody Sent: {frappe.as_json(json_data)}\nBody Sent Raw: {data_payload}"
            )
            # Surface only a single, user-friendly message to the operator: one
            # extracted line, with generic summary/duplicate sentences removed.
            # The full request/response (incl. the raw `details` payload) is
            # already captured in the Error Log above for debugging.
            raise GLSAPIError(_gls_concise(_extract_gls_user_message(e.response))) from e
        except requests.exceptions.RequestException as e:
            frappe.log_error("GLS API Request Failed", frappe.get_traceback())
            raise GLSAPIError(f"GLS API connection failed: {e}") from e

    def search_parcel_shops_by_address( self, address_payload: Dict ) -> Dict:
        required = ["Street", "CountryCode", "ZIPCode", "City"]
        if not all(address_payload.get(k) for k in required):
             frappe.log_error("GLS ParcelShop Search Error", f"Missing required address fields in payload: {address_payload}")
             return {"results": []}

        
        # Reference country from the requested address — used to drop cross-border
        # shops the radius search returns near borders. We do NOT filter by city:
        # parcel-shop pickup is proximity-based, so the nearest shop is often in a
        # neighbouring town (e.g. Lingenau → Hittisau).
        requested_country = (address_payload.get("CountryCode") or "").strip().upper()

        payload = {
            "Street": address_payload.get("Street", ""),
            "StreetNumber": address_payload.get("StreetNumber", ""),
            "CountryCode": address_payload.get("CountryCode", ""),
            "ZIPCode": address_payload.get("ZIPCode", ""),
            "City": address_payload.get("City", ""),
            "Distance": address_payload.get("Distance", "5"),
            "MaxNumberOfShops": address_payload.get("MaxNumberOfShops", "6")
        }

        try:
            search_endpoint = self.creds.parcelshop_search_url or self.PARCELSHOP_ENDPOINT
            response = self._make_request(
                method="POST",
                endpoint=search_endpoint,
                json_data=payload
            )
            
            formatted_results = []
            for shop in response.get("ParcelShop", []):
                 shop_address_data = shop.get("Address", {})

                 # GLS radius search ignores borders — keep only shops in the SAME
                 # country (drop cross-border results). City is intentionally NOT
                 # filtered: the nearest pickup shop is often in a neighbouring town.
                 shop_country = (shop_address_data.get("CountryCode") or "").strip().upper()
                 if requested_country and shop_country != requested_country:
                     continue

                 full_address = ", ".join(filter(None, [
                     shop_address_data.get("Name1"),
                     shop_address_data.get("Street"),
                     shop_address_data.get("StreetNumber"),
                     shop_address_data.get("ZIPCode"),
                     shop_address_data.get("City"),
                     shop_address_data.get("CountryCode")
                 ]))
                 formatted_results.append({
                     "id": shop.get("ParcelShopID"),
                     "name": shop_address_data.get("Name1"),
                     "address": full_address,
                     "_raw": shop 
                 })
            return {"results": formatted_results}
        except GLSAPIError as e:
            frappe.log_error("GLS ParcelShop Search Error", f"API Error searching near address: {e}");
            return {"results": []}
        except Exception as e:
            frappe.log_error("GLS ParcelShop Search Error", f"Unexpected error searching: {frappe.get_traceback()}");
            return {"results": []}

    

    
    def create_shipping_label( self, sales_order_name: Optional[str], shipment_doc: Document, carrier_product_code: Optional[str] = None, carrier_service_code: Optional[str] = None, label_format: str = "zpl" ) -> Tuple[str, bytes]:
        payload = _build_payload( shipment_doc, self.creds, carrier_product_code, carrier_service_code, label_format=label_format )
        
        label_endpoint = "/backend/rs/shipments"
        
        response_data = self._make_request(method="POST", endpoint=label_endpoint, json_data=payload)
        lbl_b64, trk = _extract_label_and_tracking(response_data)
        try: content = base64.b64decode(lbl_b64)
        except Exception as exc: raise GLSAPIError("Invalid label payload (not base64).") from exc
        return trk, content

    DEFAULT_TRACKING_ENDPOINT = "/backend/rs/tracking/parceldetails"

    def get_parcel_details(self, track_id: str) -> Dict:
        """Track & Trace details for one parcel via the ShipIT REST API.

        The endpoint is configurable in GLS Settings → *Tracking Endpoint*
        (ShipIT installations differ), relative to the API base URL or a full
        ``https://`` URL — resolved the same way as the cancel endpoint. The
        API takes exactly one TrackID per request; batching happens in the
        caller (``parcel/tracking/gls_track.py``). Read-only, safe to repeat.
        """
        endpoint = (
            cstr(frappe.db.get_single_value("GLS Settings", "gls_tracking_endpoint")).strip()
            or self.DEFAULT_TRACKING_ENDPOINT
        )
        return self._make_request(method="POST", endpoint=endpoint, json_data={"TrackID": track_id})

    def cancel_shipment(self, tracking_id: str) -> Dict:
        """Cancel a GLS parcel by its TrackID via the ShipIT REST API.

        The endpoint is **configurable** in GLS Settings → *Cancel Endpoint*
        (``cancel_endpoint`` on the credentials). The ``{trackID}`` placeholder
        in that template is replaced with the actual (URL-encoded) tracking
        number at runtime; a relative path is appended to the GLS API Endpoint
        URL, a full ``https://`` URL is used as-is (handled by ``_make_request``).
        The usual default is ``/backend/rs/shipments/cancel/{trackID}``.

        No request body is sent. On success GLS returns a small JSON result
        (e.g. ``{"result": "CANCELLED"}``); a parcel that can no longer be
        cancelled (already scanned / in transit), an unknown TrackID, or bad
        credentials come back as an HTTP 4xx, which ``_make_request`` surfaces
        as ``GLSAPIError`` (it also logs the full request/response to the Error
        Log). If no Cancel Endpoint is configured, raises
        ``GLSNotConfiguredError`` so callers can skip gracefully. Both the
        outbound request and the response are logged here for audit.
        """
        track = (tracking_id or "").strip()
        if not track:
            raise GLSAPIError("Cannot cancel GLS shipment: no tracking ID provided.")

        endpoint_template = (self.creds.cancel_endpoint or "").strip()
        if not endpoint_template:
            raise GLSNotConfiguredError(
                "GLS Cancel Endpoint is not configured (GLS Settings → Cancel Endpoint)."
            )

        from urllib.parse import quote
        track_enc = quote(track, safe="")
        # Replace the {trackID} placeholder (accept a couple of common spellings).
        endpoint = endpoint_template
        for ph in ("{trackID}", "{trackid}", "{tracking_id}"):
            endpoint = endpoint.replace(ph, track_enc)
        frappe.logger().info(
            f"[gls-cancel] request POST endpoint={endpoint} trackID={track}"
        )
        response = self._make_request(method="POST", endpoint=endpoint)
        frappe.logger().info(
            f"[gls-cancel] response trackID={track}: {frappe.as_json(response)}"
        )

        # --- DEBUG / traceability only -------------------------------------
        # Print the COMPLETE GLS cancellation response — HTTP status code, the
        # GLS message header, all response headers, and the returned payload —
        # to the console and the log. Wrapped in its own try/except so a
        # logging hiccup can never affect the cancellation flow; existing
        # logging + error handling above are left untouched.
        try:
            raw = self._last_response
            resp_headers = dict(getattr(raw, "headers", {}) or {})
            debug_dump = {
                "trackID": track,
                "status_code": getattr(raw, "status_code", None),
                "reason": getattr(raw, "reason", None),
                "message": (
                    resp_headers.get("message")
                    or resp_headers.get("X-Message")
                    or resp_headers.get("error")
                    or resp_headers.get("X-Error")
                ),
                "headers": resp_headers,
                "payload": response,
            }
            print(
                "=== GLS CANCEL RESPONSE ===\n"
                + frappe.as_json(debug_dump, indent=2)
                + "\n=== END GLS CANCEL RESPONSE ==="
            )
            frappe.logger().info(
                f"[gls-cancel] full response dump: {frappe.as_json(debug_dump)}"
            )
        except Exception:
            # Never let debug logging break the cancellation.
            frappe.logger().warning(
                f"[gls-cancel] failed to build debug response dump for {track}",
                exc_info=True,
            )

        return response




def _get_shipment_doc(doc_or_name: Any) -> Document:
    if isinstance(doc_or_name, Document): return doc_or_name
    return frappe.get_doc("Shipment", doc_or_name)

def _get_carrier_service(doc: Document) -> Document:
    
    service_name = getattr(doc, "custom_carrier_service", None)
    if not service_name and doc.doctype == "Shipment" and doc.get("sales_order"):
         service_name = frappe.db.get_value("Sales Order", doc.sales_order, "custom_carrier_service")
    
    if not service_name and doc.doctype == "Shipment" and doc.get("shipment_delivery_note"):
        for dn_row in doc.shipment_delivery_note:
            if dn_row.delivery_note:
                service_name = frappe.db.get_value("Delivery Note", dn_row.delivery_note, "custom_carrier_service")
                if service_name:
                    break
    
    if not service_name:
        raise GLSAPIError(_(
            "Carrier Service could not be determined. "
            "Please set 'custom_carrier_service' on the Shipment or Delivery Note."
        ))
    return frappe.get_cached_doc("Carrier Service", service_name)


def _get_address_dict(address_name: Optional[str], customer_name: Optional[str] = None, contact_person: Optional[str] = None, recipient_email: Optional[str] = None, recipient_phone: Optional[str] = None) -> Dict[str, Any]:
    if not address_name: return {}
    try:
        address = frappe.get_doc("Address", address_name)



        name1 = customer_name or getattr(address, "address_title", None) or getattr(address, "address_line1", "")



        # Resolve `contact_person` (the Contact's *name* / ID — a Link field
        # value, not a display string) to the Contact's `full_name`. ERPNext
        # auto-generates Contact IDs like "Muhammad Saqib younas-Muhammad
        # Saqib younas" when the source Customer name is the same as both
        # the first and last name input; passing that ID into a carrier API
        # produces invalid Name2 values (GLS 400: "Invalid field
        # Shipment.Consignee.Address.Name2"). full_name is the clean,
        # human-readable name; fall back to the raw ID only if the Contact
        # has no full_name set.
        contact_full_name = ""
        if contact_person:
            full_name = frappe.db.get_value(
                "Contact", contact_person, "full_name"
            )
            contact_full_name = (full_name or contact_person).strip()

        # Name2 is GLS's slot for a *secondary* identifier — a company name
        # when shipping to "ACME GmbH, c/o John Doe", or a real second
        # occupant. For a typical B2C webshop checkout the customer's own
        # name lands in BOTH address_title (-> name1) AND the linked
        # Contact.full_name, so populating name2 from the contact would
        # produce ``Name1=Name2="John Doe"`` — a useless duplicate in the
        # wire payload. Resolve a candidate, then drop it if it matches
        # name1 once both are whitespace-normalised and case-folded (the
        # webshop sometimes writes the customer name with a double space
        # into address_title — "Bisrat  Mehari" vs the Contact's clean
        # "Bisrat Mehari" — and a strict equality check misses that).
        # ``address_line2`` is NOT a fallback — it's a street component
        # (apartment / building continuation), never a name.
        import re

        def _norm_name(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").strip()).casefold()

        candidate = (
            contact_full_name
            or getattr(address, "contact_display", "")
            or getattr(address, "contact_person", "")
            or ""
        ).strip()
        if candidate and _norm_name(candidate) == _norm_name(name1):
            candidate = ""
        name2 = candidate

        # GLS Name fields have a strict 40-character limit. Trim defensively
        # so a long contact/company name (or legacy malformed value) doesn't
        # produce a 400 from the carrier.
        if name2 and len(name2) > 40:
            name2 = name2[:40].rstrip()
        
        
        street = getattr(address, "address_line1", "") or ""
        
        
        
        house_no = ""
        addr_line2 = getattr(address, "address_line2", "") or ""
        
        if addr_line2 and addr_line2.strip() and addr_line2.strip()[0].isdigit():
            house_no = addr_line2.strip()
        
        
        gls_addr = {
            "name1": name1,
            "name2": name2,
            "name3": "",  
            "street": street,
            "houseNo": house_no,
            "zipCode": getattr(address, "pincode", "") or "",
            "city": getattr(address, "city", "") or "",
            "country": getattr(address, "country_code", None) or getattr(address, "country", None) or "", 
            # Email/phone resolution order: the explicit recipient_* args
            # (typically the Contact's email/phone), then the Address's own
            # fields. For parcel-shop deliveries the Address holds the SHOP's
            # contact info (often empty — a kiosk doesn't have an email), so
            # the caller-supplied recipient values are required to satisfy
            # GLS's `CONSIGNEE_ADDRESS_EMAIL_MANDATORY` check.
            "email": recipient_email or getattr(address, "email_id", "") or "",
            "phone": recipient_phone or getattr(address, "phone", "") or getattr(address, "mobile_no", "") or "",
            "contact": (getattr(address, "contact_display", "") or getattr(address, "contact_person", "") or ""),
        }
        
        if not gls_addr["country"]: del gls_addr["country"] 
        return gls_addr
    except frappe.DoesNotExistError: frappe.log_error(f"Address not found: {address_name}"); return {}

def _to_gls_address(addr: Dict[str, Any]) -> Dict[str, Any]:
    if not addr:
        return {}

    import re

    def norm(v: Any) -> str:
        # Strip outer whitespace AND collapse internal runs of whitespace
        # to a single space. Webshop checkout sometimes writes names with
        # accidental double spaces ("Bisrat  Mehari" instead of
        # "Bisrat Mehari") — preserving that into the GLS wire payload
        # produces ugly labels and breaks the Name1/Name2 dedup logic
        # in _get_address_dict. Apply uniformly to every string field
        # so street/city/etc. get the same cleanup.
        return re.sub(r"\s+", " ", cstr(v or "")).strip()


    country = norm(addr.get("country"))
    if country and len(country) != 2:
        try:
            code = frappe.db.get_value("Country", country, "code")
            country_code = cstr(code or country[:2]).upper()
        except Exception:
            country_code = country[:2].upper()
    else:
        country_code = country.upper() if country else ""

    
    street = norm(addr.get("street"))
    house_no = norm(addr.get("houseNo"))
    if house_no:
        if street:
            
            street = f"{street} {house_no}"
        else:
            
            street = house_no

    
    name1 = norm(addr.get("name1"))
    if not name1:
        
        frappe.logger().warning("GLS Address: name1 is empty, using street as fallback")
        name1 = street or "Unknown Recipient"

    
    name2 = norm(addr.get("name2"))
    
    
    name3 = norm(addr.get("name3"))

    
    email_val = norm(addr.get("email"))
    mobile_val = norm(addr.get("phone"))

    
    mapped: Dict[str, Any] = {
        "Name1": name1,
        "Name2": name2 or "",
        "Name3": name3 or "",
        "CountryCode": country_code,
        "ZIPCode": norm(addr.get("zipCode")),
        "City": norm(addr.get("city")),
        "Street": street,
        "eMail": email_val or "",
        "MobilePhoneNumber": mobile_val or "",
    }

    
    # Hard-error instead of warn — GLS will reject the call anyway, but
    # surfacing it here gives ops the specific Address field that's empty.
    # Single source of truth: the canonical shipping Address must carry
    # everything GLS needs.
    required_fields = ["Name1", "CountryCode", "ZIPCode", "City", "Street"]
    missing = [f for f in required_fields if not mapped.get(f)]
    if missing:
        raise GLSAPIError(_(
            f"GLS Address is incomplete — missing required field(s): "
            f"{', '.join(missing)}. The canonical shipping Address must "
            f"carry every field GLS expects; no fallback to billing "
            f"address is performed."
        ))

    return mapped

def _sender_address(doc: Document) -> Dict[str, Any]:
    
    company_address_name = getattr(doc, "company_address", None)
    if not company_address_name and getattr(doc, "company", None):
        info = get_company_address(doc.company); company_address_name = getattr(info, "company_address", None) if info else None
    if not company_address_name: raise GLSAPIError(_("Company Address (Sender) required."))
    return _get_address_dict(company_address_name)

def _receiver_address(doc: Document) -> Dict[str, Any]:
    # Resolve the SINGLE source-of-truth Address — never falls back to
    # customer_address (billing). See _canonical_shipping_address_name.
    addr = _canonical_shipping_address_name(doc)
    _validate_address_completeness(addr)
    
    
    customer_name = None
    contact_person = None
    
    
    if hasattr(doc, "customer_name"):
        customer_name = getattr(doc, "customer_name", None)
    elif hasattr(doc, "customer"):
        
        customer_name = getattr(doc, "customer", None)
    
    
    if hasattr(doc, "contact_person"):
        contact_person = getattr(doc, "contact_person", None)
    elif hasattr(doc, "contact_display"):
        contact_person = getattr(doc, "contact_display", None)
    
    
    if not customer_name and doc.doctype == "Shipment":
        try:
            for dn_row in (doc.get("shipment_delivery_note") or []):
                if getattr(dn_row, "delivery_note", None):
                    dn_name = dn_row.delivery_note
                    
                    customer_name = frappe.db.get_value("Delivery Note", dn_name, "customer_name")
                    if not contact_person:
                        contact_person = frappe.db.get_value("Delivery Note", dn_name, "contact_person")
                    if customer_name:
                        break
        except Exception:
            pass
    
    
    if not customer_name and doc.doctype == "Shipment" and doc.get("sales_order"):
        try:
            customer_name = frappe.db.get_value("Sales Order", doc.sales_order, "customer_name")
            if not contact_person:
                contact_person = frappe.db.get_value("Sales Order", doc.sales_order, "contact_person")
        except Exception:
            pass

    # Resolve recipient email/phone from the linked Contact. Parcel-shop
    # Addresses don't carry customer contact info (a shop has no email);
    # GLS rejects the request with CONSIGNEE_ADDRESS_EMAIL_MANDATORY if
    # neither the Address nor the request carries one. Try Contact first
    # (the canonical place per ERPNext data model); fall back to Customer
    # for older customers that pre-date the Contact-link convention.
    recipient_email = None
    recipient_phone = None
    if contact_person:
        contact_row = frappe.db.get_value(
            "Contact", contact_person,
            ["email_id", "mobile_no", "phone"], as_dict=True,
        )
        if contact_row:
            recipient_email = contact_row.email_id or None
            recipient_phone = contact_row.mobile_no or contact_row.phone or None

    if not recipient_email and customer_name:
        recipient_email = frappe.db.get_value(
            "Customer", customer_name, "email_id"
        ) or None
    if not recipient_phone and customer_name:
        recipient_phone = frappe.db.get_value(
            "Customer", customer_name, "mobile_no"
        ) or None

    return _get_address_dict(
        addr,
        customer_name=customer_name,
        contact_person=contact_person,
        recipient_email=recipient_email,
        recipient_phone=recipient_phone,
    )

def _derive_weight(doc: Document) -> float:
    # Shipment-shaped docs carry the canonical weight on `total_weight`,
    # populated either from the live warehouse scale (see
    # `core._apply_scale_weight`) or from item metadata as a fallback.
    # Prefer that, then the sum of `shipment_parcel` rows, then the
    # Delivery-Note-shaped `items` path. Floor at 0.1 kg — GLS rejects 0.
    source = "none"
    total_weight = 0.0

    if doc.get("total_weight"):
        total_weight = flt(doc.get("total_weight"), 3)
        source = "shipment.total_weight"

    if not total_weight and doc.get("shipment_parcel"):
        total_weight = sum(flt(p.weight, 3) for p in doc.shipment_parcel if p.weight)
        if total_weight:
            source = "sum(shipment_parcel.weight)"

    if not total_weight and doc.get("items"):
        total_weight = sum(
            flt(item.weight_per_unit, 3) * flt(item.qty)
            if item.weight_per_unit and item.qty
            else flt(item.total_weight, 3)
            for item in doc.items
        )
        if total_weight:
            source = "sum(items: weight_per_unit*qty || total_weight)"

    final_weight = max(round(total_weight, 3), 0.1)
    if final_weight == 0.1 and total_weight < 0.1:
        source = f"floor(0.1) — raw={total_weight}"

    frappe.logger().info(
        f"[gls weight] doctype={doc.doctype} name={getattr(doc, 'name', '<new>')} "
        f"source={source} value={final_weight}"
    )
    return final_weight


def _canonical_shipping_address_name(doc: Document) -> str:
    """Return the ONE Address.name that is the single source of truth for
    every GLS-payload field that depends on the recipient address — the
    Consignee block, the ParcelShopID, the country-code consistency check,
    everything.

    Strict resolution order — never falls back to billing fields:

      Delivery Note doc  ->  doc.shipping_address_name
      Shipment doc       ->  doc.delivery_address_name
                              -> if empty, walk to the FIRST linked
                                 Delivery Note's shipping_address_name
                                 (preserves SO->DN->Shipment chain for
                                 Shipments created via the auto-creation
                                 hook, which only sets that field on
                                 the Shipment itself).

    Hard-throws GLSAPIError if no shipping address is set anywhere in the
    chain. ``customer_address`` (the BILLING address) is deliberately NOT
    consulted — the user's directive: "no Customer Master or previous
    address injection". Mixing billing and shipping is the root cause of
    the inconsistent CountryCode / City / ZIPCode / ParcelShopID payloads
    we've been seeing.
    """
    if doc.doctype == "Delivery Note":
        addr = getattr(doc, "shipping_address_name", None)
        if not addr:
            raise GLSAPIError(_(
                f"Delivery Note '{doc.name}' has no shipping_address_name "
                f"set. Cannot build GLS payload — the DN must carry its "
                f"own shipping address."
            ))
        return addr

    if doc.doctype == "Shipment":
        addr = getattr(doc, "delivery_address_name", None)
        if addr:
            return addr
        # Fall back to the FIRST linked DN's shipping address — same record
        # the Shipment was created from. Never customer_address.
        for dn_row in (doc.get("shipment_delivery_note") or []):
            dn = getattr(dn_row, "delivery_note", None)
            if not dn:
                continue
            dn_addr = frappe.db.get_value(
                "Delivery Note", dn, "shipping_address_name"
            )
            if dn_addr:
                return dn_addr
        raise GLSAPIError(_(
            f"Shipment '{doc.name}' has no delivery_address_name and no "
            f"linked Delivery Note carries a shipping_address_name. "
            f"Cannot build GLS payload without a shipping address."
        ))

    raise GLSAPIError(_(
        f"Unsupported doctype '{doc.doctype}' for GLS payload — expected "
        f"Delivery Note or Shipment."
    ))


def _validate_address_completeness(address_name: str) -> None:
    """Fail fast if the canonical shipping Address is missing fields that
    GLS would reject downstream (CountryCode / City / ZIPCode / Street).
    Surfaces the gap with the Address record name so ops can fix the source
    rather than chasing a generic GLS 400 from the wire.
    """
    row = frappe.db.get_value(
        "Address", address_name,
        ["address_line1", "city", "pincode", "country"],
        as_dict=True,
    )
    if not row:
        raise GLSAPIError(_(f"Address '{address_name}' not found."))
    missing: List[str] = []
    if not (row.get("address_line1") or "").strip():
        missing.append("address_line1 (Street)")
    if not (row.get("city") or "").strip():
        missing.append("city (City)")
    if not (row.get("pincode") or "").strip():
        missing.append("pincode (ZIPCode)")
    if not (row.get("country") or "").strip():
        missing.append("country (CountryCode)")
    if missing:
        raise GLSAPIError(_(
            f"Address '{address_name}' is incomplete — missing required "
            f"GLS field(s): {', '.join(missing)}. Fix the Address record "
            f"before re-attempting label creation."
        ))


def _parcel_shop_country_prefix(parcel_shop_id: str) -> Optional[str]:
    """Best-effort extract of the country code embedded in a parcel-shop ID.
    Recognised formats:
      ``GLS_DE-2760930405``  -> 'DE'   (carrier-prefixed)
      ``DE-1234567``         -> 'DE'   (bare country prefix)
      ``GB-1154814``         -> 'GB'
    Returns ``None`` when the format isn't recognised — callers should
    treat ``None`` as "can't validate, skip the check" rather than as an
    error, so we never block on a parcel-shop ID with an unknown format.
    """
    import re
    m = re.match(r"^(?:[A-Z]+_)?([A-Z]{2})[-_]", (parcel_shop_id or "").upper())
    return m.group(1) if m else None


def _parcel_shop_id_from_canonical_address(
    address_name: str, receiver_country_code: str
) -> str:
    """Read ``parcel_shop_id`` from the SAME Address record the Consignee
    block is built from. No SO/DN walking, no fallback chain — the
    parcel-shop and the consignee are guaranteed to be the same record.

    Validates two consistency rules before returning:
      1. The Address must actually be flagged ``is_parcel_shop=1`` and
         carry a ``parcel_shop_id``.
      2. If the ``parcel_shop_id`` encodes a country code (e.g.
         ``GLS_DE-...``), that code must match the Consignee country code.
         An ID format we don't recognise is allowed through (we can't
         validate what we can't parse).
    """
    row = frappe.db.get_value(
        "Address", address_name,
        ["is_parcel_shop", "parcel_shop_id", "country"],
        as_dict=True,
    )
    if not row:
        raise GLSAPIError(_(f"Address '{address_name}' not found."))
    if not row.is_parcel_shop:
        raise GLSAPIError(_(
            f"Shipping Address '{address_name}' is not flagged as a parcel "
            f"shop (is_parcel_shop=0) but delivery_type is "
            f"'Parcelshop Delivery'. Either toggle is_parcel_shop on the "
            f"Address or switch delivery_type to 'Home Delivery'."
        ))
    if not row.parcel_shop_id:
        raise GLSAPIError(_(
            f"Shipping Address '{address_name}' has no parcel_shop_id set."
        ))
    psid_country = _parcel_shop_country_prefix(row.parcel_shop_id)
    if psid_country and psid_country != (receiver_country_code or "").upper():
        raise GLSAPIError(_(
            f"ParcelShop ID '{row.parcel_shop_id}' encodes country "
            f"'{psid_country}' but the Consignee CountryCode is "
            f"'{receiver_country_code}'. Cannot mix countries between the "
            f"shipping Address and the parcel-shop ID — both must match."
        ))
    return row.parcel_shop_id


def _build_services(
    doc: Document,
    carrier_service: Document,
    service_code_override: Optional[str] = None,
    receiver_country_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    override = cstr(service_code_override).strip()
    primary_code = override or cstr(getattr(carrier_service, "service_code", None) or "").strip()
    if not primary_code:
        raise GLSAPIError(f"Carrier Service '{carrier_service.name}' missing GLS service code.")

    
    delivery_type = getattr(doc, "delivery_type", None)
    if not delivery_type and doc.doctype == "Shipment" and doc.get("sales_order"):
        delivery_type = frappe.db.get_value("Sales Order", doc.sales_order, "delivery_type")
    if not delivery_type and doc.doctype == "Shipment":
        
        try:
            for dn_row in (doc.get("shipment_delivery_note") or []):
                if getattr(dn_row, "delivery_note", None):
                    dt = frappe.db.get_value("Delivery Note", dn_row.delivery_note, "delivery_type")
                    if dt:
                        delivery_type = dt
                        break
        except Exception:
            pass

    services = []
    
    if delivery_type == "Parcelshop Delivery":
        # Read parcel_shop_id from the SAME canonical Address that the
        # Consignee block is built from. _parcel_shop_id_from_canonical_address
        # validates is_parcel_shop=1, parcel_shop_id non-empty, AND that
        # any country prefix in the ID matches the receiver country. No
        # walking of SO/DN/customer_address.
        canonical_addr = _canonical_shipping_address_name(doc)
        parcel_shop_id = _parcel_shop_id_from_canonical_address(
            canonical_addr, receiver_country_code or ""
        )

        services.append({
            "ShopDelivery": {
                "ServiceName": "service_shopdelivery",
                "ParcelShopID": parcel_shop_id
            }
        })
    elif delivery_type == "Home Delivery":
        # Home Delivery -> standard parcel delivery to the recipient's address,
        # booked with FlexDeliveryService: GLS notifies the recipient (eMail /
        # MobilePhoneNumber on the Consignee) and lets them reschedule or
        # redirect the parcel. Free of charge on our contract. Never forward
        # the Carrier Service's own code here — "service_shopdelivery" as
        # ShopDelivery makes GLS demand a parcelShopID we don't have
        # (400 MANDATORY_PARAMETER_NOT_SET).
        return [_FLEX_DELIVERY_SERVICE]
    else:


        service_type = _map_service_code_to_type(primary_code)
        services.append({
            service_type: {
                "ServiceName": primary_code
            }
        })

    return services


# GLS Shipit has no dedicated FlexDeliveryService element — the simple
# services go through the generic "Service" wrapper. A
# {"FlexDeliveryService": …} entry is rejected with
# 'Unrecognized field "FlexDeliveryService" (class …ShipmentService)'.
_FLEX_DELIVERY_SERVICE = {"Service": {"ServiceName": "service_flexdelivery"}}


def _map_service_code_to_type(service_code: str) -> str:
    
    mapping = {
        "service_shopdelivery": "ShopDelivery",
        "service_shopreturn": "ShopReturn",
        
    }
    return mapping.get(service_code.lower(), "Service")  

# Countries that share the EU customs union — shipments staying inside this
# set do NOT need a customs Incoterm. Anything else (UK after Brexit, CH, NO,
# US, …) is treated as a non-EU/customs destination and requires an
# IncotermCode on the GLS Shipit payload, otherwise GLS returns:
#   "Invalid field shipment.incoterm. Value SHIPMENT_VALID_INCOTERM_IF_NEEDED
#    is not a valid value."
# Single source of truth lives in parcel.customs — GLS and FedEx both key their
# customs handling off the same membership list.
_EU_COUNTRY_CODES = EU_COUNTRY_CODES

# Default GLS IncotermCode for non-EU B2C orders.
#   10 = DAP, recipient pays VAT
#   20 = DAP, sender pays VAT, recipient pays duties
#   30 = DDP — Delivered Duty Paid (sender pays VAT + duties)  <- friendliest
#   40 = DDP without TAX
# 30 keeps the recipient from getting hit with a surprise customs bill on the
# doorstep, which is the standard B2C convention. Daniel/ops can override per
# shipment by setting Shipment.custom_gls_incoterm_code if/when that field is
# added to the doctype.
_DEFAULT_NON_EU_INCOTERM = "30"

# GLS Shipit's ProductType enum only accepts EXPRESS, PARCEL, FREIGHT,
# PHARMA, PHARMAPLUS. There is NO "global" / "non-EU" variant — non-EU
# customs handling is signalled by IncotermCode + (optionally) per-country
# Services, not by switching Product. An earlier attempt to upgrade
# PARCEL -> GLOBALBUSINESSPARCEL for UK was rejected with:
#   InvalidFormatException: Cannot deserialize value of type
#   `eu.gls_group.fpcs.v1.common.ProductType` from String
#   "GLOBALBUSINESSPARCEL"
# Keep Product as the Carrier-Service-configured value; default to PARCEL.


def _resolve_incoterm_code(receiver_gls: Dict[str, Any], doc: Document) -> Optional[str]:
    """Return the IncotermCode to include on the GLS Shipit payload, or
    ``None`` if the destination is intra-EU and no Incoterm is needed.

    Resolution order:
      1. ``doc.custom_gls_incoterm_code`` if explicitly set on the Shipment
         (manual override; field is optional, may not exist).
      2. The default ``_DEFAULT_NON_EU_INCOTERM`` for any non-EU destination.
      3. ``None`` for EU destinations.
    """
    country_code = cstr(receiver_gls.get("CountryCode") or "").upper().strip()
    if not country_code or country_code in _EU_COUNTRY_CODES:
        return None
    override = cstr(getattr(doc, "custom_gls_incoterm_code", "") or "").strip()
    return override or _DEFAULT_NON_EU_INCOTERM


def _build_payload( doc: Document, credentials: GLSCredentials, product_code: Optional[str] = None, service_code: Optional[str] = None, label_format: str = "zpl" ) -> Dict[str, Any]:
    cs = _get_carrier_service(doc)
    if (cs.carrier or "").upper() != "GLS": raise GLSAPIError(f"Carrier Service '{cs.name}' not for GLS.")
    
    
    receiver = _receiver_address(doc)
    receiver_gls = _to_gls_address(receiver)
    
    
    shipper = {}
    if credentials.contact_id:
        
        shipper = {"ContactID": credentials.contact_id}
        frappe.logger().info(f"Using GLS Shipper ContactID: {credentials.contact_id}")
    else:
        
        shipper_addr = _sender_address(doc)
        shipper = {"Address": _to_gls_address(shipper_addr)}
        frappe.logger().info("Using full sender address for GLS shipment")
    
    
    # Pass the receiver CountryCode through so ShopDelivery's parcel_shop_id
    # can be validated against the same country (no GB/DE mixing, etc.).
    services = _build_services(
        doc, cs, service_code,
        receiver_country_code=cstr(receiver_gls.get("CountryCode") or "").upper().strip(),
    )
    ref = doc.name


    shipment_units = [{"Weight": _derive_weight(doc)}]


    product = cstr(product_code).strip() or cstr(getattr(cs, "product_code", None) or "").strip() or "PARCEL"
    
    
    # Step 12: read the GLS `Middleware` identifier from Parcel Station
    # Settings instead of hard-coding "Holzschuhe Versand". The Middleware
    # tag is sent with every GLS shipment payload — GLS uses it to identify
    # the calling system. We map it to the Sender section's `sender_name`
    # so it tracks the configured tenant identity, and fall back to a
    # neutral "Parcel Station" if the sender section hasn't been filled in
    # yet (matches the OSS release path — non-Devich deployments don't
    # ship the literal "Holzschuhe Versand").
    # get_single_value raises if the fieldname doesn't exist on the doctype
    # (not just when it's empty). Some deployments haven't yet added a Sender
    # section to Parcel Station Settings, so treat "field missing" the same
    # as "field empty" and fall back to a neutral identifier.
    try:
        middleware_id = (
            frappe.db.get_single_value("Parcel Station Settings", "sender_name")
            or "Parcel Station"
        )
    except Exception:
        middleware_id = "Parcel Station"
    shipment_payload = {
        "ShipmentReference": [ref],
        "Middleware": middleware_id,
        "Product": product,
        "Shipper": shipper,
        "Consignee": {"Address": receiver_gls},
        "ShipmentUnit": shipment_units,
    }

    # IncotermCode is mandatory for non-EU destinations (UK, CH, NO, …).
    # GLS rejects label creation with
    # "Invalid field shipment.incoterm. Value SHIPMENT_VALID_INCOTERM_IF_NEEDED
    #  is not a valid value" when the recipient country is outside the EU
    # customs union and this field is absent. See _resolve_incoterm_code.
    incoterm = _resolve_incoterm_code(receiver_gls, doc)
    if incoterm:
        shipment_payload["IncotermCode"] = incoterm

    if services:
        shipment_payload["Service"] = services
    
    
    
    payload = {
        "Shipment": shipment_payload,
        "PrintingOptions": {"ReturnLabels": _return_labels(label_format)},
    }
    return payload


def _return_labels(label_format: str = "zpl") -> Dict[str, str]:
    """GLS PrintingOptions for the requested label format.

    ZPL for a 203 dpi label printer, or the label as a PDF page of its own
    size (about A6) for a station that prints through the browser. Both
    verified against the GLS test system.
    """
    if label_format == "pdf":
        return {"TemplateSet": "NONE", "LabelFormat": "PDF"}
    return {"TemplateSet": "ZPL_200", "LabelFormat": "ZEBRA"}

# Step 12: removed `_resolve_gls_endpoint`. It was a dead helper that
# resolved per-endpoint URL overrides from `site_config.json` (e.g.
# "gls_parcelshop_search_url") and had zero callers. Endpoint URLs now
# live on `GLS Settings` (single doctype) and are read directly via
# `frappe.db.get_single_value("GLS Settings", "<field>")`.

def _extract_label_and_tracking(data: Dict[str, Any]) -> Tuple[str, str]:
    if not data:
        raise GLSAPIError(_("Empty response from GLS API."))
    
    
    created_shipment = data.get("CreatedShipment")
    if not created_shipment:
        raise GLSAPIError(f"No 'CreatedShipment' in response: {frappe.as_json(data)}")
    
    
    parcel_data = created_shipment.get("ParcelData")
    if not parcel_data or not isinstance(parcel_data, list) or not parcel_data:
        raise GLSAPIError(f"No 'ParcelData' array in response: {frappe.as_json(data)}")
    
    tracking_number = parcel_data[0].get("TrackID")
    if not tracking_number:
        raise GLSAPIError(f"No 'TrackID' in ParcelData[0]: {frappe.as_json(data)}")
    
    
    print_data = created_shipment.get("PrintData")
    if not print_data or not isinstance(print_data, list) or not print_data:
        raise GLSAPIError(f"No 'PrintData' array in response: {frappe.as_json(data)}")
    
    label_base64 = print_data[0].get("Data")
    if not label_base64:
        raise GLSAPIError(f"No 'Data' in PrintData[0]: {frappe.as_json(data)}")
    
    return label_base64, tracking_number


def _create_file_attachment(doc: Document, content: bytes, prefix: str = "GLS-Label", ext: str = "zpl") -> str:

    file_type = "PDF" if ext == "pdf" else "Text"
    fname = f"{doc.name}-{prefix}.{ext}"
    fid = frappe.db.exists("File", {"attached_to_doctype": doc.doctype, "attached_to_name": doc.name, "file_name": fname})
    f = frappe.get_doc("File", fid) if fid else frappe.new_doc("File")
    f.file_name = fname
    f.attached_to_doctype = doc.doctype
    f.attached_to_name = doc.name
    f.is_private = 1
    f.content = content
    f.file_type = file_type
    f.save(ignore_permissions=True)
    return f.file_url

def _log_label_trigger(shipment_name: str) -> None:
    """Log who triggered label creation, so duplicate calls are traceable."""
    import traceback

    req = getattr(frappe.local, "request", None)
    if req is not None:
        source = f"HTTP request ({getattr(req, 'path', '?')})"
    elif getattr(frappe.flags, "in_background_jobs", False) or getattr(frappe.flags, "in_worker", False):
        source = "background worker / job"
    else:
        source = "internal call / doc hook"
    # Last few frames before this function — identifies on_submit hook vs request_label vs barcode flow.
    chain = " <- ".join(f.name for f in reversed(traceback.extract_stack(limit=7)[:-1]))
    frappe.logger().info(f"[label-trigger] create_label({shipment_name}) via {source}; chain: {chain}")


def create_label( doc_or_name: Any, carrier_product_code: Optional[str] = None, carrier_service_code: Optional[str] = None, label_format: str = "zpl" ) -> Dict[str, Any]:
    doc = _get_shipment_doc(doc_or_name)
    _log_label_trigger(doc.name)

    # Idempotency + concurrency guard: never request a second parcel from GLS
    # for the same Shipment.
    #
    # The SELECT ... FOR UPDATE locks this Shipment's row for the rest of the
    # transaction, so two concurrent triggers (e.g. the Shipment.on_submit hook
    # AND an explicit request_label button/worker) serialize here instead of
    # both hitting GLS. The first stores the tracking number and commits; the
    # second then acquires the lock, sees the stored tracking, and returns the
    # existing shipment/label without an API call. Within a single transaction
    # the same read-your-writes check catches a sequential second call too.
    locked_trk = frappe.db.get_value("Shipment", doc.name, "awb_number", for_update=True)
    existing_trk = (
        locked_trk
        or getattr(doc, "custom_tracking_number", None)
        or getattr(doc, "awb_number", None)
    )
    if existing_trk:
        # Whatever format it was issued in back then — a label exists once.
        existing_url = frappe.db.get_value(
            "File",
            {
                "attached_to_doctype": doc.doctype,
                "attached_to_name": doc.name,
                "file_name": ("like", f"{doc.name}-GLS-Label.%"),
            },
            "file_url",
            order_by="creation desc",
        )
        frappe.logger().info(
            f"GLS label already exists for Shipment {doc.name} (tracking {existing_trk}); skipping GLS API call"
        )
        return {"tracking_number": existing_trk, "file_url": existing_url, "existing": True}

    client = GLSAPIClient.from_settings()
    try:
        trk, lbl_content = client.create_shipping_label( doc.get("sales_order"), doc, carrier_product_code, carrier_service_code, label_format=label_format )
        f_url = _create_file_attachment(doc, lbl_content, ext="pdf" if label_format == "pdf" else "zpl")
        tracking_field = None
        if hasattr(doc, "custom_tracking_number"):
            tracking_field = "custom_tracking_number"
        elif hasattr(doc, "awb_number"):
            tracking_field = "awb_number"
        if tracking_field:
            frappe.db.set_value(doc.doctype, doc.name, tracking_field, trk, update_modified=False)
        else:
            frappe.logger().warning(f"No tracking number field found on Shipment {doc.name}")
        doc.add_comment("Info", _(f"GLS label created. Tracking: {trk}"))
        frappe.msgprint(_(f"GLS label generated. Tracking: {trk}"), indicator="green", alert=True)
        return {"tracking_number": trk, "file_url": f_url}
    except GLSAPIError as e:
        # ``e`` already carries the single concise GLS message; don't truncate it
        # mid-sentence (the old [:120] cut "…not a valid value" off). Prefix once.
        error_msg = _gls_concise(str(e))
        frappe.log_error("GLS Label Failed", f"Shipment: {doc.name}\nError: {error_msg}")
        frappe.throw(f"Failed to create GLS label: {error_msg}")
    except Exception as e: 
        frappe.log_error("GLS Unexpected Error", frappe.get_traceback())
        frappe.throw(_("Unexpected error creating GLS label. Check Error Log for details."))


@whitelist()
def preview_gls_payload(shipment: str, carrier_product_code: Optional[str] = None, carrier_service_code: Optional[str] = None) -> Dict[str, Any]:
    doc = _get_shipment_doc(shipment)
    creds = _get_credentials()
    payload = _build_payload(doc, creds, carrier_product_code, carrier_service_code)
    
    services_summary: List[str] = []
    try:
        for svc in (payload.get("Shipment", {}).get("Service", []) or []):
            
            if isinstance(svc, dict) and svc:
                svc_type = list(svc.keys())[0]
                svc_name = svc.get(svc_type, {}).get("ServiceName")
                if svc_name:
                    services_summary.append(f"{svc_type}:{svc_name}")
                else:
                    services_summary.append(svc_type)
    except Exception:
        services_summary = []
    return {
        "shipment": doc.name,
        "services": services_summary,
        "payload": payload,
    }
