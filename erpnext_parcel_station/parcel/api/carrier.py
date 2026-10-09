from __future__ import annotations
from typing import Any, Dict
import re
import requests
from xml.etree import ElementTree as ET
import frappe
from frappe import _
from frappe.utils import flt

from .config import _get_ap_settings, _get_ps_settings
from .addresses import _country_code, _get_address_fields
from .core import _first_delivery_note_id
from ..shipment_contents import collect_parcel_contents


# Austrian Post SOAP operations all share ONE endpoint (Austrian Post Settings
# → SOAP Endpoint URL); the operation is selected by the SOAPAction header. The
# SOAPAction VALUES are stored in the DB (Austrian Post Settings) and fetched
# dynamically — never fixed in code — exactly like the endpoint URL. Each
# operation maps to its settings fieldname; the constants below are only a
# last-resort fallback if the field is somehow empty.
_SOAP_ACTION_FIELDS = {
    "ImportShipment": ("ap_import_soapaction", "http://post.ondot.at/IShippingService/ImportShipment"),
    "CancelShipments": ("ap_cancel_soapaction", "http://post.ondot.at/IShippingService/CancelShipments"),
}


def _soap_action(operation: str) -> str:
    """Return the quoted SOAPAction header for an operation, read from
    Austrian Post Settings (DB). Falls back to the standard value only if unset."""
    fieldname, fallback = _SOAP_ACTION_FIELDS[operation]
    ps = _get_ap_settings()
    value = (getattr(ps, fieldname, None) or "").strip() if ps else ""
    return f'"{value or fallback}"'


def _clean_field(value: Any) -> str:
    """Normalise a text value before it goes into the carrier SOAP payload.

    Strips control characters (TABs/newlines), trims, and collapses internal
    whitespace runs to a single space. Address data pulled from ERPNext often
    carries stray ``\\t`` / double spaces (e.g. ``"\\tFloridusgasse "``,
    ``"\\t1030"``); those break carrier-side validation and can trip the
    Austrian Post gateway/WAF, so we never send raw control characters.
    """
    s = str(value or "")
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)  # control chars -> space
    s = re.sub(r"\s+", " ", s).strip()      # collapse whitespace + trim
    return s


# Predefined fallback weight (kg) for the Austrian Post label. The carrier
# label must NEVER display 0, so whenever no real weight can be derived (scale
# offline / unavailable at creation, or no weight captured) we emit this value
# instead. Mirrors the kg unit used by the GLS payload (`_derive_weight`).
AUSTRIAN_POST_FALLBACK_WEIGHT_KG = 1.0


def _resolve_label_weight(sh: dict) -> float:
    """Resolve the parcel weight (kg) to print on the Austrian Post label.

    Prefers the *actual* captured weight: the live-scale value that
    ``core._apply_scale_weight`` writes to ``total_weight`` and mirrors onto the
    parcel rows. Previously this payload only read ``total_weight`` and fell
    straight back to a hard-coded ``1.0`` whenever that single field was empty —
    so a shipment that carried its weight only on the parcel rows (or whose
    weight rounded to ~0) printed the wrong value, or 0, on the label.

    Resolution order, mirroring the GLS ``_derive_weight`` reference:
      1. ``total_weight`` (scale value, or item-metadata fallback, set at creation)
      2. sum of ``shipment_parcel`` row weights (the scale value is mirrored here)
      3. line items (``weight_per_unit * qty``, else the row's ``total_weight``)

    The label must never show 0, so any result that would round to 0 at the
    label's precision (including "scale unavailable") yields the predefined
    fallback. The return value is therefore always > 0.
    """
    candidates: list[float] = [flt(sh.get("total_weight"))]

    for p in (sh.get("shipment_parcel") or []):
        get = p.get if isinstance(p, dict) else (lambda k: getattr(p, k, None))
        candidates.append(flt(get("weight")) * (flt(get("count")) or 1))

    for it in (sh.get("items") or []):
        get = it.get if isinstance(it, dict) else (lambda k: getattr(it, k, None))
        wpu, qty = flt(get("weight_per_unit")), flt(get("qty"))
        candidates.append(wpu * qty if (wpu and qty) else flt(get("total_weight")))

    weight = max((c for c in candidates if c and c > 0), default=0.0)
    # < 0.005 kg would render as "0.00" at the label's 2-decimal precision, which
    # the carrier prints as 0 — treat that (and the no-weight case) as a miss.
    if weight < 0.005:
        weight = AUSTRIAN_POST_FALLBACK_WEIGHT_KG
    return round(weight, 3)


def _parcel_dimensions(sh: dict) -> dict:
    """Return {length, width, height} (positive ints) from the first Shipment
    Parcel row, captured from the dimension barcode scan. Empty dict if none
    are set (so existing shipments without dimensions behave exactly as before).
    """
    parcels = (sh.get("shipment_parcel") if isinstance(sh, dict) else None) or []
    if not parcels:
        return {}
    p = parcels[0]
    get = (lambda k: p.get(k)) if isinstance(p, dict) else (lambda k: getattr(p, k, None))

    def _as_int(v):
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    dims = {}
    for key in ("length", "width", "height"):
        val = _as_int(get(key))
        if val:
            dims[key] = val
    return dims


# ColloArticleRow members in WSDL sequence order (ordinal-alphabetical). Emitting
# out of this order makes the carrier drop the element silently, so the payload
# builder iterates THIS list instead of a dict's insertion order. Members the
# integration does not fill are absent on purpose: ColloNumberOriginExport (export
# origin parcel number) and DeclarationOfOrigin (a formal origin declaration we
# have no data source for).
_COLLO_ARTICLE_SEQUENCE = (
    "ArticleName",
    "ArticleNumber",
    "ConsumerUnitNetWeight",
    "CountryOfOriginID",
    "CurrencyID",
    "CustomsOptionID",
    "HSTariffNumber",
    "Quantity",
    "UnitID",
    "ValueOfGoodsPerUnit",
)


