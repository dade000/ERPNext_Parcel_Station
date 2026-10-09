from __future__ import annotations
from typing import Any, List, Tuple
import json
import frappe
from frappe.utils import flt, now_datetime, nowdate
from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment

from .addresses import _get_address_fields
from .scale import read_scale_weight
from .. import label_formats


# Weight sources the desk may report alongside create_shipment_from_barcode.
# "scale"  – read live from the scale via the local hardware bridge
# "manual" – typed in by the operator (no bridge / no scale at this station)
CLIENT_WEIGHT_SOURCES = {"scale": "scale (hardware bridge)", "manual": "manual entry"}
MAX_CLIENT_WEIGHT_KG = 1000.0


def _parse_client_weight(client_weight_kg=None, client_weight_source=None) -> tuple[float, str] | None:
    """Validate the weight the desk measured or the operator typed in.

    Returns ``(kg, source)`` or ``None`` when the desk sent nothing — then the
    server-side scale read and the item-metadata fallback apply as before.
    """
    if client_weight_kg in (None, ""):
        return None
    try:
        kg = float(client_weight_kg)
    except (TypeError, ValueError):
        frappe.throw(f"Invalid weight: {client_weight_kg!r}")
    if not (0 < kg <= MAX_CLIENT_WEIGHT_KG):
        frappe.throw(f"Weight must be greater than 0 and at most {MAX_CLIENT_WEIGHT_KG:g} kg (got {kg:g} kg)")
    source = (client_weight_source or "").strip().lower()
    if source not in CLIENT_WEIGHT_SOURCES:
        frappe.throw(f"Unknown weight source {client_weight_source!r} (expected: {', '.join(CLIENT_WEIGHT_SOURCES)})")
    return round(kg, 3), source


def _set_shipment_weight(shipment_doc, weight: float) -> None:
    shipment_doc.total_weight = weight
    if shipment_doc.shipment_parcel:
        for parcel in shipment_doc.shipment_parcel:
            parcel.weight = weight
    else:
        shipment_doc.append("shipment_parcel", {"weight": weight, "count": 1})


def _apply_scale_weight(shipment_doc, client_weight: tuple[float, str] | None = None) -> dict[str, Any]:
    """Read the live scale and overwrite the Shipment's weight fields.

    When the desk already measured the weight (``client_weight``, from the
    local hardware bridge or typed in by the operator), that value wins and
    the server does not contact the scale at all. Otherwise the behaviour is
    unchanged: server-side scale read, then item metadata as fallback.

    Called from `create_shipment_from_barcode` right before save/submit so
    the value flows into the GLS payload via `_derive_weight`. Best-effort:
    if the scale is offline/unreachable we keep the item-metadata weight
    that `_ensure_parcel_rows` already computed and surface the failure
    code in the API response so the operator knows what fell back.

    Returns a breakdown dict::

        {
            "source": "scale" | "item_metadata",
            "applied_weight_kg": <float>,    # what landed on the Shipment
            "fallback_weight_kg": <float>,   # what _ensure_parcel_rows seeded
            "scale": {status, weight_kg?, code?, message?, raw?},  # raw read_scale_weight result
        }

    The caller echoes this into the create-shipment API response so the
    frontend can console.log exactly where the GLS payload's weight came
    from.
    """
    fallback_weight = flt(shipment_doc.total_weight)

    if client_weight:
        kg, source = client_weight
        _set_shipment_weight(shipment_doc, kg)
        frappe.logger().info(
            f"[weight] source=client_{source} applied={kg}kg fallback={fallback_weight}kg "
            f"shipment={getattr(shipment_doc, 'name', '<new>')}"
        )
        return {
            "source": f"client_{source}",
            "applied_weight_kg": kg,
            "fallback_weight_kg": fallback_weight,
            # Shape of read_scale_weight() so existing callers keep working.
            "scale": {"status": "success", "weight_kg": kg, "source": source},
        }

    scale_result = read_scale_weight() or {}
    breakdown: dict[str, Any] = {
        "fallback_weight_kg": fallback_weight,
        "scale": scale_result,
    }

    if scale_result.get("status") == "success":
        scale_weight = flt(scale_result.get("weight_kg"))
        if scale_weight > 0:
            _set_shipment_weight(shipment_doc, scale_weight)
            breakdown["source"] = "scale"
            breakdown["applied_weight_kg"] = scale_weight
            frappe.logger().info(
                f"[weight] source=scale applied={scale_weight}kg "
                f"fallback={fallback_weight}kg "
                f"shipment={getattr(shipment_doc, 'name', '<new>')}"
            )
            return breakdown

        frappe.logger().warning(
            f"[weight] scale=success but weight_kg={scale_result.get('weight_kg')} — "
            f"keeping fallback {fallback_weight}kg"
        )
    else:
        frappe.logger().warning(
            f"[weight] scale=error code={scale_result.get('code')} "
            f"msg={scale_result.get('message')} — keeping fallback {fallback_weight}kg"
        )

    breakdown["source"] = "item_metadata"
    breakdown["applied_weight_kg"] = fallback_weight
    return breakdown


