from __future__ import annotations
import base64
import json
import os
from typing import Any
import requests
import frappe
from frappe import _
from frappe.utils import add_days, cint, cstr, flt, nowdate, now_datetime, strip_html_tags
from frappe.utils.file_manager import save_file, get_file
from xml.etree import ElementTree as ET
from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment
from frappe.contacts.doctype.address.address import get_address_display

# Import helpers moved to modules
from .addresses import _get_address_fields, _clean_address_display, _normalize_display_from_html, _country_code
from .core import (
    _ensure_parcel_items_table,
    _record_parcel_items,
    _clear_parcel_items_for_shipment,
    _shipped_qty_by_item,
    _format_parcel_item_summary,
    _first_delivery_note_id,
    _prepare_selected_items,
    _ensure_parcel_rows,
    _ensure_descriptions,
    _ensure_pickup_details,
    create_shipment_from_barcode as _create_shipment_from_barcode,
    _resolve_delivery_note_from_barcode,
    _existing_shipments_for_delivery,
)
from .carrier import send_import_shipment
from ..carrier_routing import carrier_from_name
from ..customs import is_eu
from .. import label_formats


def _ns(tag: str) -> str:
    return tag


from ..bundles import components_by_line
from ..shipment_contents import apply_line_overrides, line_overrides, packable_item_codes, packable_non_stock_groups
from ..carrier_routing import (
    AUSTRIAN_POST,
    FEDEX,
    GLS,
    PARCELSHOP_DELIVERY_TYPE,
    carrier_from_name,
    resolve_carrier,
)


# Attachment naming for the carrier's customs papers (CN23), so the print and
# download paths can find the newest one per shipment. NOTE: Frappe's save_file
# inserts a random hash BEFORE the extension ("...-Customs-Documents9b3c83.pdf"),
# so the lookup pattern must wildcard between stem and extension — matching on
# f"%{CUSTOMS_DOCUMENTS_SUFFIX}" finds nothing.
CUSTOMS_DOCUMENTS_SUFFIX = "-Customs-Documents.pdf"
CUSTOMS_DOCUMENTS_LIKE = "%-Customs-Documents%.pdf"

def _validate_required_fields(sh: dict) -> None:
    """Validate presence of fields that the carrier requires."""
    shipping_addr_name = (
        sh.get("delivery_address_name")
        or sh.get("shipping_address_name")
        or sh.get("customer_address")
    )
    rec_addr = _get_address_fields(shipping_addr_name)
    ship_addr = _get_address_fields(sh.get("pickup_address_name"))

    missing: list[str] = []
    if not (ship_addr.get("pincode") or "").strip():
        missing.append("ShipperAddress-PostalCode")
    if not (rec_addr.get("pincode") or "").strip():
        missing.append("RecipientAddress-PostalCode")

    if missing:
        frappe.throw("Missing required fields: |" + "|".join(missing) + "|")