# ColloArticleRow.CustomsOptionID — "Art der Sendung", the customs category of the
# consignment. Values per the carrier's "System Units / Customs Option" tables
# (PLC_API_Description): 1 Sale of goods, 2 Gift, 3 Documents, 4 Commercial Sample,
# 5 Return of goods, 6 Other. We sell goods, so 1 is the default; Austrian Post
# Settings → "Customs Option ID" overrides it (a return flow would want 5).
# Never guess this value — a wrong category is a wrong customs declaration.
DEFAULT_CUSTOMS_OPTION_ID = 1

# ColloArticleRow.UnitID expects the carrier's own unit code ("System Units" in
# PLC_API_Description) — NOT the ERPNext UOM name. An unknown code makes Austrian
# Post fail the whole ImportShipment with an opaque
# `System.Data.SqlClient.SqlException` and a NIL errorMessage: no label, no
# tracking, nothing to debug. An unmapped UOM therefore omits UnitID entirely
# (minOccurs=0 — a shipment without it is accepted) rather than guessing.
#
# Documented codes: BE bundle, BL bale compressed, BN bale uncompressed,
# BO bottle, BR bar, BX box, DMT decimetre, FTQ cubic foot, GRM gram,
# KGM kilogram, KTM kilometre, MTQ cubic metre, MTR metre, PCE piece, SA sack.
# Independently probed against the acceptance endpoint: PCE, KGM, MTQ accepted;
# "Nos", "Stk", "STK", "EA", "H87", "NPR" rejected.
#
# NOTE on pairs: "PR" is NOT in the documented unit list but IS accepted by the
# carrier, and it is what prints correctly as the unit on the customs papers — so
# pairs use PR deliberately, documentation notwithstanding. ERPNext UOMs without a
# counterpart (e.g. Litre, Set, Dozen) intentionally have no entry and omit UnitID.
_UNIT_ID_BY_UOM = {
    "nos": "PCE",
    "no": "PCE",
    "unit": "PCE",
    "units": "PCE",
    "each": "PCE",
    "piece": "PCE",
    "pieces": "PCE",
    "stk": "PCE",
    "stück": "PCE",
    "stueck": "PCE",
    "pair": "PR",
    "pairs": "PR",
    "paar": "PR",
    "kg": "KGM",
    "kilogram": "KGM",
    "kilograms": "KGM",
    "kilogramm": "KGM",
    "gram": "GRM",
    "gramm": "GRM",
    "g": "GRM",
    "meter": "MTR",
    "metre": "MTR",
    "m": "MTR",
    "decimeter": "DMT",
    "decimetre": "DMT",
    "dezimeter": "DMT",
    "kilometer": "KTM",
    "kilometre": "KTM",
    "cubic meter": "MTQ",
    "cubic metre": "MTQ",
    "kubikmeter": "MTQ",
    "cubic foot": "FTQ",
    "box": "BX",
    "karton": "BX",
    "bundle": "BE",
    "bund": "BE",
    "sack": "SA",
    "bag": "SA",
    "bottle": "BO",
    "flasche": "BO",
    "bar": "BR",
}


def _unit_id(uom: str | None) -> str | None:
    """Map an ERPNext UOM onto the carrier's unit code, or None when unknown.

    None means "do not send UnitID" — see _UNIT_ID_BY_UOM for why guessing a code
    is worse than omitting the field.
    """
    if not uom:
        return None
    return _UNIT_ID_BY_UOM.get(str(uom).strip().lower())


def _decimal(value: float) -> str:
    """Render an xs:decimal for the carrier: plain notation, no trailing zeros.

    ``str(float)`` would emit scientific notation for small values (``1e-05``),
    which the carrier's decimal parser rejects.
    """
    text = f"{flt(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _collect_article_rows(sh: dict) -> list[dict]:
    """Map the parcel contents onto the carrier's ``ColloArticleList``.

    The *what* — which rows belong to this parcel and where their value, tariff
    number and origin come from — lives in ``shipment_contents``, shared with
    every other carrier. This function only does the Austrian-Post-specific
    *shape*: ColloArticleRow member names, unit codes and field lengths.

    Returns one dict per position; keys map 1:1 onto ColloArticleRow members and
    are omitted entirely when there is no value to send.
    """
    shipment_name = sh.get("name")
    contents = collect_parcel_contents(sh)

    if not contents:
        frappe.logger().warning(
            f"[austrian-post] no article rows for shipment {shipment_name} — "
            f"ColloArticleList omitted"
        )
        return []

    # "Art der Sendung": 1 = sale of goods (see DEFAULT_CUSTOMS_OPTION_ID). The
    # setting overrides it; it is NOT auto-populated on existing installations, so
    # the code default is what actually reaches the carrier after deployment.
    ps = _get_ap_settings()
    configured = int(flt(getattr(ps, "customs_option_id", 0))) if ps else 0
    customs_option_id = configured or DEFAULT_CUSTOMS_OPTION_ID

    articles: list[dict] = []
    missing_tariff: list[str] = []
    unmapped_uoms: list[str] = []
    for row in contents:
        item_code = row.get("item_code")

        if not row.get("tariff"):
            missing_tariff.append(item_code or "?")

        article = {
            "ArticleName": _clean_field(row.get("item_name") or item_code or "")[:100],
            "ArticleNumber": _clean_field(item_code or "")[:50],
            "Quantity": flt(row.get("qty")),
        }
        unit_id = _unit_id(row.get("uom"))
        if unit_id:
            article["UnitID"] = unit_id
        elif row.get("uom"):
            unmapped_uoms.append(str(row["uom"]))
        if flt(row.get("weight_per_unit")):
            article["ConsumerUnitNetWeight"] = flt(row["weight_per_unit"])
        if row.get("origin_country"):
            article["CountryOfOriginID"] = _country_code(row["origin_country"])
        if customs_option_id:
            article["CustomsOptionID"] = customs_option_id
        if row.get("rate"):
            article["ValueOfGoodsPerUnit"] = flt(row["rate"])
            if row.get("currency"):
                article["CurrencyID"] = _clean_field(row["currency"])
        # HSTariffNumber is minOccurs=0: a missing tariff number omits the field
        # rather than blocking the shipment. Logged per shipment so the items that
        # still need master data are visible — customs can query a parcel whose
        # articles carry no tariff number.
        if row.get("tariff"):
            article["HSTariffNumber"] = _clean_field(row["tariff"])

        articles.append(article)

    if missing_tariff:
        frappe.logger().warning(
            f"[austrian-post] shipment {shipment_name}: no customs tariff number "
            f"(Item.customs_tariff_number) for {sorted(set(missing_tariff))} — "
            f"HSTariffNumber omitted for those articles"
        )
    if unmapped_uoms:
        frappe.logger().warning(
            f"[austrian-post] shipment {shipment_name}: no carrier unit code for UOM "
            f"{sorted(set(unmapped_uoms))} — UnitID omitted. Add the code to "
            f"_UNIT_ID_BY_UOM after verifying it against the acceptance endpoint."
        )

    return articles