def _ensure_parcel_items_table() -> None:
    if frappe.db.table_exists("Parcel Shipment Item"):
        _ensure_parcel_items_columns()
        return

    frappe.db.sql(
        """
        CREATE TABLE `tabParcel Shipment Item` (
            `name` varchar(140) NOT NULL,
            `creation` datetime(6) NULL,
            `modified` datetime(6) NULL,
            `modified_by` varchar(140) NULL,
            `owner` varchar(140) NULL,
            `docstatus` int(1) NOT NULL DEFAULT 0,
            `idx` int(8) NOT NULL DEFAULT 0,
            `shipment` varchar(140) NULL,
            `delivery_note` varchar(140) NULL,
            `delivery_note_item` varchar(140) NULL,
            `packed_item` varchar(140) NULL,
            `item_code` varchar(140) NULL,
            `item_name` varchar(255) NULL,
            `description` text NULL,
            `qty` decimal(21,9) NOT NULL DEFAULT 0,
            `uom` varchar(140) NULL,
            `weight_per_unit` decimal(21,9) NULL,
            `total_weight` decimal(21,9) NULL,
            PRIMARY KEY (`name`),
            KEY `shipment` (`shipment`),
            KEY `delivery_note` (`delivery_note`),
            KEY `delivery_note_item` (`delivery_note_item`),
            KEY `packed_item` (`packed_item`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
        """
    )


def _ensure_parcel_items_columns() -> None:
    """Add columns that arrived after a site's table was first created.

    The table is raw SQL rather than a DocType, so ``bench migrate`` does not
    evolve it; this is the one place its shape is maintained.

    ``packed_item``: the Packed Item row a scanned line came from, when the line
    is a component of a Product Bundle. Shipped quantities are tracked under it
    (the bundle line's ``delivery_note_item`` is shared by all its components).
    """
    if frappe.db.has_column("Parcel Shipment Item", "packed_item"):
        return
    try:
        frappe.db.sql(
            "ALTER TABLE `tabParcel Shipment Item` "
            "ADD COLUMN `packed_item` varchar(140) NULL, ADD KEY `packed_item` (`packed_item`)"
        )
    except Exception as exc:  # noqa: BLE001
        # 1060: the column is already there and only the column cache (below)
        # was stale — e.g. a worker that added it while this process still
        # held the old list.
        if not frappe.db.is_duplicate_fieldname(exc) and "1060" not in str(exc):
            raise
    finally:
        # has_column() answers from a Redis-cached column list; without this
        # every later call keeps seeing the table as it was before the ALTER.
        frappe.cache.hdel("table_columns", "tabParcel Shipment Item")
    frappe.db.commit()


def _record_parcel_items(
    shipment_name: str,
    delivery_note: str,
    items: List[dict[str, Any]],
) -> None:
    if not items:
        return

    _ensure_parcel_items_table()
    now_dt = now_datetime()
    user = getattr(frappe.session, "user", None) or "Administrator"

    for idx, item in enumerate(items, start=1):
        name = frappe.generate_hash("PSI", 12)
        frappe.db.sql(
            """
            INSERT INTO `tabParcel Shipment Item`
            (`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
             `shipment`, `delivery_note`, `delivery_note_item`, `packed_item`, `item_code`, `item_name`,
             `description`, `qty`, `uom`, `weight_per_unit`, `total_weight`)
            VALUES
            (%s, %s, %s, %s, %s, 1, %s,
             %s, %s, %s, %s, %s, %s,
             %s, %s, %s, %s, %s)
            """,
            (
                name,
                now_dt,
                now_dt,
                user,
                user,
                idx,
                shipment_name,
                delivery_note,
                item.get("delivery_note_item"),
                item.get("packed_item") or None,
                item.get("item_code"),
                item.get("item_name"),
                item.get("description"),
                flt(item.get("qty")),
                item.get("uom"),
                flt(item.get("weight_per_unit")),
                flt(item.get("total_weight")),
            ),
        )


def _clear_parcel_items_for_shipment(shipment_name: str) -> None:
    if not frappe.db.table_exists("Parcel Shipment Item"):
        return
    frappe.db.sql(
        "delete from `tabParcel Shipment Item` where shipment = %s",
        (shipment_name,),
    )


def _shipped_qty_by_item(delivery_note: str) -> dict[str, float]:
    """Quantity already on a Shipment, keyed by packing-list row.

    The key is the Delivery Note Item for an ordinary line and the Packed Item
    row for a bundle component — the same ``name`` the packing list hands out,
    so ``available = qty - shipped[name]`` works for both without the caller
    knowing which kind it holds.
    """
    if not frappe.db.table_exists("Parcel Shipment Item"):
        return {}
    _ensure_parcel_items_columns()
    data = frappe.db.sql(
        """
        select coalesce(nullif(packed_item, ''), delivery_note_item) as row_key,
               sum(qty) as shipped_qty
        from `tabParcel Shipment Item`
        where delivery_note = %s
        group by row_key
        """,
        delivery_note,
        as_dict=True,
    )
    return {row.row_key: flt(row.shipped_qty) for row in data if row.row_key}