@frappe.whitelist()
def fetch_delivery_note_for_parcel(barcode: str | None = None) -> dict:
    logger = frappe.logger("parcel_scan")
    code = (barcode or frappe.local.form_dict.get("barcode") or "").strip()
    if not code:
        frappe.throw("Barcode value is required")

    delivery_note = _resolve_delivery_note_from_barcode(code)
    logger.info(f"[scan] lookup barcode={code!r} -> resolved Delivery Note={delivery_note}")
    if not delivery_note:
        frappe.throw(f"Delivery Note not found for barcode '{code}'")

    dn_doc = frappe.get_doc("Delivery Note", delivery_note)
    shipped_map = _shipped_qty_by_item(delivery_note)
    logger.info(
        f"[scan] DN {delivery_note} docstatus={dn_doc.docstatus} raw_items={len(dn_doc.get('items', []))}"
    )

    # The parcel-station scanning UI shows ONLY what can actually be packed.
    # A Delivery Note also carries charge lines — the freight fee, and for
    # customs destinations the pre-collected import duty — and neither has a
    # parcel to scan or a weight to pack.
    #
    # The test is Item.is_stock_item. The previous rule filtered "items
    # registered as a Carrier Service shipping_item that are also non-stock",
    # which only ever named the charges we already knew about: the import-charge
    # line was offered to the operator as if it were goods, and the next service
    # item added anywhere would have been too.
    #
    # A Product Bundle line is non-stock too, but for the opposite reason: the
    # goods are its Packed Item rows, which the list shows one by one (five
    # pairs of socks, not "1 bundle") so a split delivery records exactly what
    # went into which parcel.
    dn_rows = dn_doc.get("items", [])
    bundle_components = components_by_line(delivery_note, dn_rows)
    if bundle_components:
        logger.info(
            f"[scan] bundle lines expanded into packed components: "
            f"{ {k: len(v) for k, v in bundle_components.items()} }"
        )

    raw_codes = [
        row.item_code for row in dn_rows if row.item_code and row.name not in bundle_components
    ]
    try:
        packable = packable_item_codes(raw_codes)
    except Exception:
        logger.warning("[scan] failed building packable-item filter; showing everything", exc_info=True)
        packable = set(raw_codes)
    skipped = sorted(set(raw_codes) - packable)
    if skipped:
        logger.info(f"[scan] non-stock lines hidden from the packing list: {skipped}")

    items: list[dict[str, Any]] = []
    for row in dn_rows:
        components = bundle_components.get(row.name)
        if components:
            for comp in components:
                shipped = flt(shipped_map.get(comp["name"]))
                items.append(
                    {
                        "name": comp["name"],
                        "delivery_note_item": row.name,
                        "packed_item": comp["name"],
                        "item_code": comp["item_code"],
                        "item_name": comp["item_name"],
                        "description": comp["description"],
                        "qty": comp["qty"],
                        "shipped_qty": shipped,
                        "available_qty": max(comp["qty"] - shipped, 0),
                        "uom": comp["uom"],
                        "weight_per_unit": comp["weight_per_unit"],
                        "delivery_note": delivery_note,
                        "bundle_item_code": comp["bundle_item_code"],
                        "bundle_item_name": comp["bundle_item_name"],
                    }
                )
            continue

        if row.item_code not in packable:
            # A service line: a charge, not cargo.
            continue

        qty = flt(row.qty)
        shipped = flt(shipped_map.get(row.name))
        available = max(qty - shipped, 0)
        items.append(
            {
                "name": row.name,
                "delivery_note_item": row.name,
                "item_code": row.item_code,
                "item_name": row.item_name or row.item_code,
                "description": row.description or "",
                "qty": qty,
                "shipped_qty": shipped,
                "available_qty": available,
                "uom": row.uom,
                "weight_per_unit": flt(getattr(row, "weight_per_unit", None)),
                "delivery_note": delivery_note,
            }
        )

    # What another app says is really in the box for a line (a repair: the
    # customer's shoe instead of the service). Item code, name, description
    # and -- when the line has none -- the weight.
    try:
        apply_line_overrides(items, line_overrides(delivery_note, dn_rows))
    except Exception:
        logger.warning("[scan] line overrides failed; showing lines as they are", exc_info=True)

    logger.info(
        f"[scan] DN {delivery_note} -> returning {len(items)} packable item(s) "
        f"(hid {len(dn_doc.get('items', [])) - len(items)} service line(s))"
    )

    _ensure_parcel_items_table()
    shipments = frappe.db.sql(
        """
        select
            s.name,
            s.docstatus,
            s.status,
            s.creation,
            s.awb_number,
            group_concat(
                concat_ws('::', coalesce(psi.item_name, psi.item_code, ''), coalesce(psi.qty, 0))
                order by psi.name
                separator '||'
            ) as parcel_item_map
        from `tabShipment` s
        inner join `tabShipment Delivery Note` sdn on sdn.parent = s.name
        left join `tabParcel Shipment Item` psi on psi.shipment = s.name
        where sdn.delivery_note = %s and s.docstatus < 2
        group by s.name
        order by s.creation desc
        """,
        delivery_note,
        as_dict=True,
    )

    # Step 5 stored each GLS label as a File attached to the DN, named
    # "<shipment_name>-GLS-Label.zpl". Look up the matching file URL per
    # shipment so the parcel-station UI can show "Download Label" without an
    # extra round-trip.
    label_url_by_shipment: dict[str, str] = {}
    if shipments:
        label_files = frappe.db.get_all(
            "File",
            filters={
                "attached_to_doctype": "Delivery Note",
                "attached_to_name": delivery_note,
                "file_name": ["like", "%-Label.%"],
            },
            fields=["file_name", "file_url"],
            order_by="creation desc",
        )
        for shipment in shipments:
            sname = shipment["name"]
            for f in label_files:
                if f["file_name"].startswith(f"{sname}-"):
                    label_url_by_shipment[sname] = f["file_url"]
                    break

    for shipment in shipments:
        shipment["items"] = _format_parcel_item_summary(shipment.pop("parcel_item_map", None))
        # Labels are attached to the Shipment (older GLS ones to the Delivery
        # Note); the format tells the station which printer can take it.
        stored = label_formats.stored_label(shipment["name"])
        shipment["label_file_url"] = (
            stored.file_url if stored else label_url_by_shipment.get(shipment["name"])
        )
        shipment["label_format"] = stored.label_format if stored else label_formats.from_filename(
            shipment["label_file_url"]
        )
        shipment["tracking_number"] = shipment.get("awb_number") or None

    delivery_to = (
        dn_doc.customer_name
        if getattr(dn_doc, "customer", None)
        else getattr(dn_doc, "customer_name", "")
    )

    return {
        "delivery_note": delivery_note,
        "delivery_to": delivery_to,
        "posting_date": dn_doc.posting_date,
        "items": items,
        "shipments": shipments,
        **_delivery_note_shipping_facts(dn_doc),
        "delivery_address": (
            getattr(dn_doc, "delivery_address_display", None)
            or getattr(dn_doc, "shipping_address", None)
            or getattr(dn_doc, "address_display", None)
            or ""
        ),
        "all_items_allocated": all(item["available_qty"] <= 0 for item in items),
    }


def _address_facts(address_name: str | None) -> dict:
    """City, country and parcel-shop flag of a delivery address — what the
    station shows next to a Delivery Note. Empty values for a missing address."""
    facts = {"city": "", "country_code": "", "is_parcel_shop": 0, "customs": 0}
    if not address_name:
        return facts
    fields = ["city", "country"]
    if frappe.get_meta("Address").has_field("is_parcel_shop"):
        fields.append("is_parcel_shop")
    address = frappe.db.get_value("Address", address_name, fields, as_dict=True)
    if not address:
        return facts
    code = cstr(frappe.db.get_value("Country", address.country, "code")).upper() if address.country else ""
    facts.update(
        city=address.city or "",
        country_code=code,
        is_parcel_shop=cint(address.get("is_parcel_shop")),
        # Outside the EU customs union: the parcel needs customs papers.
        customs=0 if is_eu(code) else 1,
    )
    return facts


def _delivery_note_shipping_facts(dn_doc) -> dict:
    """How and where a Delivery Note ships: carrier, service, order numbers and
    the address facts. Display data for the station; nothing here drives the
    label call."""
    service = dn_doc.get("custom_carrier_service")
    carrier = service_name = None
    if service:
        row = frappe.db.get_value("Carrier Service", service, ["carrier", "service_name"], as_dict=True)
        if row:
            carrier, service_name = row.carrier, row.service_name
    orders = sorted(
        {row.against_sales_order for row in dn_doc.get("items", []) if row.get("against_sales_order")}
    )
    return {
        "customer_name": dn_doc.get("customer_name") or "",
        "carrier_service": service,
        "carrier": carrier,
        "carrier_key": carrier_from_name(carrier),
        "service_name": service_name,
        "sales_orders": orders,
        **_address_facts(dn_doc.get("shipping_address_name")),
    }