def _required_setting(ps, fieldname: str, label: str) -> str:
    val = getattr(ps, fieldname, None)
    if not val:
        frappe.throw(f"{label} is required in Austrian Post Settings.")
    return str(val)


def _resolve_delivery_service_id(sh: dict) -> str:
    """Resolve the Austrian Post ``DeliveryServiceThirdPartyID`` (the post.at
    Service Code) from the SELECTED Carrier Service ONLY.

    - The Service Code is an opaque STRING: leading zeros are preserved (``01``
      is sent as ``01``, never coerced to ``1``).
    - There is NO global default/fallback — the legacy settings field
      ``delivery_service_third_party_id`` is no longer used as a source.
    - If no Carrier Service is selected, or the selected one has no Service Code,
      this raises a validation error (shown in the UI) so shipment creation
      cannot proceed.

    Austrian Post only — GLS builds its own payload elsewhere.
    """
    carrier_service = sh.get("custom_carrier_service") or sh.get("carrier_service")
    if not carrier_service:
        frappe.throw(
            "Service Code is required: no Carrier Service is selected for this "
            "shipment. Select an Austrian Post Carrier Service that has a Service "
            "Code configured before creating the shipment.",
            title="Service Code Required",
        )
    raw = frappe.db.get_value("Carrier Service", carrier_service, "service_code")
    # Keep as a string and trim surrounding whitespace only — never strip or
    # numerically convert leading zeros.
    service_code = "" if raw is None else str(raw).strip()
    if not service_code:
        frappe.throw(
            f"Service Code is required: the Carrier Service '{carrier_service}' has "
            "no Service Code configured. Set its Service Code before creating the "
            "Austrian Post shipment.",
            title="Service Code Required",
        )
    return service_code