def _format_parcel_item_summary(raw: str | None) -> str:
    if not raw:
        return ""

    totals: dict[str, float] = {}
    for chunk in raw.split("||"):
        if not chunk:
            continue
        if "::" in chunk:
            label, qty_text = chunk.split("::", 1)
        else:
            label, qty_text = chunk, ""
        label = (label or "").strip()
        if not label:
            continue
        qty_val = flt(qty_text) if qty_text else 0
        totals[label] = totals.get(label, 0.0) + qty_val

    if not totals:
        return ""

    parts: list[str] = []
    for label, total in totals.items():
        if total:
            if abs(total - round(total)) < 1e-6:
                qty_display = str(int(round(total)))
            else:
                qty_display = ("{0:.3f}".format(total)).rstrip("0").rstrip(".")
            parts.append(f"{label} ({qty_display})")
        else:
            parts.append(label)

    return ", ".join(parts)


def _collapse_delivery_note_rows(shipment_doc) -> None:
    """One row per Delivery Note in the Shipment's ``shipment_delivery_note``.

    ERPNext's ``make_shipment`` maps every Delivery Note ITEM to a row of that
    table, so a Delivery Note with six lines shows up six times, each carrying
    one line's amount — freight and customs charges included. The table is
    meant to say which Delivery Notes travel in this Shipment and what they are
    worth; the rows are merged and their amounts added up.
    """
    rows = list(shipment_doc.get("shipment_delivery_note") or [])
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        note = row.get("delivery_note")
        if note in merged:
            merged[note]["grand_total"] += flt(row.get("grand_total"))
        else:
            merged[note] = {"delivery_note": note, "grand_total": flt(row.get("grand_total"))}
    if len(merged) == len(rows):
        return
    shipment_doc.set("shipment_delivery_note", [])
    for values in merged.values():
        shipment_doc.append("shipment_delivery_note", values)


def _first_delivery_note_id(sh: dict) -> str | None:
    notes = sh.get("shipment_delivery_note") or []
    if not notes:
        return None
    for row in notes:
        dn = (row.get("delivery_note") or "").strip()
        if dn:
            return dn
    return None


# The dimension scanner emits MILLIMETRES — its barcode "L:350W:250H:150" is a
# 35 x 25 x 15 cm parcel, not a 3.5 m one. Everything downstream is centimetres:
# the Shipment Parcel fields are labelled "Length (cm)", and Austrian Post's
# ColloRow carries NO unit at all (bare xs:int) and reads the numbers as cm. The
# values were previously forwarded unconverted, so a parcel was declared ten times
# too large — visible in the Post Label Center as mm-values shown as cm.
SCAN_DIMENSION_MM_PER_CM = 10.0


def _parse_scan_dimensions(length=None, width=None, height=None) -> dict | None:
    """Return {length, width, height} in CENTIMETRES from a dimension scan, or
    None if none are provided. Used to stamp the Shipment parcel row.

    Inputs are the raw scanner values in millimetres — this is the single point
    where a scan enters the domain, so converting here keeps both the ERPNext
    record and the carrier payload in the unit they document.
    """
    vals = {}
    for key, raw in (("length", length), ("width", width), ("height", height)):
        if raw in (None, ""):
            continue
        try:
            vals[key] = flt(raw) / SCAN_DIMENSION_MM_PER_CM
        except Exception:
            continue
    if vals:
        frappe.logger().info(
            f"[dimensions] scan mm -> cm: "
            f"{ {k: round(v, 1) for k, v in vals.items()} }"
        )
    return vals or None