def _packed_today() -> int:
    """Labelled Shipments created today — the station's progress counter."""
    return frappe.db.count(
        "Shipment",
        {"docstatus": 1, "awb_number": ("is", "set"), "creation": (">=", nowdate())},
    )


#: Delivery Note statuses that no longer need packing.
_NOT_TO_PACK_STATUSES = ("Closed", "Cancelled", "Return", "Return Issued")


@frappe.whitelist(methods=["GET"])
def list_orders_to_pack(days: int = 30) -> dict:
    """Delivery Notes that still need a parcel: a Carrier Service is set, but no
    live Shipment (draft or submitted) exists — or the live Shipment never got
    a label, so the parcel cannot leave either.

    Cancelled Shipments do not count: a Delivery Note whose only Shipment was
    cancelled is back on the list. Limited to the last ``days`` days so that
    Delivery Notes from before the Parcel Station existed do not pile up.
    """
    if not frappe.has_permission("Delivery Note", "read"):
        frappe.throw(_("Not permitted to list delivery notes."), frappe.PermissionError)

    days = cint(days) or 30
    since = add_days(nowdate(), -days)
    notes = frappe.db.sql(
        """
        select
            dn.name,
            dn.posting_date,
            dn.customer_name,
            dn.status,
            dn.custom_carrier_service as carrier_service,
            dn.shipping_address_name,
            cs.service_name,
            cs.carrier,
            (select count(*) from `tabDelivery Note Item` dni where dni.parent = dn.name) as item_count,
            (select count(*) from `tabDelivery Note Item` dni
                inner join `tabItem` i on i.name = dni.item_code
                where dni.parent = dn.name
                  and (i.is_stock_item = 1
                       or i.item_group in %(goods_groups)s
                       or exists (select 1 from `tabProduct Bundle` pb where pb.new_item_code = i.name))
            ) as goods_count
        from `tabDelivery Note` dn
        left join `tabCarrier Service` cs on cs.name = dn.custom_carrier_service
        where dn.docstatus = 1
          and dn.is_return = 0
          and ifnull(dn.custom_carrier_service, '') != ''
          and dn.status not in %(excluded)s
          and dn.posting_date >= %(since)s
        order by dn.posting_date asc, dn.name asc
        """,
        {
            "excluded": _NOT_TO_PACK_STATUSES,
            "since": since,
            # Non-stock lines that are goods anyway (repairs); a placeholder
            # keeps the IN clause valid when nothing is configured.
            "goods_groups": tuple(packable_non_stock_groups()) or ("",),
        },
        as_dict=True,
    )
    if not notes:
        return {"data": [], "days": days, "packed_today": _packed_today()}

    # One row per live Shipment; the child table can carry duplicate rows for
    # the same pair (known data issue), hence DISTINCT.
    live = frappe.db.sql(
        """
        select distinct sdn.delivery_note, s.name as shipment, s.docstatus, s.awb_number
        from `tabShipment Delivery Note` sdn
        inner join `tabShipment` s on s.name = sdn.parent
        where s.docstatus < 2 and sdn.delivery_note in %(notes)s
        """,
        {"notes": [n["name"] for n in notes]},
        as_dict=True,
    )
    by_note: dict[str, list[dict]] = {}
    for row in live:
        by_note.setdefault(row["delivery_note"], []).append(row)

    result = []
    for note in notes:
        shipments = by_note.get(note["name"], [])
        labelled = [s for s in shipments if cstr(s.get("awb_number")).strip()]
        if labelled:
            continue  # packed and labelled: done
        if shipments:
            note["state"] = "label_missing"
            note["shipment"] = shipments[-1]["shipment"]
            note["shipment_docstatus"] = shipments[-1]["docstatus"]
        else:
            note["state"] = "to_pack"
            note["shipment"] = None
        note["posting_date"] = cstr(note["posting_date"])
        note["carrier_key"] = carrier_from_name(note.get("carrier"))
        note.update(_address_facts(note.pop("shipping_address_name", None)))
        result.append(note)

    return {"data": result, "days": days, "packed_today": _packed_today()}


@frappe.whitelist()
def list_shipments(limit: int = 50) -> dict:
    """Return submitted shipments with a preview of their delivery items."""

    _ensure_parcel_items_table()

    rows = frappe.db.sql(
        """
        select
            s.name,
            s.delivery_to,
            s.delivery_customer,
            s.delivery_company,
            s.delivery_address_name,
            s.delivery_address,
            s.pickup_address_name,
            s.total_weight,
            s.status,
            s.awb_number,
            fallback.items as fallback_items,
            psi.parcel_item_map
        from `tabShipment` s
        left join (
            select
                sdn.parent as shipment,
                group_concat(distinct dni.item_name order by dni.idx separator ', ') as items
            from `tabShipment Delivery Note` sdn
            left join `tabDelivery Note Item` dni on dni.parent = sdn.delivery_note
            group by sdn.parent
        ) fallback on fallback.shipment = s.name
        left join (
            select
                shipment,
                group_concat(
                    concat_ws('::', coalesce(item_name, item_code, ''), coalesce(qty, 0))
                    order by name
                    separator '||'
                ) as parcel_item_map
            from `tabParcel Shipment Item`
            group by shipment
        ) psi on psi.shipment = s.name
        where s.docstatus = 1
        order by s.creation desc
        limit %s
        """,
        (limit,),
        as_dict=True,
    )

    for row in rows:
        parcel_map = row.pop("parcel_item_map", None)
        fallback_items = row.pop("fallback_items", "")
        summary = _format_parcel_item_summary(parcel_map)
        row["items"] = summary or fallback_items

    return {"data": rows}