def _build_import_shipment_xml(sh: dict, label_format: str = "zpl") -> str:
    """Build SOAP ImportShipment XML for the carrier."""
    # Two settings sources: Austrian Post Settings carries the API identity
    # (ClientID / Org Unit), Parcel Station Settings the carrier-independent
    # sender block used as customs contact fallback further down.
    _ap = _get_ap_settings()
    if not _ap:
        frappe.throw("Austrian Post Settings is not configured.")
    _ps = _get_ps_settings()

    client_id = _required_setting(_ap, "client_id", "Client ID")
    org_guid = _required_setting(_ap, "org_unit_guid", "Org Unit GUID")
    org_id = _required_setting(_ap, "org_unit_id", "Org Unit ID")
    # DeliveryServiceThirdPartyID = the Austrian Post Service Code, taken ONLY
    # from the selected Carrier Service (string; leading zeros preserved). No
    # global default/fallback — see _resolve_delivery_service_id.
    delivery_service_id = _resolve_delivery_service_id(sh)
    label_format_id = "100x150"
    # ZPL2 for a label printer, pdf for a station that prints through the
    # browser. For pdf the 100x150 label comes on a page of its own size (A6);
    # without PaperLayoutID the carrier puts it into the corner of an A4
    # landscape sheet. Both verified against the acceptance endpoint.
    label_language = "pdf" if label_format == "pdf" else "ZPL2"
    paper_layout = "A6" if label_format == "pdf" else None

    shipping_addr_name = (
        sh.get("delivery_address_name")
        or sh.get("shipping_address_name")
        or sh.get("customer_address")
    )
    rec_addr = _get_address_fields(shipping_addr_name)
    rec_name = sh.get("delivery_to") or sh.get("delivery_customer") or sh.get("delivery_company") or "Recipient"

    ship_addr = _get_address_fields(sh.get("pickup_address_name"))
    ship_name = sh.get("pickup") or sh.get("pickup_company") or "Shipper"

    weight = _resolve_label_weight(sh)

    soapenv = "http://schemas.xmlsoap.org/soap/envelope/"
    post = "http://post.ondot.at"

    env = ET.Element(ET.QName(soapenv, "Envelope"))
    env.set("xmlns:soapenv", soapenv)
    env.set("xmlns:post", post)

    header = ET.SubElement(env, ET.QName(soapenv, "Header"))
    body = ET.SubElement(env, ET.QName(soapenv, "Body"))

    import_shipment = ET.SubElement(body, ET.QName(post, "ImportShipment"))
    row = ET.SubElement(import_shipment, ET.QName(post, "row"))

    # ShipmentRow is a WCF DataContract: its xs:sequence is in ordinal-alphabetical
    # order and the deserializer walks it STRICTLY forward — an element that arrives
    # after a later sibling is silently SKIPPED (no SOAP fault, no errorMessage).
    # So the emit order below must match the WSDL sequence exactly:
    #   ClientID -> ColloList -> DeliveryServiceThirdPartyID -> OURecipientAddress
    #   -> OUShipperAddress -> OUShipperReference1 -> OrgUnitGuid -> OrgUnitID
    #   -> PrinterObject
    # (ColloList used to be emitted AFTER DeliveryServiceThirdPartyID, which made
    # Austrian Post drop the whole ColloList — weight and dimensions never arrived,
    # while the label was still created. Do not reorder without checking the WSDL.)
    ET.SubElement(row, ET.QName(post, "ClientID")).text = client_id

    collo_list = ET.SubElement(row, ET.QName(post, "ColloList"))
    collo_row = ET.SubElement(collo_list, ET.QName(post, "ColloRow"))
    # ColloArticleList is the FIRST member of ColloRow in the WSDL sequence, so the
    # parcel contents have to be emitted before Height/Length/Weight/Width. Each
    # ColloArticleRow keeps its own members in sequence order too (see
    # _COLLO_ARTICLE_SEQUENCE); a field the shipment has no value for is skipped
    # rather than sent empty.
    articles = _collect_article_rows(sh)
    if articles:
        article_list = ET.SubElement(collo_row, ET.QName(post, "ColloArticleList"))
        for article in articles:
            article_row = ET.SubElement(article_list, ET.QName(post, "ColloArticleRow"))
            for member in _COLLO_ARTICLE_SEQUENCE:
                if member not in article:
                    continue
                value = article[member]
                ET.SubElement(article_row, ET.QName(post, member)).text = (
                    _decimal(value) if isinstance(value, (int, float)) else str(value)
                )
    # Parcel dimensions (cm) captured from the dimension barcode scan and stored
    # on the Shipment Parcel row. Austrian Post's ColloRow accepts Length/Width/
    # Height (xs:int). Emit in the schema (alphabetical) sequence order
    # Height, Length, Weight, Width — only when a value is present, so shipments
    # without dimensions send exactly Weight as before.
    dims = _parcel_dimensions(sh)
    if dims.get("height"):
        ET.SubElement(collo_row, ET.QName(post, "Height")).text = str(dims["height"])
    if dims.get("length"):
        ET.SubElement(collo_row, ET.QName(post, "Length")).text = str(dims["length"])
    # ColloRow Weight is xs:decimal in KILOGRAMS (per the carrier WSDL). `weight`
    # is the resolved kg value (always > 0). NOTE: the carrier does NOT print this
    # declared weight on the label — it renders the depot-measured weight (0,00 kg
    # until the parcel is physically weighed). The label is printed exactly as
    # the carrier returns it; we do not rewrite it.
    ET.SubElement(collo_row, ET.QName(post, "Weight")).text = (
        f"{weight:.0f}" if weight.is_integer() else f"{weight:.2f}"
    )
    if dims.get("width"):
        ET.SubElement(collo_row, ET.QName(post, "Width")).text = str(dims["width"])

    # Shipment.description_of_content is already built from the positions at
    # creation time (core._ensure_descriptions) but was never transmitted. It is
    # the carrier's plain-text content description, alongside the structured
    # article list above. Sequence position: after ColloList, before
    # DeliveryServiceThirdPartyID.
    content_description = _clean_field(sh.get("description_of_content"))
    if content_description:
        ET.SubElement(row, ET.QName(post, "CustomsDescription")).text = content_description[:250]

    ET.SubElement(row, ET.QName(post, "DeliveryServiceThirdPartyID")).text = delivery_service_id

    recip = ET.SubElement(row, ET.QName(post, "OURecipientAddress"))
    ET.SubElement(recip, ET.QName(post, "AddressLine1")).text = _clean_field(rec_addr["address_line1"])
    ET.SubElement(recip, ET.QName(post, "City")).text = _clean_field(rec_addr["city"])
    ET.SubElement(recip, ET.QName(post, "CountryID")).text = _clean_field(rec_addr["country_code"])
    # Customs needs a recipient phone OR email as well, otherwise the carrier
    # rejects the shipment with SN#10075 ("Telefonnummer oder Emailadresse des
    # Empfängers für Zoll erforderlich"). Address record first, then the contact
    # captured on the Shipment. Sequence: Email before Name1, Tel1 after PostalCode.
    recipient_email = _clean_field(rec_addr.get("email") or sh.get("delivery_contact_email"))
    recipient_phone = _clean_field(rec_addr.get("phone"))
    if recipient_email:
        ET.SubElement(recip, ET.QName(post, "Email")).text = recipient_email
    ET.SubElement(recip, ET.QName(post, "Name1")).text = _clean_field(rec_name)
    ET.SubElement(recip, ET.QName(post, "PostalCode")).text = _clean_field(rec_addr["pincode"])
    if recipient_phone:
        ET.SubElement(recip, ET.QName(post, "Tel1")).text = recipient_phone
    if not (recipient_email or recipient_phone):
        frappe.logger().warning(
            f"[austrian-post] shipment {sh.get('name')}: no recipient email or phone "
            f"(delivery Address, or Shipment delivery contact) — customs will reject "
            f"international shipments with SN#10075"
        )

    ship = ET.SubElement(row, ET.QName(post, "OUShipperAddress"))
    ET.SubElement(ship, ET.QName(post, "AddressLine1")).text = _clean_field(ship_addr["address_line1"])
    ET.SubElement(ship, ET.QName(post, "City")).text = _clean_field(ship_addr["city"])
    ET.SubElement(ship, ET.QName(post, "CountryID")).text = _clean_field(ship_addr["country_code"])
    # Customs needs a sender phone OR email: without either, an international
    # shipment is rejected with SN#10074 ("Telefonnummer oder Emailadresse des
    # Absenders für Zoll erforderlich"). Prefer the pickup Address record, fall
    # back to the sender block in Parcel Station Settings. Sequence: Email before
    # Name1, Tel1 after PostalCode.
    sender_email = _clean_field(ship_addr.get("email") or getattr(_ps, "sender_email", ""))
    sender_phone = _clean_field(ship_addr.get("phone") or getattr(_ps, "sender_phone", ""))
    if sender_email:
        ET.SubElement(ship, ET.QName(post, "Email")).text = sender_email
    ET.SubElement(ship, ET.QName(post, "Name1")).text = _clean_field(ship_name)
    ET.SubElement(ship, ET.QName(post, "PostalCode")).text = _clean_field(ship_addr["pincode"])
    if sender_phone:
        ET.SubElement(ship, ET.QName(post, "Tel1")).text = sender_phone
    if not (sender_email or sender_phone):
        frappe.logger().warning(
            f"[austrian-post] shipment {sh.get('name')}: no sender email or phone "
            f"(pickup Address, or Parcel Station Settings sender block) — customs "
            f"will reject international shipments with SN#10074"
        )

    # The schema has NO "OUShipperReference" — only OUShipperReference1/2. The
    # unknown element was ignored by the deserializer, so the Delivery Note id
    # never reached the carrier.
    shipper_reference = _first_delivery_note_id(sh)
    if shipper_reference:
        ET.SubElement(row, ET.QName(post, "OUShipperReference1")).text = shipper_reference

    ET.SubElement(row, ET.QName(post, "OrgUnitGuid")).text = org_guid
    ET.SubElement(row, ET.QName(post, "OrgUnitID")).text = org_id
    printer = ET.SubElement(row, ET.QName(post, "PrinterObject"))
    ET.SubElement(printer, ET.QName(post, "LabelFormatID")).text = label_format_id
    ET.SubElement(printer, ET.QName(post, "LanguageID")).text = label_language
    # Sequence order matters (PrinterRow: Encoding, LabelFormatID, LanguageID,
    # PaperLayoutID): an element out of order is dropped without an error.
    if paper_layout:
        ET.SubElement(printer, ET.QName(post, "PaperLayoutID")).text = paper_layout

    xml_bytes = ET.tostring(env, encoding="utf-8", method="xml")
    return xml_bytes.decode("utf-8")