def create_shipment_from_barcode(
    barcode: str | None = None,
    items: Any = None,
    auto_label: bool = True,
    length=None,
    width=None,
    height=None,
    client_weight_kg=None,
    client_weight_source=None,
    label_format: str | None = None,
) -> dict:
    """
    Create a shipment from a delivery note barcode.

    Args:
        barcode: Delivery note barcode/name
        items: List of items with delivery_note_item and qty
        length/width/height: raw dimension-scan values in MILLIMETRES; converted
                    to centimetres by _parse_scan_dimensions before they reach the
                    Shipment parcel row or the carrier.
        client_weight_kg / client_weight_source: weight measured in the desk
                    ("scale" via the local hardware bridge) or typed in by the
                    operator ("manual"). Skips the server-side scale read.
        label_format: "zpl" (default) or "pdf" — the format that fits the
                    printer selected at the station, see parcel/label_formats.py.
        auto_label: If True (default), automatically request a shipping label
                    AFTER the shipment is submitted. Best-effort — a label
                    failure logs an error but does not roll back the shipment.
                    Pass auto_label=false explicitly to skip label creation.
    """
    form_dict = frappe.local.form_dict or {}
    code = (barcode or form_dict.get("barcode") or "").strip()
    if not code:
        frappe.throw("Barcode value is required")
    # Reject an unknown format before anything is created.
    label_format = label_formats.normalize(label_format or form_dict.get("label_format"))
    # Validate up front so a bad weight never leaves a half-built Shipment.
    client_weight = _parse_client_weight(client_weight_kg, client_weight_source)

    # Allow explicit override from form_dict (e.g., ?auto_label=false)
    if "auto_label" in form_dict:
        auto_label = form_dict.get("auto_label") in [True, "true", "1", 1]

    raw_items = items if items is not None else form_dict.get("items")
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except Exception as exc:  # noqa: BLE001
            frappe.throw(f"Unable to parse selected items payload: {exc}")
    if not isinstance(raw_items, list):
        raw_items = []

    # Parcel dimensions from a dimension barcode scan (@:L:..W:..H:..:@).
    # Accept explicit kwargs or form_dict values; None when not a dimension scan.
    dimensions = _parse_scan_dimensions(
        length if length is not None else form_dict.get("length"),
        width if width is not None else form_dict.get("width"),
        height if height is not None else form_dict.get("height"),
    )

    delivery_note = _resolve_delivery_note_from_barcode(code)
    if not delivery_note:
        frappe.throw(f"Delivery Note not found for barcode '{code}'")

    dn_doc = frappe.get_doc("Delivery Note", delivery_note)

    # Serialize concurrent shipment creation for the same Delivery Note: this
    # row lock is held until the current request commits, so two parallel calls
    # can't both pass the idempotency check below — the second waits, then sees
    # the Shipment the first created and returns it instead of duplicating.
    frappe.db.get_value("Delivery Note", delivery_note, "name", for_update=True)

    existing = _existing_shipments_for_delivery(delivery_note)

    # Idempotency: never create a second Shipment for a Delivery Note that
    # already has a submitted one. If it already carries a tracking number it
    # is fully shipped+labelled — return it untouched (no API calls). If it was
    # submitted but the label step previously failed (no tracking), reuse that
    # same Shipment and retry ONLY the label rather than creating a duplicate.
    submitted = next((row for row in existing if row.docstatus == 1), None)
    if submitted:
        result = {
            "status": "exists",
            "delivery_note": delivery_note,
            "shipment": submitted.name,
            "docstatus": 1,
            "tracking_number": submitted.get("tracking"),
        }
        if submitted.get("tracking"):
            result["label_created"] = True
            return result
        if auto_label:
            try:
                from erpnext_parcel_station.parcel.api.shipments_ui import request_label

                label_result = request_label(submitted.name, label_format=label_format) or {}
                trk = label_result.get("tracking_number") or (
                    (label_result.get("tracking_codes") or [None])[0]
                )
                result["label_created"] = bool(trk)
                result["tracking_number"] = trk
                result["label_file_url"] = label_result.get("label_file_url")
                result["carrier"] = label_result.get("carrier", "Unknown")
            except Exception as e:
                frappe.log_error(
                    title=f"Auto-label retry failed for Shipment {submitted.name}",
                    message=f"{type(e).__name__}: {e}\n{frappe.get_traceback()}",
                )
                result["label_created"] = False
                result["label_error"] = str(e)
        return result

    # Only a NEW parcel takes quantities. This has to come after the retry
    # branch above: the items of a Shipment whose label failed are already
    # recorded on it, so checking them against "what is left" would reject the
    # very retry that is meant to finish that Shipment.
    selected_items: list[dict[str, Any]] = []
    total_amount = 0.0
    total_weight = 0.0
    if raw_items:
        selected_items, total_amount, total_weight = _prepare_selected_items(dn_doc, raw_items)
        if not selected_items:
            frappe.throw("Please select at least one item before creating a shipment.")

    delivery_addr = (
        getattr(dn_doc, "delivery_address_name", None)
        or getattr(dn_doc, "shipping_address_name", None)
        or getattr(dn_doc, "customer_address", None)
    )
    delivery_display = (
        getattr(dn_doc, "delivery_address_display", None)
        or getattr(dn_doc, "shipping_address", None)
        or getattr(dn_doc, "address_display", None)
        or ""
    )
    if delivery_addr:
        dn_doc.delivery_address_name = delivery_addr
        dn_doc.delivery_address_display = delivery_display

    # Reuse an existing draft Shipment for this Delivery Note if one is present;
    # only call make_shipment when none exists. Guarantees one Shipment per
    # Delivery Note even when items are selected (any submitted shipment has
    # already returned above).
    draft = next((row for row in existing if row.docstatus == 0), None)
    if draft:
        shipment_doc = frappe.get_doc("Shipment", draft.name)
        action = "submitted"
    else:
        shipment_doc = make_shipment(delivery_note)
        action = "created"
    _collapse_delivery_note_rows(shipment_doc)

    if delivery_addr:
        shipment_doc.delivery_address_name = delivery_addr
    if delivery_display:
        shipment_doc.delivery_address = delivery_display
    
    # Copy GLS-related fields from Delivery Note to Shipment
    if hasattr(dn_doc, "custom_carrier_service") and dn_doc.custom_carrier_service:
        shipment_doc.custom_carrier_service = dn_doc.custom_carrier_service
    elif hasattr(dn_doc, "carrier_service") and dn_doc.carrier_service:
        shipment_doc.custom_carrier_service = dn_doc.carrier_service
    else:
        # If no carrier service is set, try to find a default GLS carrier service
        delivery_type = getattr(dn_doc, "delivery_type", None)
        if delivery_type == "Parcelshop Delivery":
            # Try to find a GLS carrier service
            gls_carrier_service = frappe.db.get_value(
                "Carrier Service",
                {"carrier": "GLS", "is_active": 1},
                "name",
                order_by="creation asc"
            )
            if gls_carrier_service:
                frappe.logger().info(f"Auto-assigning GLS Carrier Service: {gls_carrier_service}")
                shipment_doc.custom_carrier_service = gls_carrier_service
            else:
                frappe.logger().warning(
                    "Delivery Note has delivery_type='ParcelShop Delivery' but no GLS Carrier Service found. "
                    "Please create a Carrier Service with carrier='GLS'"
                )
    
    if hasattr(dn_doc, "delivery_type"):
        shipment_doc.delivery_type = dn_doc.delivery_type
    # Step 8: parcel-shop info now lives on the linked Address record
    # (is_parcel_shop / parcel_shop_id / parcel_shop_carrier on Address).
    # The Shipment naturally inherits it via delivery_address_name, so no copy.

    # Austrian Post: the DeliveryServiceThirdPartyID sent to the carrier is the
    # selected Carrier Service's Service Code. Validate it is configured BEFORE
    # the shipment is created/submitted, so creation aborts with a clear UI error
    # when it's missing (the Shipment is committed before the label call below,
    # so a later failure could not undo it). Reuses the single source of truth in
    # carrier._resolve_delivery_service_id (which frappe.throws when absent).
    #
    # This check is Austrian-Post-SPECIFIC and must be gated on that carrier, not
    # on "not GLS": other carriers identify their service differently (GLS uses
    # its own product code, FedEx a serviceType), so demanding an Austrian Post
    # service ID from them would block shipment creation outright.
    if auto_label:
        from erpnext_parcel_station.parcel.carrier_routing import (
            AUSTRIAN_POST,
            resolve_carrier,
        )

        if resolve_carrier(shipment_doc) == AUSTRIAN_POST:
            from erpnext_parcel_station.parcel.api.carrier import (
                _resolve_delivery_service_id,
            )

            _resolve_delivery_service_id(shipment_doc.as_dict())

    if selected_items:
        if shipment_doc.shipment_delivery_note:
            shipment_doc.shipment_delivery_note[0].grand_total = total_amount or shipment_doc.shipment_delivery_note[0].grand_total
        if total_amount:
            shipment_doc.value_of_goods = total_amount
        _ensure_parcel_rows(shipment_doc, dn_doc, selected_items, dimensions=dimensions)
        if total_weight:
            shipment_doc.total_weight = total_weight
        _ensure_descriptions(shipment_doc, dn_doc, selected_items)
    else:
        _ensure_parcel_rows(shipment_doc, dn_doc, dimensions=dimensions)
        _ensure_descriptions(shipment_doc, dn_doc)

    _ensure_pickup_details(shipment_doc, dn_doc)

    # Live scale read — must run AFTER _ensure_parcel_rows (which seeds
    # weight from item metadata as the fallback) and BEFORE save, so the
    # scale value flows into the GLS payload via _derive_weight.
    weight_breakdown = _apply_scale_weight(shipment_doc, client_weight)

    shipment_doc.flags.ignore_permissions = True
    # Skip the on_submit-driven label creation — we'll trigger it explicitly below
    # so a label failure can be logged without rolling back the shipment itself.
    shipment_doc.flags.skip_gls_label_on_submit = True

    if shipment_doc.is_new():
        shipment_doc.insert(ignore_permissions=True)
    else:
        shipment_doc.save(ignore_permissions=True)

    if shipment_doc.docstatus == 0:
        shipment_doc.submit()
        action = "submitted" if action == "created" else action

    if selected_items:
        _record_parcel_items(shipment_doc.name, delivery_note, selected_items)

    # Leave a trace of where the label weight came from: after the fact,
    # "scale" vs "typed in" vs "article weights" is otherwise invisible.
    shipment_doc.add_comment(
        "Info",
        "Weight {0} kg from {1}".format(
            flt(weight_breakdown.get("applied_weight_kg"), 3),
            {
                "client_scale": CLIENT_WEIGHT_SOURCES["scale"],
                "client_manual": CLIENT_WEIGHT_SOURCES["manual"],
                "scale": "scale (server)",
            }.get(weight_breakdown.get("source"), "article weights (no scale reading)"),
        ),
    )

    frappe.db.commit()

    result = {
        "status": action,
        "delivery_note": delivery_note,
        "shipment": shipment_doc.name,
        "docstatus": shipment_doc.docstatus,
        "weight": weight_breakdown,
        "scale": weight_breakdown.get("scale"),
        "weight_kg": flt(shipment_doc.total_weight),
    }

    # Auto-create the carrier label AFTER submit (best-effort).
    # Works for both GLS (ParcelShop) and Austrian Post — request_label
    # routes to the right backend internally.
    if auto_label:
        try:
            from erpnext_parcel_station.parcel.api.shipments_ui import request_label

            frappe.logger().info(f"Auto-creating label for Shipment {shipment_doc.name}")
            label_result = request_label(shipment_doc.name, label_format=label_format) or {}

            tracking = label_result.get("tracking_number") or (
                (label_result.get("tracking_codes") or [None])[0]
            )
            result["label_created"] = bool(tracking)
            result["tracking_number"] = tracking
            result["label_file_url"] = label_result.get("label_file_url")
            result["carrier"] = label_result.get("carrier", "Unknown")
        except Exception as e:
            # Log but DON'T roll back the shipment — user can retry via
            # /api/method/erpnext_parcel_station.parcel.api.shipments_ui.request_label
            frappe.log_error(
                title=f"Auto-label failed for Shipment {shipment_doc.name}",
                message=f"{type(e).__name__}: {e}\n{frappe.get_traceback()}",
            )
            frappe.logger().error(
                f"Auto-label failed for Shipment {shipment_doc.name}: {e}"
            )
            result["label_created"] = False
            result["label_error"] = str(e)

    return result