# address display helpers moved to `addresses.py`


@frappe.whitelist(methods=["POST"])
def update_shipment_address(
    shipment_name: str | None = None,
    address_name: str | None = None,
    address_display: str | None = None,
) -> dict:
    if not shipment_name:
        frappe.throw("shipment_name is required")

    if not frappe.db.exists("Shipment", shipment_name):
        frappe.throw(f"Shipment '{shipment_name}' not found")

    shipment = frappe.get_doc("Shipment", shipment_name)
    if shipment.docstatus != 1:
        frappe.throw("Only submitted shipments can be updated from this page.")

    address_name = (address_name or "").strip() or None
    address_display = _clean_address_display(address_display)

    if address_name and not frappe.db.exists("Address", address_name):
        frappe.throw(f"Address '{address_name}' not found")

    if address_name:
        shipment.delivery_address_name = address_name
        if not address_display:
            address_display = _normalize_display_from_html(get_address_display(address_name))

    if not address_display:
        # fall back to existing stored value
        address_display = _clean_address_display(shipment.delivery_address)

    if not address_display:
        frappe.throw("Shipping address cannot be empty.")

    shipment.delivery_address = address_display
    shipment.flags.ignore_permissions = True
    shipment.flags.ignore_validate_update_after_submit = True
    shipment.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "delivery_address_name": shipment.delivery_address_name,
        "delivery_address": shipment.delivery_address,
    }


@frappe.whitelist(methods=["GET", "POST"])
def request_label(shipment_name: str | None = None, label_format: str | None = None) -> dict:
    """
    Request a shipping label for a Shipment.

    The target carrier comes from ``carrier_routing.resolve_carrier`` — the same
    resolver the on_submit and on_cancel hooks use — and each branch below
    handles exactly one carrier. A carrier without a label adapter is rejected
    outright rather than silently falling through to Austrian Post.

    ``label_format`` is ``"zpl"`` (default) or ``"pdf"``, see
    ``parcel/label_formats.py``. It only applies when the label is created
    now: a Shipment that already has one hands that label back in the format
    it was issued in, reported as ``label_format`` in the result.
    """
    result = _request_label(shipment_name, label_format)
    return _with_label_format(result)


def _with_label_format(result: dict | None) -> dict | None:
    """Add what the stored label really is, so the station knows which kind of
    printer can take it."""
    if not result or not result.get("shipment"):
        return result
    stored = label_formats.stored_label(result["shipment"])
    result["label_format"] = stored.label_format if stored else None
    result["zpl_present"] = bool(stored and stored.label_format == label_formats.ZPL)
    result["pdf_present"] = bool(stored and stored.label_format == label_formats.PDF)
    if stored and not result.get("label_file_url"):
        result["label_file_url"] = stored.file_url
    return result


