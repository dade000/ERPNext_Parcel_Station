import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint
from .gls_api import GLSAPIClient, GLSAPIError, GLSNotConfiguredError, GLSCredentials, _get_credentials, create_label
from ..carrier_routing import AUSTRIAN_POST, FEDEX, GLS, resolve_carrier

LOGGER = frappe.logger("holzschu_gls", allow_site=True)


def get_gls_credentials() -> GLSCredentials:
    return _get_credentials()


def map_dn_to_gls_parcel(dn_doc: Document) -> Dict[str, Any]:
    """
    Map Delivery Note to GLS parcel payload.
    Placeholder implementation - adapt as needed.
    """
    # Basic mapping - expand based on GLS API requirements
    return {
        "customer_name": dn_doc.customer_name,
        "address": dn_doc.shipping_address_name or dn_doc.customer_address,
        "weight_kg": sum(item.weight_per_unit * item.qty for item in dn_doc.items if item.weight_per_unit),
        "service_code": getattr(dn_doc, "custom_carrier_service", None),
    }


def get_label_from_gls(tracking_number: str, creds: GLSCredentials) -> bytes:
    """
    Get label data from GLS API.
    Placeholder - implement actual API call.
    """
    # Placeholder - return empty bytes
    return b""


def _get_carrier_service_name(doc: Document) -> Optional[str]:
    return getattr(doc, "custom_carrier_service", None) or None


# Minimal per-country ZIP rules. Carriers (GLS, DPD, Hermes, Post.at) all
# reject obviously malformed addresses, so a fast local check saves an API
# round-trip and gives the operator a useful error message. Only the country
# codes we actively ship to are listed; anything else falls through to a
# generic length check.
_ZIP_RULES_BY_COUNTRY: Dict[str, Dict[str, Any]] = {
    "Austria":  {"length": 4, "numeric": True},
    "Germany":  {"length": 5, "numeric": True},
    "Switzerland": {"length": 4, "numeric": True},
}


def _validate_shipping_address_for_carrier(address_name: str) -> List[str]:
    """Return a list of human-readable error strings for the named Address.

    Empty list means the address looks OK for handing to a carrier API.
    Checks the fields every carrier we integrate with requires: City,
    pincode (ZIP), country, plus a non-garbage street/line1. ZIP format is
    validated against the country when we know the rule.
    """
    errors: List[str] = []
    try:
        addr = frappe.get_doc("Address", address_name)
    except Exception:
        return [f"address record '{address_name}' not found"]

    line1 = (getattr(addr, "address_line1", "") or "").strip()
    city = (getattr(addr, "city", "") or "").strip()
    pincode = (getattr(addr, "pincode", "") or "").strip()
    country = (getattr(addr, "country", "") or "").strip()

    if not line1:
        errors.append("address_line1 is empty")
    if not city:
        errors.append("city is empty")
    if not pincode:
        errors.append("pincode/ZIP is empty")
    if not country:
        errors.append("country is empty")

    if pincode:
        rule = _ZIP_RULES_BY_COUNTRY.get(country)
        if rule:
            if rule.get("numeric") and not pincode.isdigit():
                errors.append(
                    f"pincode '{pincode}' is not numeric "
                    f"(required for {country})"
                )
            expected_len = rule.get("length")
            if expected_len and len(pincode) != expected_len:
                errors.append(
                    f"pincode '{pincode}' must be {expected_len} digits "
                    f"for {country}, got {len(pincode)}"
                )
        else:
            if len(pincode) < 3 or len(pincode) > 10:
                errors.append(
                    f"pincode '{pincode}' looks invalid "
                    f"(length {len(pincode)})"
                )

    return errors


def _get_shipping_country(doc: Document) -> Optional[str]:
    shipping_address = getattr(doc, "shipping_address_name", None) or getattr(doc, "customer_address", None)
    if not shipping_address:
        return None
    return frappe.db.get_value("Address", shipping_address, "country")


def _get_carrier_shipping_rule(carrier: str, country: str) -> Optional[Dict[str, str]]:
    if not carrier or not country:
        return None

    fields = ["name", "carrier_product_code", "carrier_service_code"]
    rule = frappe.get_all(
        "Carrier Shipping Rule",
        filters={
            "carrier": carrier,
            "country": country,
            "is_active": 1,
        },
        fields=fields,
        limit=1,
    )
    if rule:
        return rule[0]

    default_rule = frappe.get_all(
        "Carrier Shipping Rule",
        filters={
            "carrier": carrier,
            "apply_to_all_other_countries": 1,
            "is_active": 1,
        },
        fields=fields,
        limit=1,
    )
    if default_rule:
        return default_rule[0]

    return None