def _resolve_delivery_note_from_barcode(barcode: str) -> str | None:
    # Barcode scanners frequently append/prepend whitespace (or a newline);
    # normalise first so a clean DN id like "MAT-DN-2026-00129" always resolves.
    code = (barcode or "").strip()
    if not code:
        return None

    # Match by document name and ALWAYS return the canonical name stored in the
    # DB (get_value resolves case-insensitively via MySQL collation), so a
    # lower/upper-cased scan doesn't yield a non-canonical id that later breaks
    # frappe.get_doc(). Try the value as-scanned, then upper-cased.
    for candidate in (code, code.upper()):
        canonical = frappe.db.get_value("Delivery Note", candidate, "name")
        if canonical:
            return canonical

    # Fallback: a dedicated barcode field on the Delivery Note, if configured.
    if frappe.db.has_column("Delivery Note", "barcode_number"):
        for candidate in (code, code.upper()):
            match = frappe.db.get_value("Delivery Note", {"barcode_number": candidate}, "name")
            if match:
                return match

    return None


def _existing_shipments_for_delivery(delivery_note: str) -> list[dict[str, Any]]:
    return frappe.db.sql(
        """
        select s.name, s.docstatus, s.awb_number as tracking
        from `tabShipment` s
        inner join `tabShipment Delivery Note` sdn on sdn.parent = s.name
        where sdn.delivery_note = %s and s.docstatus < 2
        order by s.creation desc
        """,
        delivery_note,
        as_dict=True,
    )