def _request_label(shipment_name: str | None = None, label_format: str | None = None) -> dict:
    if not shipment_name:
        shipment_name = frappe.local.form_dict.get("shipment_name")
    # None stays None for FedEx (its settings decide); the others default to ZPL.
    requested_format = label_format or frappe.local.form_dict.get("label_format")
    label_format = label_formats.normalize(requested_format)
    fedex_format = label_format if requested_format else None
    if not shipment_name:
        frappe.throw("shipment_name is required")

    if not frappe.db.exists("Shipment", shipment_name):
        frappe.throw(f"Shipment '{shipment_name}' not found")

    sh_doc = frappe.get_doc("Shipment", shipment_name)
    sh = sh_doc.as_dict()

    # Which carrier does this Shipment belong to? Resolved in ONE place
    # (parcel.carrier_routing) that every dispatch site shares, so the label,
    # cancel and pre-flight paths can never disagree about a Shipment.
    carrier = resolve_carrier(sh_doc)
    frappe.logger().info(f"[carrier-routing] request_label Shipment={shipment_name} carrier={carrier}")

    # GLS Integration: Use GLS API
    if carrier == GLS:
        try:
            # Import the GLS integration module
            from erpnext_parcel_station.parcel.infra.gls_api import create_label as gls_create_label
            
            frappe.logger().info(f"Creating GLS label for Shipment {shipment_name}")
            
            # This function handles all the GLS API logic, payload building, and file attachment
            label_result = gls_create_label(sh_doc, label_format=label_format)
            
            if label_result and label_result.get("tracking_number"):
                tracking_number = label_result.get("tracking_number")
                file_url = label_result.get("file_url")
                
                # Update shipment with tracking number
                frappe.db.set_value("Shipment", shipment_name, "awb_number", tracking_number)
                frappe.db.commit()
                
                frappe.logger().info(f"GLS label created successfully for Shipment {shipment_name}, Tracking: {tracking_number}")
                
                return {
                    "shipment": shipment_name,
                    "tracking_codes": [tracking_number],
                    "label_file_url": file_url,
                    "tracking_number": tracking_number,
                    "zpl_present": True,
                    "pdf_present": False,
                    "gls_label": True,
                    "carrier": "GLS"
                }
            else:
                frappe.throw("GLS API returned an invalid response")
            
        except ImportError:
            frappe.log_error(
                title="GLS Integration Not Available",
                message="The GLS integration module (erpnext_parcel_station.parcel.infra.gls_api) could not be imported."
            )
            frappe.throw(
                "GLS integration is not available. The erpnext_parcel_station GLS module failed to import.",
                title="GLS Integration Error"
            )
        except Exception as e:
            error_msg = str(e)
            frappe.log_error(
                title="GLS Label Creation Failed",
                message=f"Shipment: {shipment_name}\nError: {error_msg}\n\n{frappe.get_traceback()}"
            )
            # create_label already surfaced a single, user-friendly
            # "Failed to create GLS label: <reason>" via frappe.throw — which also
            # recorded it in Frappe's message log (_server_messages). Calling
            # frappe.throw again here appended a SECOND identical copy, so the UI
            # rendered the whole line twice. Re-raise the SAME exception instead,
            # leaving exactly one message in the log.
            if isinstance(e, frappe.ValidationError):
                raise
            frappe.throw(f"Failed to create GLS label: {error_msg}")
    
    # FedEx Integration: one REST call creates the parcel and returns the label.
    if carrier == FEDEX:
        from erpnext_parcel_station.parcel.infra.fedex_api import (
            FedExNotConfiguredError,
            create_label as fedex_create_label,
        )

        try:
            result = fedex_create_label(sh_doc, label_format=fedex_format)
        except FedExNotConfiguredError as exc:
            frappe.throw(
                _("FedEx is not configured on this site: {0}").format(exc),
                title=_("FedEx Configuration Error"),
            )

        tracking_number = (result or {}).get("tracking_number")
        if not tracking_number:
            frappe.throw(_("FedEx returned no tracking number for this shipment."))

        frappe.db.commit()
        return {
            "shipment": shipment_name,
            "tracking_codes": [tracking_number],
            "tracking_number": tracking_number,
            "label_file_url": result.get("file_url"),
            "zpl_present": bool(result.get("file_url")),
            "pdf_present": False,
            "customs_documents_url": result.get("invoice_file_url"),
            "shipment_documents_present": bool(result.get("invoice_file_url")),
            "existing": bool(result.get("existing")),
            "carrier": "FedEx",
        }

    # A resolved carrier we have no label adapter for must fail loudly. Letting
    # it fall through to the Austrian Post block below would hand the parcel to
    # the wrong carrier — the exact bug the shared resolver exists to prevent.
    if carrier != AUSTRIAN_POST:
        frappe.throw(
            _("No label integration is available for carrier {0} (Shipment {1}).").format(
                carrier, shipment_name
            )
        )

    # Austrian Post Integration (SOAP ImportShipment)
    #
    # Idempotency guard (mirrors the GLS create_label guard): if this Shipment
    # already has a tracking number, an Austrian Post parcel was already created
    # for it — calling ImportShipment again would register a SECOND parcel and
    # attach a duplicate label (the SHIPMENT-00136 case: two parcels, two .zpl
    # files seconds apart). The SELECT ... FOR UPDATE row lock also serialises
    # two concurrent request_label calls so only the first hits the API. Reuse
    # the existing tracking + attached label instead of re-calling the carrier.
    existing_awb = (frappe.db.get_value("Shipment", shipment_name, "awb_number", for_update=True) or "").strip()
    if existing_awb:
        existing_label = label_formats.stored_label(shipment_name)
        existing_url = existing_label.file_url if existing_label else None
        # The customs papers were attached by the original call — hand them back
        # too, so a re-print finds them without re-calling the carrier.
        existing_customs_url = frappe.db.get_value(
            "File",
            {
                "attached_to_doctype": "Shipment",
                "attached_to_name": shipment_name,
                "file_name": ("like", CUSTOMS_DOCUMENTS_LIKE),
            },
            "file_url",
            order_by="creation desc",
        )
        frappe.logger().info(
            f"[request_label] Shipment {shipment_name} already has tracking "
            f"{existing_awb}; reusing existing label, skipping Austrian Post ImportShipment."
        )
        return {
            "shipment": shipment_name,
            "tracking_codes": [existing_awb],
            "label_file_url": existing_url,
            "zpl_present": bool(existing_url),
            "customs_documents_url": existing_customs_url,
            "shipment_documents_present": bool(existing_customs_url),
            "existing": True,
        }

    _validate_required_fields(sh)

    result = send_import_shipment(sh, label_format=label_format)
    parsed = result.get("parsed", {})
    codes = parsed.get("codes") or []
    zpl = parsed.get("zpl")
    pdf = parsed.get("pdf")

    # Austrian Post can return a *soft* error in an HTTP 200 body (no SOAP
    # fault, so send_import_shipment did not raise): empty codes together with
    # errorCode/errorMessage. Without this guard the call returns successfully
    # with no tracking, and the UI shows a generic "no tracking number" notice
    # that hides the carrier's real reason. Surface the carrier's OWN message
    # instead — concise, single line. The full raw response is already logged
    # by send_import_shipment.
    if not codes:
        parts = [parsed.get("error_code"), parsed.get("error_message")]
        detail = " ".join(" ".join(str(x).split()) for x in parts if x)
        if len(detail) > 250:
            detail = detail[:247].rstrip() + "..."
        frappe.throw(
            f"Austrian Post: {detail}"
            if detail
            else "Austrian Post returned no tracking number for this shipment."
        )

    file_url = None
    if zpl:
        # Name the label after the Shipment (human-readable, GLS-style:
        # "SHIPMENT-00158-Austria-Label.zpl") — NOT the raw tracking / internal
        # number (e.g. 1019012500000380110309), which must not appear in the
        # label name. Mirrors the GLS "<name>-GLS-Label.zpl" convention.
        filename = f"{shipment_name}-Austria-Label.zpl"
        file_doc = save_file(filename, zpl.encode("utf-8"), "Shipment", shipment_name, is_private=1)
        file_url = file_doc.file_url
    elif pdf:
        # The label was requested as PDF (station without a label printer).
        try:
            label_pdf = base64.b64decode(pdf, validate=True)
        except Exception:
            label_pdf = None
            frappe.log_error(
                title=f"Label PDF undecodable for Shipment {shipment_name}",
                message=frappe.get_traceback(),
            )
        if label_pdf:
            file_doc = save_file(
                f"{shipment_name}-Austria-Label.pdf", label_pdf, "Shipment", shipment_name, is_private=1
            )
            file_url = file_doc.file_url

    # Customs papers (CN23) for customs destinations: the carrier returns them as a
    # base64 A4 PDF in `shipmentDocuments`, unrequested, but ONLY when the customs
    # declaration is complete (a missing HSTariffNumber yields SN#10076 and no
    # documents). Attach alongside the label so the parcel station can print and
    # download it; a broken blob must never fail an otherwise good shipment.
    customs_url = None
    if parsed.get("shipment_documents"):
        try:
            pdf_bytes = base64.b64decode(parsed["shipment_documents"], validate=True)
        except Exception:
            pdf_bytes = None
            frappe.log_error(
                title=f"Customs documents undecodable for Shipment {shipment_name}",
                message=frappe.get_traceback(),
            )
        if pdf_bytes:
            customs_doc = save_file(
                f"{shipment_name}{CUSTOMS_DOCUMENTS_SUFFIX}",
                pdf_bytes,
                "Shipment",
                shipment_name,
                is_private=1,
            )
            customs_url = customs_doc.file_url
            frappe.logger().info(
                f"[austrian-post] customs documents attached to {shipment_name} "
                f"({len(pdf_bytes)} bytes)"
            )

    if codes:
        frappe.db.set_value("Shipment", shipment_name, "awb_number", codes[0])

    frappe.db.commit()

    return {
        "shipment": shipment_name,
        "tracking_codes": codes,
        "label_file_url": file_url,
        "number_type_id": parsed.get("number_type_id"),
        "zpl_present": bool(zpl),
        "pdf_present": parsed.get("pdf_present"),
        "shipment_documents_present": parsed.get("shipment_documents_present"),
        "customs_documents_url": customs_url,
        "error_code": parsed.get("error_code"),
        "error_message": parsed.get("error_message"),
    }