def _parse_import_response(xml_text: str) -> Dict[str, Any]:
    try:
        tree = ET.fromstring(xml_text)
        nsmap = {
            "s": "http://schemas.xmlsoap.org/soap/envelope/",
            "post": "http://post.ondot.at",
            "i": "http://www.w3.org/2001/XMLSchema-instance",
        }
        codes: list[str] = []
        number_type_ids: list[str] = []
        for row in tree.findall('.//{http://post.ondot.at}ColloCodeRow'):
            code_el = row.find('{http://post.ondot.at}Code')
            ntid_el = row.find('{http://post.ondot.at}NumberTypeID')
            if code_el is not None and code_el.text:
                codes.append(code_el.text.strip())
            if ntid_el is not None and ntid_el.text:
                number_type_ids.append(ntid_el.text.strip())

        zpl_el = tree.find('.//{http://post.ondot.at}zplLabelData')
        zpl = zpl_el.text if zpl_el is not None else None

        # pdfData carries the label itself (base64) when it was requested as pdf.
        pdf_el = tree.find('.//{http://post.ondot.at}pdfData')
        pdf_nil = False
        pdf_present = False
        pdf_data = None
        if pdf_el is not None:
            pdf_nil = pdf_el.attrib.get('{%s}nil' % nsmap['i']) == 'true'
            pdf_data = (pdf_el.text or '').strip() or None
            pdf_present = bool(pdf_data) and not pdf_nil
            if pdf_nil:
                pdf_data = None

        # shipmentDocuments carries the customs papers (CN23) as a base64 A4 PDF.
        # The carrier emits it on its own for customs destinations as soon as the
        # declaration is COMPLETE — requesting it via BusinessDocumentEntryList is
        # not needed (verified against the acceptance system). An incomplete
        # declaration (e.g. a missing HSTariffNumber -> SN#10076) yields no
        # documents even though tracking and label are returned.
        docs_el = tree.find('.//{http://post.ondot.at}shipmentDocuments')
        docs_nil = False
        docs_present = False
        docs_data = None
        if docs_el is not None:
            docs_nil = docs_el.attrib.get('{%s}nil' % nsmap['i']) == 'true'
            docs_data = (docs_el.text or '').strip() or None
            docs_present = bool(docs_data) and not docs_nil
            if docs_nil:
                docs_data = None

        err_code_el = tree.find('.//{http://post.ondot.at}errorCode')
        err_msg_el = tree.find('.//{http://post.ondot.at}errorMessage')
        err_code = (err_code_el.text or '').strip() if err_code_el is not None and err_code_el.text else None
        err_msg = (err_msg_el.text or '').strip() if err_msg_el is not None and err_msg_el.text else None

        return {
            "codes": codes,
            "number_type_id": number_type_ids[0] if number_type_ids else None,
            "zpl": zpl,
            "pdf": pdf_data,
            "pdf_present": pdf_present,
            "shipment_documents_present": docs_present,
            "shipment_documents": docs_data,
            "error_code": err_code,
            "error_message": err_msg,
        }
    except Exception:
        return {
            "codes": [],
            "number_type_id": None,
            "zpl": None,
            "pdf": None,
            "pdf_present": False,
            "shipment_documents_present": False,
            "shipment_documents": None,
            "error_code": None,
            "error_message": None,
        }