def _prepare_selected_items(
    delivery_note_doc,
    raw_items: list[dict[str, Any]],
) -> Tuple[list[dict[str, Any]], float, float]:
    from ..bundles import component_qty_for_bundles, components_by_line

    dn_rows = delivery_note_doc.get("items", [])
    rows_by_name = {row.name: row for row in dn_rows}
    shipped_map = _shipped_qty_by_item(delivery_note_doc.name)
    bundle_components = components_by_line(delivery_note_doc.name, dn_rows)
    components_by_name = {
        comp["name"]: comp for comps in bundle_components.values() for comp in comps
    }

    prepared: list[dict[str, Any]] = []
    total_amount = 0.0
    total_weight = 0.0

    def _take_component(comp: dict[str, Any], qty: float) -> None:
        nonlocal total_amount, total_weight
        key = comp["name"]
        available = flt(comp["qty"]) - flt(shipped_map.get(key, 0))
        if qty - available > 1e-6:
            frappe.throw(
                f"Selected quantity for item '{comp['item_name']}' (from bundle "
                f"'{comp['bundle_item_name']}') exceeds remaining quantity ({available})."
            )
        shipped_map[key] = shipped_map.get(key, 0) + qty

        weight_per_unit = flt(comp.get("weight_per_unit"))
        entry = {
            "delivery_note_item": comp["delivery_note_item"],
            "packed_item": key,
            "item_code": comp["item_code"],
            "item_name": comp["item_name"],
            "description": comp.get("description") or "",
            "qty": qty,
            "uom": comp.get("uom"),
            "rate": flt(comp.get("rate")),
            "amount": flt(comp.get("rate")) * qty,
            "weight_per_unit": weight_per_unit,
            "total_weight": weight_per_unit * qty if weight_per_unit else 0,
        }
        prepared.append(entry)
        total_amount += entry["amount"]
        total_weight += entry["total_weight"]

    for entry in raw_items:
        if isinstance(entry, str):
            continue
        name = (entry.get("delivery_note_item") or entry.get("name") or "").strip()
        packed_item = (entry.get("packed_item") or "").strip()
        qty = flt(entry.get("qty") or entry.get("quantity"))
        if not (name or packed_item) or qty <= 0:
            continue

        if packed_item:
            # A bundle component picked from the packing list.
            comp = components_by_name.get(packed_item)
            if not comp or (name and comp["delivery_note_item"] != name):
                frappe.throw(
                    f"Packed Item '{packed_item}' is not linked to Delivery Note {delivery_note_doc.name}."
                )
            _take_component(comp, qty)
            continue

        dn_row = rows_by_name.get(name)
        if not dn_row:
            frappe.throw(
                f"Delivery Note Item '{name}' is not linked to Delivery Note {delivery_note_doc.name}."
            )

        comps = bundle_components.get(name)
        if comps:
            # The bundle line itself was selected: that means "whole bundles",
            # so every component goes out in proportion.
            for comp in comps:
                _take_component(comp, component_qty_for_bundles(comp, dn_row.qty, qty))
            continue

        available = flt(dn_row.qty) - flt(shipped_map.get(name, 0))
        if qty - available > 1e-6:
            frappe.throw(
                f"Selected quantity for item '{dn_row.item_name or dn_row.item_code}' exceeds remaining quantity ({available})."
            )

        shipped_map[name] = shipped_map.get(name, 0) + qty

        base_amount = flt(getattr(dn_row, "base_amount", None)) or flt(getattr(dn_row, "amount", 0))
        divisor = flt(dn_row.qty) or 1
        per_qty_amount = base_amount / divisor if base_amount else 0
        amount = per_qty_amount * qty

        weight_per_unit = flt(getattr(dn_row, "weight_per_unit", 0))
        total_weight_item = weight_per_unit * qty if weight_per_unit else 0

        prepared.append(
            {
                "delivery_note_item": name,
                "item_code": dn_row.item_code,
                "item_name": dn_row.item_name or dn_row.item_code,
                "description": dn_row.description or "",
                "qty": qty,
                "uom": dn_row.uom,
                "rate": per_qty_amount,
                "amount": amount,
                "weight_per_unit": weight_per_unit,
                "total_weight": total_weight_item,
            }
        )

        total_amount += amount
        total_weight += total_weight_item

    return prepared, total_amount, total_weight