@frappe.whitelist(methods=["GET"])
def list_network_printers() -> list[dict[str, Any]]:
    """Return available Network Printer Settings entries."""

    if not frappe.has_permission("Network Printer Settings", "read"):
        frappe.throw(_("Not permitted to list network printers."), frappe.PermissionError)

    printers = frappe.get_all(
        "Network Printer Settings",
        fields=["name", "printer_name", "server_ip", "port"],
        order_by="name asc",
    )

    return printers


def _load_latest_attachment(shipment_name: str, name_like: str, kind: str) -> bytes:
    """Fetch the newest attachment matching ``name_like`` from the shipment."""

    file_url = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "Shipment",
            "attached_to_name": shipment_name,
            "file_name": ("like", name_like),
        },
        "file_url",
        order_by="creation desc",
    )

    if not file_url:
        frappe.throw(
            _("No {0} found for Shipment {1}. Please request the label first.").format(
                kind, shipment_name
            )
        )

    try:
        _fname, content = get_file(file_url)
    except Exception as exc:  # pragma: no cover - defensive
        frappe.throw(_("Unable to load {0}: {1}").format(kind, exc))

    if not content:
        frappe.throw(_("The {0} file is empty.").format(kind))

    return content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")


def _load_latest_zpl(shipment_name: str) -> bytes:
    """Fetch the latest ZPL label attached to the shipment."""
    stored = label_formats.stored_label(shipment_name)
    if stored and stored.label_format == label_formats.PDF:
        # A raw queue would print the PDF's bytes as garbage.
        frappe.throw(
            _(
                "The label of Shipment {0} was issued as PDF and cannot be sent to a label printer. "
                "Print it through the PDF option (print dialog)."
            ).format(shipment_name)
        )
    return _load_latest_attachment(shipment_name, "%.zpl", _("ZPL label"))


def _file_payload(file_url: str, label_format: str | None = None) -> dict:
    """A stored attachment as base64, for the station to print it itself
    (hardware bridge or browser dialog)."""
    try:
        file_name, content = get_file(file_url)
    except Exception as exc:  # pragma: no cover - defensive
        frappe.throw(_("Unable to load the file: {0}").format(exc))
    if not content:
        frappe.throw(_("The file is empty."))
    data = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
    return {
        "file_name": file_name,
        "label_format": label_format,
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


@frappe.whitelist(methods=["GET", "POST"])
def get_shipment_label(shipment_name: str) -> dict:
    """The stored carrier label, untouched, for printing at the station.

    The station prints a ZPL label through the hardware bridge and a PDF label
    through the browser's print dialog; either way it needs the bytes exactly
    as the carrier returned them.
    """
    if not frappe.has_permission("Shipment", "read", shipment_name):
        frappe.throw(_("Not permitted to print this shipment."), frappe.PermissionError)
    stored = label_formats.stored_label(shipment_name)
    if not stored:
        frappe.throw(_("Shipment {0} has no label yet.").format(shipment_name))
    return _file_payload(stored.file_url, stored.label_format)


@frappe.whitelist(methods=["GET", "POST"])
def get_shipment_documents(shipment_name: str) -> dict:
    """The stored customs documents (A4 PDF) for printing through the browser."""
    if not frappe.has_permission("Shipment", "read", shipment_name):
        frappe.throw(_("Not permitted to print this shipment."), frappe.PermissionError)
    file_url = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "Shipment",
            "attached_to_name": shipment_name,
            "file_name": ("like", CUSTOMS_DOCUMENTS_LIKE),
        },
        "file_url",
        order_by="creation desc",
    )
    if not file_url:
        frappe.throw(_("Shipment {0} has no customs documents.").format(shipment_name))
    return _file_payload(file_url, label_formats.PDF)


def _load_latest_customs_pdf(shipment_name: str) -> bytes:
    """Fetch the latest customs-documents PDF attached to the shipment."""
    return _load_latest_attachment(
        shipment_name, CUSTOMS_DOCUMENTS_LIKE, _("customs documents")
    )