def send_import_shipment(sh: dict, timeout: int = 30, label_format: str = "zpl") -> Dict[str, Any]:
    """Send built XML to carrier, return parsed info and raw response text."""
    xml_body = _build_import_shipment_xml(sh, label_format=label_format)
    ps = _get_ap_settings()
    url = _required_setting(ps, "endpoint_url", "SOAP Endpoint URL") if ps else None
    if not url:
        frappe.throw("Austrian Post Settings is not configured.")
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": _soap_action("ImportShipment"),
    }

    # ---- Request logging (mirrors the GLS payload debug) -------------------
    # Print a readable summary of exactly what is being sent to Austrian Post,
    # plus the full SOAP request body, so the operator can verify the data and
    # which call is executed. Best-effort — never let logging break the call.
    try:
        dims = _parcel_dimensions(sh)
        summary = {
            "operation": "ImportShipment",
            "endpoint": url,
            "soap_action": _soap_action("ImportShipment").strip('"'),
            "shipping_number": sh.get("name"),
            "tracking_reference": _first_delivery_note_id(sh),
            "weight_kg": _resolve_label_weight(sh),
            "length": dims.get("length"),
            "width": dims.get("width"),
            "height": dims.get("height"),
            "recipient": sh.get("delivery_to") or sh.get("delivery_customer") or sh.get("delivery_company"),
            "recipient_address": sh.get("delivery_address_name") or sh.get("shipping_address_name"),
            "delivery_service_third_party_id_sent": _resolve_delivery_service_id(sh),
            "delivery_type": sh.get("delivery_type"),
            "carrier_service": sh.get("custom_carrier_service") or sh.get("carrier_service"),
        }
        print(
            "\n=== AUSTRIAN POST ImportShipment REQUEST ===\n"
            + frappe.as_json(summary, indent=2)
            + "\n--- SOAP body ---\n"
            + xml_body
            + "\n=== END REQUEST ===\n"
        )
        frappe.logger().info(f"[austrian-post] ImportShipment request: {frappe.as_json(summary)}")
    except Exception:
        frappe.logger().warning("[austrian-post] failed to log request summary", exc_info=True)

    try:
        resp = requests.post(url, data=xml_body.encode("utf-8"), headers=headers, timeout=timeout)
    except requests.RequestException as e:
        frappe.throw(f"Carrier request failed (network): {e}")

    # ---- Response logging --------------------------------------------------
    try:
        print(
            "\n=== AUSTRIAN POST ImportShipment RESPONSE ===\n"
            f"HTTP {resp.status_code} {resp.reason}\n"
            + (resp.text or "")[:4000]
            + "\n=== END RESPONSE ===\n"
        )
        frappe.logger().info(
            f"[austrian-post] ImportShipment response HTTP {resp.status_code}; "
            f"body[:500]={ (resp.text or '')[:500] }"
        )
    except Exception:
        frappe.logger().warning("[austrian-post] failed to log response", exc_info=True)

    # Don't `raise_for_status()` here: Austrian Post returns SOAP Faults inside
    # an HTTP 500 response, and the fault body contains the actual diagnosis
    # ("ClientID is invalid", "X cannot be parsed as type 'Guid'", etc.). Bare
    # raise_for_status drops the body and leaves the operator with a useless
    # "500 Server Error" toast. Parse first, surface the fault detail second.
    #
    # IMPORTANT: a SOAP Fault can also arrive with HTTP 200 — the Austrian Post
    # gateway/WAF returns its rejection ("The requested operation was rejected
    # … support ID: …") as a 200 + <soap:Fault> body. Gating fault handling on
    # `not resp.ok` alone let that case slip through: the Fault parsed to empty
    # fields (no Code / no zplLabelData), so the label step silently returned
    # nothing with NO error logged. So we detect a Fault regardless of status.
    fault = _extract_soap_fault(resp.text)
    if not resp.ok or fault:
        frappe.log_error(
            title="Austrian Post SOAP error",
            message=(
                f"HTTP {resp.status_code}\n"
                f"Fault: {fault or '<no SOAP fault parsed>'}\n\n"
                f"Raw response (first 2k):\n{(resp.text or '')[:2000]}\n\n"
                f"Request XML (first 2k):\n{xml_body[:2000]}"
            ),
        )
        # Surface only ONE concise line to the operator — never the raw SOAP
        # body. Prefer the SOAP fault (faultcode: faultstring); otherwise fall
        # back to the parsed Austrian Post errorCode/errorMessage, then to the
        # bare HTTP status. The full raw response is already captured in the
        # Error Log above for debugging.
        detail = fault
        if not detail:
            try:
                p = _parse_import_response(resp.text)
                parts = [p.get("error_code"), p.get("error_message")]
                detail = " ".join(str(x) for x in parts if x).strip() or None
            except Exception:
                detail = None
        detail = " ".join(
            str(detail or f"HTTP {resp.status_code} {resp.reason}").split()
        )
        if len(detail) > 250:
            detail = detail[:247].rstrip() + "..."
        frappe.throw(f"Carrier request failed: {detail}")

    parsed = _parse_import_response(resp.text)
    # NOTE: dimensions are NOT written onto the label — they are already sent to
    # Austrian Post in the ImportShipment request (ColloRow Height/Length/Width).
    # (_inject_dimensions_into_zpl was intentionally removed.)
    return {"parsed": parsed, "raw": resp.text, "request_xml": xml_body}