def _ensure_parcel_rows(
    shipment_doc,
    delivery_note_doc,
    selected_items: list[dict[str, Any]] | None = None,
    dimensions: dict | None = None,
) -> None:
    weight_sources: list[float] = []

    if selected_items:
        for item in selected_items:
            weight = flt(item.get("total_weight"))
            if not weight:
                per_unit = flt(item.get("weight_per_unit"))
                qty = flt(item.get("qty"))
                if per_unit and qty:
                    weight = per_unit * qty
            if weight:
                weight_sources.append(weight)
        if not weight_sources:
            fallback_weight = sum(flt(item.get("qty")) for item in selected_items)
        else:
            fallback_weight = sum(weight_sources)
    else:
        items = delivery_note_doc.get("items", [])
        total_net = flt(getattr(delivery_note_doc, "total_net_weight", 0))
        if total_net:
            weight_sources.append(total_net)

        for row in items:
            total_weight = flt(getattr(row, "total_weight", 0))
            if total_weight:
                weight_sources.append(total_weight)
            else:
                per_unit = flt(getattr(row, "weight_per_unit", 0))
                qty = flt(getattr(row, "qty", 0))
                if per_unit and qty:
                    weight_sources.append(per_unit * qty)

        fallback_weight = max(weight_sources) if weight_sources else 0
        if not fallback_weight:
            fallback_weight = float(delivery_note_doc.total_qty or 0)

    if not fallback_weight:
        fallback_weight = 1.0

    shipment_doc.total_weight = fallback_weight

    if shipment_doc.shipment_parcel:
        for parcel in shipment_doc.shipment_parcel:
            if not parcel.weight:
                parcel.weight = fallback_weight
    else:
        shipment_doc.append("shipment_parcel", {"weight": fallback_weight, "count": 1})

    # Stamp scanned dimensions (L/W/H) onto the parcel row(s) so the Shipment
    # carries the physical parcel size captured by the dimension barcode scan.
    if dimensions:
        for parcel in shipment_doc.shipment_parcel:
            if dimensions.get("length"):
                parcel.length = dimensions["length"]
            if dimensions.get("width"):
                parcel.width = dimensions["width"]
            if dimensions.get("height"):
                parcel.height = dimensions["height"]