def _dispatch_print_job(
    shipment_name: str,
    printer: str,
    trigger: str,
    what: str,
    loader,
    filename_ext: str,
    mime: str,
) -> dict:
    """Send one shipment attachment to a CUPS queue.

    Shared by the label (raw ZPL to the label printer) and the customs documents
    (PDF to an A4 printer): identical permission checks, logging and CUPS error
    translation — only the payload, its MIME type and the target queue differ.

    ``trigger`` is "auto" (Auto Shipment flow) or "manual" (a Print button); it is
    logged so auto-print stays verifiable in the server/docker logs.
    """

    if not shipment_name:
        frappe.throw(_("shipment_name is required"))
    if not printer:
        frappe.throw(_("printer is required"))

    # Server-side trace — shows in `docker logs <backend>` stdout AND the bench
    # log files, so AUTO-print is verifiable even without a physical printer.
    _tag = "AUTO-PRINT" if trigger == "auto" else "MANUAL PRINT"
    print(
        f"=== PARCEL STATION {_tag} {what.upper()} === trigger={trigger} "
        f"shipment={shipment_name} printer={printer}"
    )
    frappe.logger().info(
        f"[parcel-print] what={what} trigger={trigger} shipment={shipment_name} "
        f"printer={printer}"
    )

    if not frappe.has_permission("Shipment", "read", shipment_name):
        frappe.throw(_("Not permitted to print this shipment."), frappe.PermissionError)

    if not frappe.has_permission("Network Printer Settings", "read", printer):
        frappe.throw(_("Not permitted to use this printer."), frappe.PermissionError)

    if not frappe.db.exists("Shipment", shipment_name):
        frappe.throw(_("Shipment {0} was not found.").format(shipment_name))

    printer_doc = frappe.get_doc("Network Printer Settings", printer)
    data = loader(shipment_name)

    try:
        import cups
    except ImportError as exc:
        frappe.throw(
            _("pycups is not installed on the server: {0}").format(exc),
            title=_("Printing Dependency Missing"),
        )

    job_id = None
    try:
        cups.setServer(printer_doc.server_ip)
        if printer_doc.port:
            cups.setPort(int(printer_doc.port))
        if hasattr(cups, "HTTP_ENCRYPT_ALWAYS"):
            try:
                cups.setEncryption(cups.HTTP_ENCRYPT_ALWAYS)
            except RuntimeError:
                # Ignore platforms where encryption setting is not supported
                pass

        conn = cups.Connection()
        available_printers = conn.getPrinters() or {}
        if printer_doc.printer_name not in available_printers:
            frappe.throw(
                _(
                    "Printer queue '{0}' not found on {1}:{2}. Please verify the Network Printer Settings entry."
                ).format(printer_doc.printer_name, printer_doc.server_ip, printer_doc.port)
            )

        job_title = f"Shipment-{shipment_name}-{what}"
        job_id = conn.createJob(printer_doc.printer_name, job_title, {})
        conn.startDocument(
            printer_doc.printer_name,
            job_id,
            f"{job_title}{filename_ext}",
            mime,
            True,
        )
        conn.writeRequestData(data, len(data))
        conn.finishDocument(printer_doc.printer_name)
    except cups.IPPError as exc:
        error_code = exc.args[0] if exc.args else None
        error_message = exc.args[1] if len(exc.args) > 1 else str(exc)
        if error_code == 1280:
            friendly = _(
                "CUPS reports that the printer or document path could not be found (queue '{0}' on {1}:{2})."
            ).format(printer_doc.printer_name, printer_doc.server_ip, printer_doc.port)
            frappe.throw(friendly)
        elif error_code == 1025:
            friendly = _(
                "CUPS denied access to printer '{0}' on {1}:{2}. Check CUPS authentication and printer permissions."
            ).format(printer_doc.printer_name, printer_doc.server_ip, printer_doc.port)
            frappe.throw(friendly)
        frappe.throw(_("CUPS rejected the print job: {0}").format(exc))
    except RuntimeError as exc:
        frappe.throw(_("Failed to send print job: {0}").format(exc))

    print(
        f"=== PARCEL STATION {_tag} {what.upper()} OK === job_id={job_id} "
        f"shipment={shipment_name} printer={printer_doc.printer_name} "
        f"({printer_doc.server_ip}:{printer_doc.port})"
    )
    frappe.logger().info(
        f"[parcel-print] dispatched what={what} trigger={trigger} job_id={job_id} "
        f"shipment={shipment_name} printer={printer_doc.printer_name}"
    )

    return {
        "job_id": job_id,
        "printer": printer_doc.printer_name,
        "server": printer_doc.server_ip,
        "port": printer_doc.port,
        "what": what,
    }


@frappe.whitelist(methods=["POST"])
def print_shipment_label(
    shipment_name: str | None = None,
    printer: str | None = None,
    trigger: str | None = None,
) -> dict:
    """Send the most recent ZPL label for a shipment to a configured network printer."""

    return _dispatch_print_job(
        shipment_name=shipment_name or frappe.local.form_dict.get("shipment_name"),
        printer=printer or frappe.local.form_dict.get("printer"),
        trigger=(trigger or frappe.local.form_dict.get("trigger") or "manual").strip().lower(),
        what="label",
        loader=_load_latest_zpl,
        filename_ext=".zpl",
        # Raw: the printer interprets ZPL itself, CUPS must not filter it.
        mime="application/vnd.cups-raw",
    )


@frappe.whitelist(methods=["POST"])
def print_shipment_documents(
    shipment_name: str | None = None,
    printer: str | None = None,
    trigger: str | None = None,
) -> dict:
    """Send the customs-documents PDF to a configured (A4) network printer.

    Separate queue from the label on purpose: the label goes to the ZPL label
    printer, the CN23 customs papers are A4 and need a sheet printer. Sent as
    application/pdf so CUPS renders it instead of passing bytes through raw.
    """

    return _dispatch_print_job(
        shipment_name=shipment_name or frappe.local.form_dict.get("shipment_name"),
        printer=printer or frappe.local.form_dict.get("printer"),
        trigger=(trigger or frappe.local.form_dict.get("trigger") or "manual").strip().lower(),
        what="customs-documents",
        loader=_load_latest_customs_pdf,
        filename_ext=".pdf",
        mime="application/pdf",
    )