def _extract_soap_fault(xml_text: str) -> str | None:
    """Pull the <faultstring> (and optional <faultcode>) out of a SOAP 1.1
    Fault, or return None if the response isn't a Fault. WCF / Austrian Post
    returns ``<s:Fault><faultcode>...</faultcode><faultstring>...</faultstring></s:Fault>``;
    we return ``"<faultcode>: <faultstring>"`` so the operator gets both the
    machine code and the human-readable message in one toast.
    """
    if not xml_text:
        return None
    try:
        tree = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    # faultcode/faultstring are unqualified in SOAP 1.1.
    fc = tree.find(".//faultcode")
    fs = tree.find(".//faultstring")
    fc_text = (fc.text or "").strip() if fc is not None and fc.text else ""
    fs_text = (fs.text or "").strip() if fs is not None and fs.text else ""
    if not fc_text and not fs_text:
        return None
    return f"{fc_text}: {fs_text}" if fc_text else fs_text


# ---------------------------------------------------------------------------
# Austrian Post CancelShipments
# ---------------------------------------------------------------------------

ARR_NS = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"


def _build_cancel_shipment_xml(tracking_id: str) -> str:
    """Build the CancelShipments SOAP body for Austrian Post.

    Austrian Post requires BOTH ``Number`` (the Send Ref. Nr.) AND
    ``ColloCodeList`` to be populated with the shipment reference — sending only
    one returns ``SN#10020`` ("no shipment found with the supplied parameters").
    For these shipments the Send Ref. Nr. equals the colli/tracking code, so we
    set both to ``tracking_id``. Auth (ClientID + OrgUnitID + OrgUnitGuid) is
    carried in the body, identical to ImportShipment.
    """
    _ap = _get_ap_settings()
    if not _ap:
        frappe.throw("Austrian Post Settings is not configured.")

    client_id = _required_setting(_ap, "client_id", "Client ID")
    org_guid = _required_setting(_ap, "org_unit_guid", "Org Unit GUID")
    org_id = _required_setting(_ap, "org_unit_id", "Org Unit ID")
    track = _clean_field(tracking_id)

    soapenv = "http://schemas.xmlsoap.org/soap/envelope/"
    post = "http://post.ondot.at"

    env = ET.Element(ET.QName(soapenv, "Envelope"))
    env.set("xmlns:soapenv", soapenv)
    env.set("xmlns:post", post)
    env.set("xmlns:arr", ARR_NS)
    ET.SubElement(env, ET.QName(soapenv, "Header"))
    body = ET.SubElement(env, ET.QName(soapenv, "Body"))

    cancel = ET.SubElement(body, ET.QName(post, "CancelShipments"))
    shipments = ET.SubElement(cancel, ET.QName(post, "shipments"))
    row = ET.SubElement(shipments, ET.QName(post, "CancelShipmentRow"))

    # Order matters (WCF DataContract): ClientID, ColloCodeList, Number,
    # OrgUnitGuid, OrgUnitID.
    ET.SubElement(row, ET.QName(post, "ClientID")).text = client_id
    collo = ET.SubElement(row, ET.QName(post, "ColloCodeList"))
    ET.SubElement(collo, ET.QName(ARR_NS, "string")).text = track
    ET.SubElement(row, ET.QName(post, "Number")).text = track
    ET.SubElement(row, ET.QName(post, "OrgUnitGuid")).text = org_guid
    ET.SubElement(row, ET.QName(post, "OrgUnitID")).text = org_id

    return ET.tostring(env, encoding="utf-8", method="xml").decode("utf-8")


def _parse_cancel_response(xml_text: str) -> Dict[str, Any]:
    """Parse a CancelShipments response → success flag + error code/message."""
    out = {"success": False, "error_code": None, "error_message": None, "number": None}
    try:
        tree = ET.fromstring(xml_text)
    except Exception:
        return out
    ns = "{http://post.ondot.at}"
    ok = tree.find(f".//{ns}CancelSuccessful")
    ec = tree.find(f".//{ns}ErrorCode")
    em = tree.find(f".//{ns}ErrorMessage")
    num = tree.find(f".//{ns}Number")
    out["success"] = (ok is not None and (ok.text or "").strip().lower() == "true")
    out["error_code"] = (ec.text or "").strip() if ec is not None and ec.text else None
    out["error_message"] = (em.text or "").strip() if em is not None and em.text else None
    out["number"] = (num.text or "").strip() if num is not None and num.text else None
    return out