def _ensure_descriptions(
    shipment_doc,
    delivery_note_doc,
    selected_items: list[dict[str, Any]] | None = None,
) -> None:
    if shipment_doc.description_of_content:
        return

    labels: list[str] = []
    source_rows = selected_items if selected_items else delivery_note_doc.get("items", [])

    # Without an item selection this falls back to the whole Delivery Note,
    # which also carries the freight fee and any pre-collected import duty.
    # This text becomes Shipment.description_of_content and travels on to the
    # carrier as the parcel's contents — a customs document listing "import tax"
    # as something inside the box is worse than a short description.
    if not selected_items:
        from ..bundles import components_by_line, expand_lines
        from ..shipment_contents import packable_item_codes

        # A Product Bundle line is a non-stock Item and would be filtered out
        # below; what is in the box are its packed components.
        try:
            source_rows = expand_lines(
                source_rows,
                components_by_line(getattr(delivery_note_doc, "name", None), source_rows),
            )
        except Exception:
            frappe.logger().warning(
                "[descriptions] bundle expansion failed; describing bundle lines as-is",
                exc_info=True,
            )

        try:
            packable = packable_item_codes(
                [row.get("item_code") if isinstance(row, dict) else getattr(row, "item_code", None)
                 for row in source_rows]
            )
            source_rows = [
                row for row in source_rows
                if (row.get("item_code") if isinstance(row, dict) else getattr(row, "item_code", None))
                in packable
            ]
        except Exception:
            frappe.logger().warning(
                "[descriptions] packable-item filter failed; describing every line",
                exc_info=True,
            )

        # A repair line describes the shoe, not the service (hook).
        try:
            from ..shipment_contents import apply_line_overrides, line_overrides

            overrides = line_overrides(getattr(delivery_note_doc, "name", None), source_rows)
            if overrides:
                source_rows = [
                    dict(row) if isinstance(row, dict) else {
                        "item_code": getattr(row, "item_code", None),
                        "item_name": getattr(row, "item_name", None),
                        "qty": getattr(row, "qty", 0),
                        "delivery_note_item": getattr(row, "name", None),
                    }
                    for row in source_rows
                ]
                for row in source_rows:
                    row.setdefault("delivery_note_item", row.get("name"))
                apply_line_overrides(source_rows, overrides)
        except Exception:
            frappe.logger().warning(
                "[descriptions] line overrides failed; describing lines as they are",
                exc_info=True,
            )

    for row in source_rows:
        if isinstance(row, dict):
            label = (row.get("item_name") or row.get("item_code") or "").strip()
            qty = flt(row.get("qty"))
        else:
            label = (getattr(row, "item_name", None) or getattr(row, "item_code", None) or "").strip()
            qty = flt(getattr(row, "qty", 0))
        if not label:
            continue
        if qty:
            if abs(qty - round(qty)) < 1e-6:
                qty_text = str(int(round(qty)))
            else:
                qty_text = ("{0:.3f}".format(qty)).rstrip("0").rstrip(".")
            label = f"{label} x{qty_text}"
        if label not in labels:
            labels.append(label)
    if labels:
        shipment_doc.description_of_content = ", ".join(labels)[:140]


def _ensure_pickup_details(shipment_doc, delivery_note_doc) -> None:
    """Ensure pickup details are set on the shipment, including company and address."""
    if not shipment_doc.pickup_date:
        shipment_doc.pickup_date = delivery_note_doc.posting_date or nowdate()
    if not shipment_doc.pickup_from:
        shipment_doc.pickup_from = "09:00:00"
    if not shipment_doc.pickup_to:
        shipment_doc.pickup_to = "17:00:00"
    
    # Ensure pickup company is set
    if not getattr(shipment_doc, "pickup_company", None):
        # Try to get company from delivery note
        company = getattr(delivery_note_doc, "company", None)
        if company:
            shipment_doc.pickup_company = company
            shipment_doc.pickup_from_type = "Company"
        else:
            # Fallback to default company
            default_company = frappe.db.get_single_value("Global Defaults", "default_company")
            if default_company:
                shipment_doc.pickup_company = default_company
                shipment_doc.pickup_from_type = "Company"
    
    # Ensure company address is set for GLS API
    if not getattr(shipment_doc, "pickup_address_name", None) and getattr(shipment_doc, "pickup_company", None):
        # Get the company's default address
        from frappe.contacts.doctype.address.address import get_company_address
        company_address = get_company_address(shipment_doc.pickup_company)
        if company_address:
            shipment_doc.pickup_address_name = company_address.get("company_address")
            # Also set the company_address field for GLS API compatibility
            if hasattr(shipment_doc, "company_address"):
                shipment_doc.company_address = company_address.get("company_address")
    
    # Set the company field as well (used by GLS API)
    if not getattr(shipment_doc, "company", None) and getattr(shipment_doc, "pickup_company", None):
        shipment_doc.company = shipment_doc.pickup_company