def _extract_parcel_shop_entries(payload: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []

    if isinstance(payload, dict):
        # Per documentation, the key is 'ParcelShop' (PascalCase)
        candidate_keys = ["ParcelShop", "parcelShops", "pickupPoints", "results", "data", "items"]
        for key in candidate_keys:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                entries = candidate
                break
        else:
            if payload:
                entries = [payload]  # fallback single result
    elif isinstance(payload, list):
        entries = payload

    return [entry for entry in entries if isinstance(entry, dict)]

def _format_parcel_shop(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    shop_id = (
        entry.get("parcelShopId")
        or entry.get("pudoId")
        or entry.get("pickupPointId")
        or entry.get("id")
        or entry.get("number")
    )
    if not shop_id:
        return None

    name = entry.get("name") or entry.get("company") or entry.get("contact") or _("ParcelShop")
    address_data = entry.get("address") or {}
    if isinstance(address_data, dict):
        street = " ".join(
            filter(
                None,
                [
                    address_data.get("street"),
                    address_data.get("streetNo")
                    or address_data.get("houseNumber")
                    or address_data.get("houseNo"),
                ],
            )
        )
        zip_code = address_data.get("zipCode") or address_data.get("postalCode")
        city = address_data.get("city")
        country_code = address_data.get("countryCode") or address_data.get("country")
    else:
        street = entry.get("street")
        zip_code = entry.get("zipCode") or entry.get("postalCode")
        city = entry.get("city")
        country_code = entry.get("countryCode") or entry.get("country")

    address_parts = [part for part in [street, zip_code, city, country_code] if part]
    formatted_address = ", ".join(address_parts)

    return {
        "id": shop_id,
        "name": name,
        "address": formatted_address,
        "zip_code": zip_code,
        "city": city,
        "country": country_code,
        "raw": entry,
    }


@frappe.whitelist()
def search_parcel_shops(
    address_payload: Any,
    term: Optional[str] = None,
    shipping_address_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Whitelisted function called by frontend proxy (/api/gls)
    to search for GLS ParcelShops using an address payload.
    """
    if not isinstance(address_payload, dict):
        try:
            # If called with stringified JSON, parse it
            address_payload = json.loads(address_payload)
            if not isinstance(address_payload, dict):
                raise ValueError("Payload is not a dictionary.")
        except Exception:
             frappe.log_error(f"Invalid address_payload received: {address_payload}", "GLS Search")
             frappe.throw(_("Invalid search criteria provided."), title="GLS ParcelShop Search")
             return {"results": []} # Add return here

    # Log the received payload for debugging
    # frappe.log_error(f"Received address payload for search: {address_payload}", "GLS Search Debug")

    try:
        client = GLSAPIClient.from_settings()
        # Call the updated client method that takes the dictionary
        response = client.search_parcel_shops_by_address(address_payload)
        return response # Return the results dictionary { "results": [...] }
    except GLSAPIError as e:
        frappe.log_error(title="GLS ParcelShop Search API Error", message=str(e))
        # Return empty list in the expected structure on failure
        return {"results": []}
    except Exception as e:
        frappe.log_error(title="GLS ParcelShop Search Unexpected Error", message=frappe.get_traceback())
        # Return empty list in the expected structure on failure
        return {"results": []}
    """Search GLS parcel shops using the configured GLS API."""
    
    search_limit = cint(limit) or 10
    search_term = (term or "").strip()

    # 1. Get Credentials
    try:
        credentials = gls_api._get_credentials()  # pylint: disable=protected-access
    except GLSAPIError as exc:
        frappe.throw(_("GLS credentials are not configured: {0}").format(frappe.as_unicode(exc)))

    # 2. Get URL
    # Step 12: migrated from site_config.json -> GLS Settings doctype.
    url = frappe.db.get_single_value("GLS Settings", "gls_parcelshop_search_url")
    if not url:
        frappe.throw(_("GLS Parcel Shop Search URL is not configured. Set it in GLS Settings."))

    # 3. Resolve Country Code
    resolved_country_name = (country or "").strip()
    if not resolved_country_name and shipping_address_name:
        resolved_country_name = frappe.db.get_value("Address", shipping_address_name, "country") or ""

    resolved_country_code = ""
    if resolved_country_name:
        if len(resolved_country_name) == 2:
            resolved_country_code = resolved_country_name.upper()
        else:
            # Get the 2-letter country code from the Country DocType
            country_code = frappe.db.get_value("Country", resolved_country_name, "code")
            if country_code:
                resolved_country_code = frappe.as_unicode(country_code).strip().upper()
    
    if not resolved_country_code:
        # Default to Austria if no country is found
        resolved_country_code = "AT" 

    # 4. Build Headers (from documentation)
    headers = {
        "Accept": "application/glsVersion1+json, application/json",
        "Content-Type": "application/glsVersion1+json"
    }

    # 5. Build JSON Payload
    # CRITICAL: Keys are PascalCase (case-sensitive) per GLS documentation
    payload = {
        "Street": "",
        "StreetNumber": "",
        "ZIPCode": "",
        "City": "",
        "CountryCode": resolved_country_code
    }

    # --- THIS IS THE LOGIC FIX ---
    # We must prioritize the 'term' from the search box.
    # The shipping_address_name is only used as a fallback.
    
    if search_term:
        # User typed something, use it for ZIP or City
        if search_term.isdigit():
            payload["ZIPCode"] = search_term
        else:
            payload["City"] = search_term
    elif shipping_address_name:
        # No search term, fall back to the shipping address
        search_address = frappe.get_doc("Address", shipping_address_name)
        if search_address:
            payload["Street"] = getattr(search_address, "address_line1", "") or ""
            payload["ZIPCode"] = getattr(search_address, "pincode", "") or ""
            payload["City"] = getattr(search_address, "city", "") or ""
    else:
        # No term or address, cannot search
        return {"results": []}
    # --- END OF LOGIC FIX ---

    # 6. Make POST request
    try:
        response = requests.post(
            url,
            json=payload,
            auth=(credentials.username, credentials.password),
            headers=headers,
            timeout=credentials.timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        response_text = exc.response.text if exc.response else "No Response"
        frappe.log_error(
            message=f"ParcelShop search failed for payload '{frappe.as_json(payload)}'. Response: {response_text}",
            title="GLS ParcelShop Search Error",
        )
        raise GLSAPIError(_("Unable to search GLS ParcelShops at the moment.")) from exc

    # 7. Process Response
    try:
        response_data = response.json()
    except ValueError as exc:
        frappe.log_error(
            message=f"ParcelShop API returned non-JSON payload: {response.text}",
            title="GLS ParcelShop Response Error",
        )
        raise GLSAPIError(_("Unable to parse GLS ParcelShop search response.")) from exc

    entries = _extract_parcel_shop_entries(response_data)
    formatted_results = []
    for entry in entries:
        formatted = _format_parcel_shop(entry)
        if formatted:
            formatted_results.append(formatted)
        if len(formatted_results) >= search_limit:
            break
    print(formatted_results)
    return {"results": formatted_results}
    """Search GLS parcel shops using the configured GLS API."""
    
    search_limit = cint(limit) or 10
    
    # 1. Get Credentials
    try:
        credentials = gls_api._get_credentials()  # pylint: disable=protected-access
    except GLSAPIError as exc:
        frappe.throw(_("GLS credentials are not configured: {0}").format(frappe.as_unicode(exc)))

    # 2. Get URL
    # Step 12: migrated from site_config.json -> GLS Settings doctype.
    url = frappe.db.get_single_value("GLS Settings", "gls_parcelshop_search_url")
    if not url:
        frappe.throw(_("GLS Parcel Shop Search URL is not configured. Set it in GLS Settings."))

    # 3. Resolve Country Code
    resolved_country_name = (country or "").strip()
    if not resolved_country_name and shipping_address_name:
        resolved_country_name = frappe.db.get_value("Address", shipping_address_name, "country") or ""

    resolved_country_code = ""
    if resolved_country_name:
        if len(resolved_country_name) == 2:
            resolved_country_code = resolved_country_name.upper()
        else:
            # Get the 2-letter country code from the Country DocType
            country_code = frappe.db.get_value("Country", resolved_country_name, "code")
            if country_code:
                resolved_country_code = frappe.as_unicode(country_code).strip().upper()
    
    if not resolved_country_code:
        # Default to Austria if no country is found
        resolved_country_code = "AT" 

    # 4. Build Headers (from documentation)
    headers = {
        "Accept": "application/glsVersion1+json, application/json",
        "Content-Type": "application/glsVersion1+json"
    }

    # 5. Build JSON Payload
    # CRITICAL: Keys are PascalCase (case-sensitive) per GLS documentation
    payload = {
        "Street": "",
        "StreetNumber": "",
        "ZIPCode": "",
        "City": "",
        "CountryCode": resolved_country_code
    }

    search_address = None
    if shipping_address_name:
        search_address = frappe.get_doc("Address", shipping_address_name)

    if search_address:
        # If we have a full address, use it
        payload["Street"] = getattr(search_address, "address_line1", "") or ""
        payload["ZIPCode"] = getattr(search_address, "pincode", "") or ""
        payload["City"] = getattr(search_address, "city", "") or ""
    elif term:
        # Fallback: assume 'term' is a ZIP code or city
        if term.isdigit():
            payload["ZIPCode"] = term
        else:
            payload["City"] = term
    else:
        # No address or term, cannot search
        return {"results": []}

    # 6. Make POST request
    try:
        response = requests.post(
            url,
            json=payload,
            auth=(credentials.username, credentials.password),
            headers=headers,
            timeout=credentials.timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        frappe.log_error(
            message=f"ParcelShop search failed for payload '{frappe.as_json(payload)}'. Response: {exc.response.text if exc.response else 'No Response'}",
            title="GLS ParcelShop Search Error",
        )
        raise GLSAPIError(_("Unable to search GLS ParcelShops at the moment.")) from exc

    # 7. Process Response
    try:
        response_data = response.json()
    except ValueError as exc:
        frappe.log_error(
            message=f"ParcelShop API returned non-JSON payload: {response.text}",
            title="GLS ParcelShop Response Error",
        )
        raise GLSAPIError(_("Unable to parse GLS ParcelShop search response.")) from exc

    # Use the key 'ParcelShop' from the documentation response
    entries = _extract_parcel_shop_entries(response_data)
    formatted_results = []
    for entry in entries:
        formatted = _format_parcel_shop(entry)
        if formatted:
            formatted_results.append(formatted)
        if len(formatted_results) >= search_limit:
            break

    return {"results": formatted_results}

    """Search GLS parcel shops using the configured GLS API."""
    
    search_limit = cint(limit) or 10
    
    # 1. Get Credentials
    try:
        credentials = gls_api._get_credentials()  # pylint: disable=protected-access
    except GLSAPIError as exc:
        frappe.throw(_("GLS credentials are not configured: {0}").format(frappe.as_unicode(exc)))

    # 2. Get URL
    # Step 12: migrated from site_config.json -> GLS Settings doctype.
    url = frappe.db.get_single_value("GLS Settings", "gls_parcelshop_search_url")
    if not url:
        frappe.throw(_("GLS Parcel Shop Search URL is not configured. Set it in GLS Settings."))

    # 3. Resolve Country Code (This is critical)
    resolved_country_name = (country or "").strip()
    if not resolved_country_name and shipping_address_name:
        resolved_country_name = frappe.db.get_value("Address", shipping_address_name, "country") or ""

    resolved_country_code = ""
    if resolved_country_name:
        if len(resolved_country_name) == 2:
            resolved_country_code = resolved_country_name.upper()
        else:
            country_code = frappe.db.get_value("Country", resolved_country_name, "code")
            if country_code:
                resolved_country_code = frappe.as_unicode(country_code).strip().upper()
    
    if not resolved_country_code:
        # Default to Austria if no country is found
        resolved_country_code = "AT" 

    # 4. Build Headers (from documentation)
    headers = {
        "Accept": "application/glsVersion1+json, application/json",
        "Content-Type": "application/glsVersion1+json"
    }

    # 5. Build JSON Payload
    # IMPORTANT: Initialize all keys, as the API expects a full address object.
    payload = {
        "street": "",
        "zipCode": "",
        "city": "",
        "country": resolved_country_code
    }

    search_address = None
    if shipping_address_name:
        search_address = frappe.get_doc("Address", shipping_address_name)

    if search_address:
        # If we have an address, use it
        payload["street"] = getattr(search_address, "address_line1", "") or ""
        payload["zipCode"] = getattr(search_address, "pincode", "") or ""
        payload["city"] = getattr(search_address, "city", "") or ""
    elif term:
        # Fallback: assume 'term' is a ZIP code or city
        if term.isdigit():
            payload["zipCode"] = term
        else:
            payload["city"] = term
    else:
        # No address or term, cannot search
        return {"results": []}

    # 6. Make POST request
    try:
        response = requests.post(
            url,
            json=payload,
            auth=(credentials.username, credentials.password),
            headers=headers,
            timeout=credentials.timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        frappe.log_error(
            message=f"ParcelShop search failed for payload '{frappe.as_json(payload)}': {exc}",
            title="GLS ParcelShop Search Error",
        )
        raise GLSAPIError(_("Unable to search GLS ParcelShops at the moment.")) from exc

    # 7. Process Response
    try:
        response_data = response.json()
    except ValueError as exc:
        frappe.log_error(
            message=f"ParcelShop API returned non-JSON payload: {response.text}",
            title="GLS ParcelShop Response Error",
        )
        raise GLSAPIError(_("Unable to parse GLS ParcelShop search response.")) from exc

    entries = _extract_parcel_shop_entries(response_data)
    formatted_results = []
    for entry in entries:
        formatted = _format_parcel_shop(entry)
        if formatted:
            formatted_results.append(formatted)
        if len(formatted_results) >= search_limit:
            break

    return {"results": formatted_results}
# NOTE: Make sure these helper functions exist in your file
def _extract_parcel_shop_entries(payload: Dict[str, Any]) -> list:
    return payload.get("parcel_shops", [])

def _format_parcel_shop(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not all(k in entry for k in ["id", "name", "address"]):
        return None
    return {
        "value": entry["id"],
        "label": f"{entry['name']} - {entry['address']}",
        "description": entry.get("opening_hours", "")
    }

@frappe.whitelist()
def copy_shipping_details_to_shipment(doc: Document, method: str):
    """Populate Shipment fields on insert from the linked Delivery Note and from
    the chosen Carrier Service master, and self-populate `shipment_id`.

    Runs from the ``Shipment.before_insert`` hook, so ``doc.name`` is already
    assigned by autoname (``SHIPMENT-.#####``) by the time we get here.
    """
    if doc.doctype != "Shipment":
        return

    # Mirror the auto-generated doc name into the visible `shipment_id` Data
    # field. The field ships empty and ops wants it pre-filled with the same
    # ID on every new Shipment (auto-created from DN or manually created).
    if not doc.get("shipment_id"):
        doc.shipment_id = doc.name

    # Pull DN-side defaults (carrier service link, delivery type, currency)
    # if a linked DN exists. Manual-from-desk Shipments without a DN link
    # skip this block and only get the Carrier Service translation below.
    delivery_note_name = None
    if doc.get("shipment_delivery_note"):
        for dn_row in doc.shipment_delivery_note:
            if dn_row.delivery_note:
                delivery_note_name = dn_row.delivery_note
                break

    if delivery_note_name:
        try:
            delivery_note = frappe.get_doc("Delivery Note", delivery_note_name)

            if (
                not doc.get("custom_carrier_service")
                and getattr(delivery_note, "custom_carrier_service", None)
            ):
                doc.custom_carrier_service = delivery_note.custom_carrier_service

            # Derive delivery_type from the shipping Address's is_parcel_shop
            # flag — the canonical source-of-truth (same rule the SO's
            # _sync_delivery_type_from_address and DN.on_submit's
            # _delivery_type_for_address use). Always overrides whatever was
            # prefilled, because ERPNext's stock "Create Shipment" button on
            # the DN form seeds delivery_type="Home Delivery" unconditionally,
            # which is wrong for parcel-shop orders. Delivery Note itself
            # has no delivery_type field, so reading from the Address is
            # the only sound option here.
            address_name = (
                doc.get("delivery_address_name")
                or getattr(delivery_note, "shipping_address_name", None)
                or getattr(delivery_note, "customer_address", None)
            )
            if address_name:
                is_parcel_shop = frappe.db.get_value(
                    "Address", address_name, "is_parcel_shop"
                )
                desired = "Parcelshop Delivery" if is_parcel_shop else "Home Delivery"
                if doc.delivery_type != desired:
                    doc.delivery_type = desired

        except Exception as e:
            frappe.log_error(
                title="Error copying shipping details to Shipment",
                message=(
                    f"Shipment: {doc.name}, Delivery Note: {delivery_note_name}, "
                    f"Error: {e}"
                ),
            )

    # Force currency from the Shipment's pickup_company default currency.
    # The Shipment is filed from the company's perspective, so it's always
    # quoted in the company's currency — never the DN's, customer's, or
    # party_account_currency. Guards the meta check for older ERPNext versions
    # where the Shipment doctype doesn't yet expose a `currency` field.
    pickup_company_name = doc.get("pickup_company")
    if pickup_company_name and frappe.get_meta("Shipment").has_field("currency"):
        pickup_currency = frappe.db.get_value(
            "Company", pickup_company_name, "default_currency"
        )
        if pickup_currency:
            doc.currency = pickup_currency

    # Shipment.shipment_amount comes from the linked Delivery Note's shipping
    # line item — the single place the cost lives. DN.on_submit already derives
    # it and hands it forward via the dict literal, so this hook normally finds
    # shipment_amount populated and leaves it alone. The fallback below covers
    # Shipments created from non-DN paths (e.g. manual desk entry) by deriving
    # it from the first linked DN. Purely informational: no amount is sent to
    # GLS (see gls_api._build_payload).
    if not doc.shipment_amount:
        first_dn = next(
            (
                getattr(row, "delivery_note", None)
                for row in (doc.get("shipment_delivery_note") or [])
                if getattr(row, "delivery_note", None)
            ),
            None,
        )
        if first_dn:
            from erpnext_parcel_station.parcel.events.delivery_note import (
                _shipping_charge,
            )

            doc.shipment_amount = _shipping_charge(
                frappe.get_doc("Delivery Note", first_dn)
            )

    # Translate the Carrier Service Link into ERPNext's stock `carrier` +
    # `carrier_service` Data fields, then clear `custom_carrier_service`.
    #
    # Example for a Carrier Service named "DPD-service_standard" whose master
    # has carrier="DPD":
    #     carrier                = "DPD"
    #     carrier_service        = "DPD-service_standard"   # full doc name
    #     custom_carrier_service = None                     # left empty per spec
    #
    # Label dispatch resolves the carrier via ``carrier_routing.resolve_carrier``,
    # which reads ``carrier`` first and falls back to ``carrier_service`` for
    # legacy Shipments. So clearing ``custom_carrier_service`` here is safe.
    if doc.get("custom_carrier_service"):
        cs_carrier = frappe.db.get_value(
            "Carrier Service", doc.custom_carrier_service, "carrier"
        )
        if cs_carrier and not doc.get("carrier"):
            doc.carrier = cs_carrier
        if not doc.get("carrier_service"):
            doc.carrier_service = doc.custom_carrier_service
        doc.custom_carrier_service = None

@frappe.whitelist()
def create_carrier_label_on_submit(doc, method=None):
    """
    Auto-create the carrier label on Shipment submit, then attach it to the
    source Delivery Note in-process.

    Routing is delegated to ``carrier_routing.resolve_carrier`` (shared with the
    label button, the cancel hooks and the pre-flight check), and the resolved
    carrier selects its adapter from ``_LABEL_ADAPTERS``. A carrier with no
    adapter is logged and skipped — never handed to another carrier's API.

    After a successful label call we save_file() the label bytes against the
    originating Delivery Note so the operator sees the label attached there.
    (In the single-site merged setup both apps share one DB, so the old
    HMAC-signed HTTP callback to Devich was retired in Step 5.)

    Skip conditions:
      - doc.flags.skip_gls_label_on_submit (set by parcel-side manual create flow)
      - doc.docstatus != 1
      - missing carrier creds: log + skip; never block submit on dev sites
    """
    # Skip if flag is set (e.g., from parcel station - labels are created manually there)
    if getattr(doc.flags, "skip_gls_label_on_submit", False):
        frappe.logger().info(f"Skipping automatic label creation for Shipment {doc.name} (skip flag set)")
        return

    if doc.docstatus != 1:
        return

    delivery_type = getattr(doc, "delivery_type", None)
    carrier_service_link = (
        getattr(doc, "carrier_service", None)
        or getattr(doc, "custom_carrier_service", None)
    )
    carrier = resolve_carrier(doc)
    carrier_label = carrier

    logger = frappe.logger()
    logger.info(
        f"[label-dispatch] Shipment={doc.name} carrier={carrier_label} "
        f"delivery_type={delivery_type} carrier_service={carrier_service_link}"
    )

    # Pre-flight: validate the shipping Address has the fields a carrier API
    # will actually accept. Without this the operator sees a generic "GLS API
    # Error (400): 400 Bad Request" with no clue which field is bad, and the
    # Shipment doc is already submitted by the time the call fails.
    addr_name = getattr(doc, "delivery_address_name", None)
    if addr_name:
        addr_errors = _validate_shipping_address_for_carrier(addr_name)
        if addr_errors:
            frappe.log_error(
                title=f"{carrier_label} label skipped (invalid address)",
                message=f"Shipment {doc.name} → Address {addr_name}: "
                + "; ".join(addr_errors),
            )
            from urllib.parse import quote as _urlquote
            addr_url = f"/app/address/{_urlquote(addr_name)}"
            frappe.throw(
                f"Cannot create {carrier_label} label — shipping address "
                f"<a href='{addr_url}'>{addr_name}</a> has invalid data: "
                + "; ".join(addr_errors)
                + ". Fix the address fields and resubmit."
            )

    label_adapter = _LABEL_ADAPTERS.get(carrier)
    if label_adapter is None:
        # No adapter for this carrier yet. Skip rather than fall through to
        # another carrier's API, and never block the submit.
        frappe.log_error(
            title=f"{carrier_label} label skipped (no adapter)",
            message=f"Shipment {doc.name}: no label adapter registered for carrier {carrier}.",
        )
        return

    try:
        label_info = label_adapter(doc)
    except _LabelConfigMissing as exc:
        # Carrier creds not configured locally — log and skip; do NOT block submit.
        frappe.log_error(
            title=f"{carrier_label} label skipped (config missing)",
            message=f"Shipment {doc.name}: {exc}",
        )
        return
    except Exception as exc:
        # Real failure (network, validation from carrier, parse error). Log; do NOT block submit.
        frappe.log_error(
            title=f"{carrier_label} label failed",
            message=f"Shipment {doc.name}: {type(exc).__name__}: {exc}\n{frappe.get_traceback()}",
        )
        return

    if not label_info:
        return

    # Persist the carrier-returned tracking number into the AWB Number field.
    # The AustrianPost path also sets this inline so its raw label save and
    # awb write happen atomically before this; doing it here too is a safe
    # no-op for that path and the only place the GLS path writes it. The doc
    # is at docstatus=1, so update_modified=False avoids bumping `modified`.
    tracking_number = (label_info.get("tracking_number") or "").strip()
    if tracking_number and not (getattr(doc, "awb_number", None) or "").strip():
        frappe.db.set_value(
            "Shipment", doc.name, "awb_number", tracking_number,
            update_modified=False,
        )
        doc.awb_number = tracking_number

    # Attach the label directly to the originating Delivery Note.
    # Replaces the deleted HTTP callback to Devich: in the merged single-site
    # setup both apps share one DB, so a save_file() against "Delivery Note"
    # lands the label exactly where the operator looks for it — no HTTP, no
    # HMAC, no payload reconstruction.
    dn_name = _first_delivery_note_for_shipment(doc.name)
    if not dn_name:
        logger.info(f"[label-dispatch] Shipment {doc.name} has no linked Delivery Note; skipping attach")
        return

    label_bytes = label_info.get("bytes") or b""
    if not label_bytes:
        return

    try:
        from frappe.utils.file_manager import save_file
        save_file(
            fname=label_info.get("filename") or f"{doc.name}-label",
            content=label_bytes,
            dt="Delivery Note",
            dn=dn_name,
            is_private=1,
        )
    except Exception as exc:
        # Attach is best-effort. Never block the submit.
        frappe.log_error(
            title=f"{carrier_label} label attach to Delivery Note raised",
            message=f"Shipment {doc.name}: {type(exc).__name__}: {exc}",
        )


#: Pre-rename alias. ``doc_events`` now points at create_carrier_label_on_submit;
#: this keeps any DB-stored Server Script or custom hook still referencing the old
#: name working. Safe to drop once nothing references it.
create_gls_label_on_submit = create_carrier_label_on_submit


def _is_gls_shipment(doc) -> bool:
    """True if this Shipment routes to GLS.

    Thin wrapper kept for readability at GLS-only call sites; the rules live in
    ``carrier_routing.resolve_carrier``. Do NOT use its negation to mean
    "Austrian Post" — compare against ``AUSTRIAN_POST`` explicitly instead,
    otherwise a third carrier silently inherits the Austrian Post path.
    """
    return resolve_carrier(doc) == GLS


def _gls_response_message(result) -> str:
    """Best-effort human-readable message from a GLS cancel response dict."""
    if isinstance(result, dict):
        msg = result.get("result") or result.get("message") or result.get("status")
        if msg:
            return str(msg)
        try:
            return frappe.as_json(result)
        except Exception:
            return str(result)
    return str(result) if result not in (None, "") else "OK"


def _cancel_activity(doc, text: str) -> None:
    """Add an Activity/Timeline (Comment) entry for the GLS cancellation audit
    trail. Best-effort — never let a timeline write disrupt the cancellation."""
    try:
        doc.add_comment("Info", text)
    except Exception:
        frappe.logger().warning(
            f"[gls-cancel] failed to add activity entry for {getattr(doc, 'name', None)}",
            exc_info=True,
        )


@frappe.whitelist()
def cancel_gls_label_on_cancel(doc, method=None):
    """On Shipment cancel, also cancel the parcel at GLS and record the full
    audit trail on the Activity/Timeline. Registered on ``Shipment.on_cancel``.

    Behaviour:
      - GLS shipments only — Austrian Post / other carriers are skipped.
      - No tracking ID (``awb_number``) → no API call, no audit (nothing was
        ever registered at GLS).
      - The local ERPNext cancellation ALWAYS proceeds; the GLS outcome
        (request sent → success / failure / skipped) is recorded as timeline
        entries so the audit trail shows both the local status change and
        whether GLS processed the cancellation, with the exact GLS response.
        (This is intentionally non-blocking: a "GLS cancellation failed"
        timeline entry can only persist if the cancel transaction commits, so
        we no longer abort on GLS failure — see the warning we surface instead.)
      - Errors/skips are also written to the Error Log and shown to the user.
    """
    if getattr(doc, "doctype", None) != "Shipment":
        return

    tracking_id = (getattr(doc, "awb_number", None) or "").strip()
    if not tracking_id:
        LOGGER.info(
            f"[gls-cancel] Shipment={doc.name}: no tracking ID (awb_number); "
            f"skipping GLS cancellation."
        )
        return

    if resolve_carrier(doc) != GLS:
        LOGGER.info(
            f"[gls-cancel] Shipment={doc.name}: not a GLS shipment; skipping "
            f"GLS cancellation (trackID={tracking_id})."
        )
        return

    try:
        client = GLSAPIClient.from_settings()
    except Exception as exc:
        frappe.log_error(
            title="GLS cancellation skipped (config missing)",
            message=f"Shipment {doc.name} (trackID {tracking_id}): {exc}",
        )
        _cancel_activity(doc, _(
            "GLS cancellation skipped — GLS is not configured. Parcel {0} must "
            "be cancelled manually in the GLS portal."
        ).format(tracking_id))
        frappe.msgprint(
            _(
                "GLS is not configured, so parcel {0} was NOT cancelled at GLS. "
                "Cancel it manually in the GLS portal."
            ).format(tracking_id),
            title=_("GLS cancellation skipped"),
            indicator="orange",
        )
        return

    # Audit: request sent
    _cancel_activity(doc, _("GLS shipment cancellation request sent (tracking {0})").format(tracking_id))

    try:
        result = client.cancel_shipment(tracking_id)
    except GLSNotConfiguredError as exc:
        frappe.log_error(
            title="GLS cancellation skipped (endpoint not configured)",
            message=f"Shipment {doc.name} (trackID {tracking_id}): {exc}",
        )
        _cancel_activity(doc, _(
            "GLS cancellation skipped — no Cancel Endpoint configured. Parcel "
            "{0} must be cancelled manually in the GLS portal."
        ).format(tracking_id))
        frappe.msgprint(
            _(
                "No GLS Cancel Endpoint is configured (GLS Settings → Cancel "
                "Endpoint), so parcel {0} was NOT cancelled at GLS. The ERPNext "
                "cancellation will proceed; cancel the parcel manually in the "
                "GLS portal if required."
            ).format(tracking_id),
            title=_("GLS cancellation skipped"),
            indicator="orange",
        )
        return
    except GLSAPIError as exc:
        # Non-blocking: the shipment is cancelled locally; record the GLS
        # failure (with the exact GLS response) in the timeline + Error Log so
        # ops can reconcile the parcel at GLS manually.
        frappe.log_error(
            title="GLS cancellation failed",
            message=f"Shipment {doc.name} (trackID {tracking_id}): {exc}",
        )
        _cancel_activity(doc, _("GLS cancellation failed. Response: {0}").format(str(exc)))
        frappe.msgprint(
            _(
                "GLS cancellation FAILED for tracking {0}: {1}<br><br>The "
                "shipment was still cancelled in ERPNext — reconcile the parcel "
                "at GLS manually if needed."
            ).format(tracking_id, str(exc)),
            title=_("GLS cancellation failed"),
            indicator="red",
        )
        return

    # Audit: success
    response_msg = _gls_response_message(result)
    LOGGER.info(
        f"[gls-cancel] Shipment={doc.name} trackID={tracking_id} cancelled at "
        f"GLS: {result}"
    )
    _cancel_activity(doc, _("GLS cancellation successful. Response: {0}").format(response_msg))
    frappe.msgprint(
        _("GLS shipment {0} cancelled successfully.").format(tracking_id),
        title=_("GLS cancellation"),
        indicator="green",
        alert=True,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _LabelConfigMissing(Exception):
    """Raised when carrier creds aren't configured (so we can skip gracefully on dev)."""


def _do_gls_label(doc) -> dict | None:
    """Generate a GLS label and return label bytes + tracking + filename."""
    try:
        creds = _get_credentials()  # already imported at module top
    except Exception as exc:
        raise _LabelConfigMissing(f"GLS credentials missing: {exc}")

    # Reuse the existing high-level create_label() which builds the payload,
    # calls GLS, attaches the file, and sets the tracking number.
    res = create_label(doc.name)  # imported at module top
    if not res or not res.get("file_url"):
        return None

    label_bytes, mime_type, filename = _read_attached_label(res["file_url"])
    return {
        "tracking_number": res.get("tracking_number") or "",
        "bytes": label_bytes,
        "mime_type": mime_type,
        "filename": filename,
    }


def _do_austrian_post_label(doc) -> dict | None:
    """Generate an Austrian Post label via SOAP, attach to Shipment, return label bytes."""
    from frappe.utils.file_manager import save_file
    from ..api.carrier import send_import_shipment

    # Idempotency guard: if this Shipment already has a tracking number, an
    # Austrian Post parcel was already created — never call ImportShipment again
    # (that registers a SECOND parcel + duplicate label). Reuse the attached
    # label if present. Mirrors the GLS create_label guard and request_label.
    existing = (frappe.db.get_value("Shipment", doc.name, "awb_number", for_update=True) or "").strip()
    if existing:
        frappe.logger().info(
            f"[ap-label] Shipment {doc.name} already has tracking {existing}; "
            f"skipping Austrian Post ImportShipment."
        )
        existing_url = frappe.db.get_value(
            "File",
            {"attached_to_doctype": "Shipment", "attached_to_name": doc.name, "file_name": ("like", "%.zpl")},
            "file_url",
        )
        if existing_url:
            label_bytes, mime_type, filename = _read_attached_label(existing_url)
            return {"tracking_number": existing, "bytes": label_bytes, "mime_type": mime_type, "filename": filename}
        return None

    sh = doc.as_dict()
    try:
        result = send_import_shipment(sh)
    except frappe.ValidationError as exc:
        # send_import_shipment uses frappe.throw both for missing settings AND
        # for carrier rejections (network error / SOAP Fault). Only the former
        # is a quiet "config missing" skip; a carrier rejection is a real label
        # failure and must surface as such (send_import_shipment has already
        # written the detailed "Austrian Post SOAP error" log for the fault).
        msg = str(exc)
        if "is required" in msg or "not configured" in msg:
            raise _LabelConfigMissing(msg)
        raise RuntimeError(f"Austrian Post label failed: {msg}")

    parsed = result.get("parsed", {}) or {}
    err_code = parsed.get("error_code")
    if err_code:
        raise RuntimeError(f"Austrian Post error {err_code}: {parsed.get('error_message')}")

    codes = parsed.get("codes") or []
    zpl = parsed.get("zpl")
    if not zpl:
        return None

    tracking_number = codes[0] if codes else ""
    # Name the label after the Shipment (e.g. "SHIPMENT-00126-Austria-Label.zpl")
    # rather than the raw tracking number, mirroring the GLS "-GLS-Label" convention.
    filename = f"{doc.name}-Austria-Label.zpl"
    label_bytes = zpl.encode("utf-8") if isinstance(zpl, str) else bytes(zpl)

    save_file(filename, label_bytes, "Shipment", doc.name, is_private=1)
    if tracking_number:
        frappe.db.set_value("Shipment", doc.name, "awb_number", tracking_number)
    # Activity/Timeline entry, mirroring the GLS "GLS label created. Tracking: …"
    # line so Austrian Post shipments get the same visible label-created audit.
    try:
        doc.add_comment("Info", _("Austrian Post label created. Tracking: {0}").format(tracking_number or "-"))
    except Exception:
        frappe.logger().warning(
            f"[ap-label] failed to add label-created activity entry for {doc.name}",
            exc_info=True,
        )
    frappe.db.commit()

    return {
        "tracking_number": tracking_number,
        "bytes": label_bytes,
        "mime_type": "application/octet-stream",  # ZPL2 raw
        "filename": filename,
    }


def _do_fedex_label(doc) -> dict | None:
    """Generate a FedEx label and return label bytes + tracking + filename."""
    from .fedex_api import FedExNotConfiguredError, create_label as fedex_create_label

    try:
        res = fedex_create_label(doc)
    except FedExNotConfiguredError as exc:
        raise _LabelConfigMissing(f"FedEx not configured: {exc}")

    if not res or not res.get("file_url"):
        return None

    label_bytes, mime_type, filename = _read_attached_label(res["file_url"])
    return {
        "tracking_number": res.get("tracking_number") or "",
        "bytes": label_bytes,
        "mime_type": mime_type,
        "filename": filename,
    }


#: Carrier identity -> label adapter. Every adapter takes the Shipment Document
#: and returns ``{tracking_number, bytes, mime_type, filename}`` (or None when it
#: produced no label), raising ``_LabelConfigMissing`` when the carrier is simply
#: not configured on this site. Adding a carrier means adding one entry here —
#: NOT another branch in the dispatch function.
_LABEL_ADAPTERS = {
    GLS: _do_gls_label,
    AUSTRIAN_POST: _do_austrian_post_label,
    FEDEX: _do_fedex_label,
}


def _read_attached_label(file_url: str) -> tuple[bytes, str, str]:
    """Read a File doc by file_url and return (bytes, mime_type, filename)."""
    file_doc = frappe.get_all(
        "File",
        filters={"file_url": file_url},
        fields=["name", "file_name"],
        limit=1,
    )
    if not file_doc:
        return b"", "application/octet-stream", "label"

    f = frappe.get_doc("File", file_doc[0]["name"])
    content = f.get_content()
    if isinstance(content, str):
        content = content.encode("utf-8")
    fname = f.file_name or "label"
    if fname.lower().endswith(".pdf"):
        mime = "application/pdf"
    elif fname.lower().endswith(".zpl"):
        mime = "application/octet-stream"
    else:
        mime = "application/octet-stream"
    return content, mime, fname


def _first_delivery_note_for_shipment(shipment_name: str) -> str | None:
    rows = frappe.get_all(
        "Shipment Delivery Note",
        filters={"parent": shipment_name},
        fields=["delivery_note"],
        order_by="idx asc",
        limit=1,
    )
    return rows[0]["delivery_note"] if rows else None