def send_cancel_shipment(tracking_id: str, timeout: int = 30) -> Dict[str, Any]:
    """POST a CancelShipments request to Austrian Post and return the parsed
    result: ``{success, error_code, error_message, number, message, raw}``.

    Uses the single shared ``endpoint_url`` with the ``CancelShipments``
    SOAPAction header (mapped internally in ``SOAP_ACTIONS``). SOAP Faults are
    detected on any HTTP status (the gateway can return them as HTTP 200),
    logged, and raised.
    """
    track = _clean_field(tracking_id)
    if not track:
        frappe.throw("Cannot cancel Austrian Post shipment: no tracking/reference number.")

    xml_body = _build_cancel_shipment_xml(track)
    ps = _get_ap_settings()
    # Single shared endpoint; the operation is chosen by the SOAPAction header.
    url = _required_setting(ps, "endpoint_url", "SOAP Endpoint URL") if ps else None
    if not url:
        frappe.throw("Austrian Post Settings is not configured.")

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": _soap_action("CancelShipments"),
    }
    try:
        resp = requests.post(url, data=xml_body.encode("utf-8"), headers=headers, timeout=timeout)
    except requests.RequestException as e:
        frappe.throw(f"Austrian Post cancel request failed (network): {e}")

    fault = _extract_soap_fault(resp.text)
    if not resp.ok or fault:
        frappe.log_error(
            title="Austrian Post cancel SOAP error",
            message=(
                f"HTTP {resp.status_code}\nFault: {fault or '<none>'}\n\n"
                f"Response (2k):\n{(resp.text or '')[:2000]}\n\n"
                f"Request (2k):\n{xml_body[:2000]}"
            ),
        )
        detail = fault or (resp.text or "").strip()[:400] or "no response body"
        frappe.throw(f"Austrian Post cancel failed (HTTP {resp.status_code}): {detail}")

    parsed = _parse_cancel_response(resp.text)
    if parsed["success"]:
        parsed["message"] = "CancelSuccessful=true"
    else:
        parsed["message"] = (
            f"{parsed.get('error_code') or ''} {parsed.get('error_message') or ''}".strip()
            or "CancelSuccessful=false"
        )
    parsed["raw"] = resp.text
    return parsed


def _ap_cancel_activity(doc, text: str) -> None:
    """Best-effort Activity/Timeline entry for the Austrian Post cancel audit."""
    try:
        doc.add_comment("Info", text)
    except Exception:
        frappe.logger().warning(
            f"[ap-cancel] failed to add activity entry for {getattr(doc, 'name', None)}",
            exc_info=True,
        )


@frappe.whitelist()
def cancel_austrian_post_on_cancel(doc, method=None):
    """On Shipment cancel, cancel the parcel at Austrian Post too (for non-GLS
    shipments) and record the audit trail on the Activity/Timeline.

    Carrier detection mirrors the label-dispatch side: GLS shipments are handled
    by ``cancel_gls_label_on_cancel`` and skipped here; everything else routes to
    Austrian Post. Non-blocking — the local ERPNext cancellation always proceeds;
    the Austrian Post outcome (triggered → success / failure / skipped) is logged
    to the timeline + Error Log, with the exact API response (incl. SN#... codes).
    """
    if getattr(doc, "doctype", None) != "Shipment":
        return

    # Only Austrian Post shipments belong here. This used to read "skip if GLS",
    # which meant every non-GLS carrier — including FedEx — got its parcel
    # cancelled at Austrian Post. Compare against the carrier we handle instead
    # of negating another one.
    from ..carrier_routing import AUSTRIAN_POST, resolve_carrier

    carrier = resolve_carrier(doc)
    if carrier != AUSTRIAN_POST:
        frappe.logger().info(
            f"[ap-cancel] Shipment={doc.name}: carrier={carrier}; skipping "
            f"Austrian Post cancellation."
        )
        return

    tracking_id = (getattr(doc, "awb_number", None) or "").strip()
    if not tracking_id:
        frappe.logger().info(
            f"[ap-cancel] Shipment={doc.name}: no tracking/reference number; "
            f"skipping Austrian Post cancellation."
        )
        return

    ps = _get_ap_settings()
    enabled = bool(int(getattr(ps, "enable_austrian_post_integration", 0) or 0)) if ps else False
    if not enabled:
        _ap_cancel_activity(doc, _(
            "Austrian Post integration is disabled — cancellation API not called "
            "for reference {0}. Cancel manually in the Austrian Post portal if needed."
        ).format(tracking_id))
        return

    # Audit: request triggered
    _ap_cancel_activity(doc, _("Austrian Post Cancel API triggered (Send Ref {0})").format(tracking_id))

    try:
        result = send_cancel_shipment(tracking_id)
    except Exception as exc:
        frappe.log_error(
            title="Austrian Post cancellation failed",
            message=f"Shipment {doc.name} (ref {tracking_id}): {exc}",
        )
        _ap_cancel_activity(doc, _("Austrian Post cancellation failed. Response: {0}").format(str(exc)))
        frappe.msgprint(
            _(
                "Austrian Post cancellation FAILED for reference {0}: {1}<br><br>The "
                "shipment was still cancelled in ERPNext — reconcile with Austrian "
                "Post manually if needed."
            ).format(tracking_id, str(exc)),
            title=_("Austrian Post cancellation failed"),
            indicator="red",
        )
        return

    if result.get("success"):
        _ap_cancel_activity(doc, _("Austrian Post cancellation successful. Response: {0}").format(result.get("message")))
        frappe.msgprint(
            _("Austrian Post shipment {0} cancelled successfully.").format(tracking_id),
            title=_("Austrian Post cancellation"),
            indicator="green",
            alert=True,
        )
    else:
        detail = result.get("message") or "CancelSuccessful=false"
        frappe.log_error(
            title="Austrian Post cancellation rejected",
            message=f"Shipment {doc.name} (ref {tracking_id}): {detail}",
        )
        _ap_cancel_activity(doc, _("Austrian Post cancellation failed. Response: {0}").format(detail))
        frappe.msgprint(
            _(
                "Austrian Post cancellation was REJECTED for reference {0}: {1}<br><br>"
                "The shipment was still cancelled in ERPNext — reconcile with "
                "Austrian Post manually if needed."
            ).format(tracking_id, detail),
            title=_("Austrian Post cancellation rejected"),
            indicator="red",
        )