@frappe.whitelist(methods=["POST"])
def create_shipment_from_barcode(
    barcode: str | None = None,
    items: Any = None,
    length=None,
    width=None,
    height=None,
    client_weight_kg=None,
    client_weight_source=None,
    label_format: str | None = None,
) -> dict:
    return _create_shipment_from_barcode(
        barcode=barcode,
        items=items,
        length=length,
        width=width,
        height=height,
        client_weight_kg=client_weight_kg,
        client_weight_source=client_weight_source,
        label_format=label_format,
    )

def clear_parcel_items_for_shipment(doc, method: str | None = None) -> None:
    shipment_name = getattr(doc, "name", None) or doc
    if shipment_name:
        _clear_parcel_items_for_shipment(shipment_name)


def log_cancellation_activity(doc, method: str | None = None) -> None:
    """Add an explicit, user-attributed Activity/Timeline entry when a Shipment
    is cancelled — e.g. "You cancelled this shipment · {timestamp}".

    Frappe already records the docstatus change ("Status: Submitted ->
    Cancelled") via the Version log, which renders as its own timeline line; this
    adds the clear, traceable "cancelled this shipment" activity alongside it,
    attributed to the acting user (Comment.owner) with a timestamp
    (Comment.creation). Runs inside ``Shipment.on_cancel``, so the comment only
    persists if the cancellation actually commits (a rollback drops it too).
    Best-effort: a logging failure must never block the cancellation.
    """
    if getattr(doc, "doctype", None) != "Shipment":
        return
    try:
        doc.add_comment("Cancelled", _("cancelled this shipment"))
    except Exception:
        frappe.logger().warning(
            f"[shipment-cancel] failed to add cancellation timeline entry for "
            f"{getattr(doc, 'name', None)}",
            exc_info=True,
        )


@frappe.whitelist(methods=["GET"])
def check_shipment_config(shipment_name: str) -> dict:
    """
    Diagnostic endpoint to check shipment configuration for GLS integration.
    Helps identify why GLS label creation might not be triggered.
    """
    if not frappe.db.exists("Shipment", shipment_name):
        frappe.throw(f"Shipment '{shipment_name}' not found")
    
    sh_doc = frappe.get_doc("Shipment", shipment_name)
    
    # Get delivery note info
    dn_name = _first_delivery_note_id(sh_doc.as_dict())
    dn_info = {}
    if dn_name:
        dn_doc = frappe.get_doc("Delivery Note", dn_name)
        # Step 8: parcel_shop_id now lives on the linked Address, not on the DN.
        dn_addr = getattr(dn_doc, "shipping_address_name", None) or getattr(dn_doc, "customer_address", None)
        dn_parcel_shop_id = frappe.db.get_value("Address", dn_addr, "parcel_shop_id") if dn_addr else None
        dn_info = {
            "name": dn_name,
            "delivery_type": getattr(dn_doc, "delivery_type", None),
            "custom_carrier_service": getattr(dn_doc, "custom_carrier_service", None),
            "carrier_service": getattr(dn_doc, "carrier_service", None),
            "parcel_shop_id": dn_parcel_shop_id,
        }
    
    # Get shipment info
    delivery_type = getattr(sh_doc, "delivery_type", None)
    carrier_service = getattr(sh_doc, "custom_carrier_service", None) or getattr(sh_doc, "carrier_service", None)
    
    # Check carrier service details
    carrier_info = {}
    if carrier_service:
        try:
            cs_doc = frappe.get_doc("Carrier Service", carrier_service)
            carrier_info = {
                "name": carrier_service,
                "carrier": getattr(cs_doc, "carrier", None),
                "service_code": getattr(cs_doc, "service_code", None),
                "product_code": getattr(cs_doc, "product_code", None),
            }
        except Exception as e:
            carrier_info = {"error": str(e)}
    
    # Report the SAME routing decision the label/cancel paths will make, via the
    # shared resolver — a diagnostic that computes its own answer is worse than
    # none, because it can disagree with what actually happens.
    resolved_carrier = resolve_carrier(sh_doc)
    if delivery_type == PARCELSHOP_DELIVERY_TYPE:
        detection_method = "delivery_type"
    elif carrier_from_name(getattr(sh_doc, "carrier", None)):
        detection_method = "shipment.carrier"
    elif carrier_from_name(carrier_info.get("carrier")):
        detection_method = "carrier_service"
    else:
        detection_method = "default"
    has_adapter = resolved_carrier in (GLS, AUSTRIAN_POST, FEDEX)
    
    # Step 8: parcel_shop_id now lives on the linked Address.
    sh_addr = getattr(sh_doc, "delivery_address_name", None) or getattr(sh_doc, "shipping_address_name", None)
    sh_parcel_shop_id = frappe.db.get_value("Address", sh_addr, "parcel_shop_id") if sh_addr else None
    return {
        "shipment": shipment_name,
        "shipment_config": {
            "delivery_type": delivery_type,
            "custom_carrier_service": getattr(sh_doc, "custom_carrier_service", None),
            "carrier_service": getattr(sh_doc, "carrier_service", None),
            "parcel_shop_id": sh_parcel_shop_id,
        },
        "delivery_note": dn_info,
        "carrier_service_details": carrier_info,
        "carrier_routing": {
            "carrier": resolved_carrier,
            "detection_method": detection_method,
            "has_label_adapter": has_adapter,
        },
        "recommendation": (
            f"✅ {resolved_carrier} API will be used (resolved via {detection_method})"
            if has_adapter
            else f"❌ No label integration for carrier {resolved_carrier}. "
            f"Assign a Carrier Service whose carrier has an adapter, or set "
            f"delivery_type='{PARCELSHOP_DELIVERY_TYPE}' for GLS."
        ),
    }
