"""Support for FedEx's label approval process.

Before an account may print production labels, FedEx's Bar Code Analysis group
evaluates physical sample labels (three business days turnaround, submitted to
label@fedex.com or their Collierville address). Their acceptance rules that
software can actually check are:

  * labels must come from the test environment and from the real integration,
    using genuine shipper/recipient addresses;
  * one sample per service being applied for;
  * FedEx International Express samples must include the auxiliary/secondary
    Air Waybill label;
  * multi-piece shipments need one label per package;
  * for thermal printers the image type must match the printer — ZPLII for
    Zebra. PNG/PDF are laser-only and will be rejected.

This module checks those properties on labels we have already produced, and
prints the set. What it deliberately does NOT do is send anything to FedEx: the
submission is a physical/mail step.

The remaining rule — "printed and scanned at a minimum of 600 DPI, do not send
images returned directly from the API" — is about the SCAN of the printed
label. Print at the printer's native resolution (see
``FedEx Settings -> Label Resolution``), then scan the physical label at 600
dpi or better.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.utils.file_manager import get_file

from ..carrier_routing import FEDEX, resolve_carrier
from .fedex_api import _get_credentials

#: Media the label stock actually is, in millimetres (width x height). The
#: Versandraum stock is A6 (105 x 148 mm); FedEx's own STOCK_4X6 artwork is
#: 4 x 6 inch (101.6 x 152.4 mm), so the printable area is bounded by the
#: NARROWER of the two in each axis.
DEFAULT_MEDIA_MM = (105.0, 148.0)

#: Each ZPL label starts with ^XA. An international FedEx Express label stream
#: carries two: the shipping label and the auxiliary Air Waybill label.
_ZPL_LABEL_START = "^XA"


def _zpl_geometry(zpl: str, dpi: int) -> List[Dict[str, Any]]:
    """Measure each ^XA block in a ZPL stream, in millimetres.

    ZPL coordinates are in printer dots, so the physical size depends entirely
    on the dpi the label was generated for. Measuring here is what catches a
    resolution mismatch before it reaches a printer: a 300 dpi label rendered at
    203 dpi silently comes out ~1.5x too large and clipped.
    """
    blocks = [b for b in zpl.split(_ZPL_LABEL_START) if b.strip()]
    measured = []
    for index, block in enumerate(blocks, start=1):
        xs = [int(m.group(1)) for m in re.finditer(r"\^FO(\d+),(\d+)", block)]
        ys = [int(m.group(2)) for m in re.finditer(r"\^FO(\d+),(\d+)", block)]
        widths = [int(w) for w in re.findall(r"\^PW(\d+)", block)]
        measured.append(
            {
                "block": index,
                "print_width_dots": widths[0] if widths else None,
                "print_width_mm": round(widths[0] / dpi * 25.4, 1) if widths else None,
                "content_width_mm": round(max(xs) / dpi * 25.4, 1) if xs else None,
                "content_height_mm": round(max(ys) / dpi * 25.4, 1) if ys else None,
            }
        )
    return measured


def _label_file(shipment_name: str) -> tuple[str, str] | None:
    file_url = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "Shipment",
            "attached_to_name": shipment_name,
            "file_name": ("like", "%-FedEx-Label.%"),
        },
        "file_url",
        order_by="creation desc",
    )
    if not file_url:
        return None
    _name, content = get_file(file_url)
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return file_url, content


@frappe.whitelist()
def check_validation_labels(
    shipments: str | list[str] | None = None, media_mm: tuple[float, float] | None = None
) -> Dict[str, Any]:
    """Inspect FedEx labels for everything the approval submission requires.

    Pass the Shipments that make up the sample set — one per service being
    applied for. Returns a per-shipment report plus a list of blocking problems;
    an empty ``problems`` list means the set is ready to print, scan and send.
    """
    if isinstance(shipments, str):
        shipments = frappe.parse_json(shipments) if shipments.startswith("[") else [shipments]
    if not shipments:
        frappe.throw(_("Pass the Shipment names that make up the sample set."))

    creds = _get_credentials()
    width_limit, height_limit = media_mm or DEFAULT_MEDIA_MM

    report: List[Dict[str, Any]] = []
    problems: List[str] = []
    services_seen: set[str] = set()

    if creds.mode != "Sandbox":
        problems.append(
            "FedEx Settings is in Production mode — evaluation labels must come "
            "from the test environment."
        )
    if creds.label_image_type.upper() != "ZPLII":
        problems.append(
            f"Label image type is {creds.label_image_type}; FedEx rejects PNG/PDF "
            f"samples for thermal printers. Use ZPLII for the Zebra."
        )

    for name in shipments:
        entry: Dict[str, Any] = {"shipment": name}
        if not frappe.db.exists("Shipment", name):
            problems.append(f"{name}: Shipment not found.")
            report.append(entry)
            continue

        doc = frappe.get_doc("Shipment", name)
        carrier = resolve_carrier(doc)
        entry["carrier"] = carrier
        entry["tracking"] = cstr(doc.awb_number)
        entry["service"] = cstr(doc.carrier_service)
        service_code = frappe.db.get_value("Carrier Service", doc.carrier_service, "service_code")
        entry["service_code"] = service_code
        if service_code:
            services_seen.add(service_code)

        if carrier != FEDEX:
            problems.append(f"{name}: routes to {carrier}, not FedEx.")
        if not entry["tracking"]:
            problems.append(f"{name}: no tracking number — the label was never created.")

        origin = frappe.db.get_value("Address", doc.pickup_address_name, "country")
        destination = frappe.db.get_value("Address", doc.delivery_address_name, "country")
        entry["origin"] = origin
        entry["destination"] = destination
        international = bool(origin and destination and origin != destination)
        entry["international"] = international

        found = _label_file(name)
        if not found:
            problems.append(f"{name}: no FedEx label attached.")
            report.append(entry)
            continue

        file_url, zpl = found
        entry["label_file_url"] = file_url
        blocks = _zpl_geometry(zpl, creds.label_resolution)
        entry["label_blocks"] = blocks
        entry["auxiliary_label_present"] = len(blocks) > 1

        # FedEx International Express samples are rejected without the
        # auxiliary/secondary Air Waybill label.
        if international and len(blocks) < 2:
            problems.append(
                f"{name}: international shipment but only {len(blocks)} label block — "
                f"the auxiliary Air Waybill label is missing."
            )

        packages = len(doc.shipment_parcel or []) or 1
        entry["packages"] = packages
        expected_blocks = packages + (1 if international else 0)
        if packages > 1 and len(blocks) < expected_blocks:
            problems.append(
                f"{name}: {packages} packages but only {len(blocks)} label blocks — "
                f"multi-piece submissions need one label per package."
            )

        for block in blocks:
            if block["print_width_mm"] and block["print_width_mm"] > width_limit:
                problems.append(
                    f"{name} block {block['block']}: {block['print_width_mm']} mm wide "
                    f"exceeds the {width_limit} mm stock — check Label Resolution "
                    f"against the printer."
                )
            if block["content_height_mm"] and block["content_height_mm"] > height_limit:
                problems.append(
                    f"{name} block {block['block']}: content reaches "
                    f"{block['content_height_mm']} mm on {height_limit} mm stock."
                )

        report.append(entry)

    return {
        "mode": creds.mode,
        "label_image_type": creds.label_image_type,
        "label_resolution_dpi": creds.label_resolution,
        "label_stock_type": creds.label_stock_type,
        "media_mm": [width_limit, height_limit],
        "services_covered": sorted(services_seen),
        "shipments": report,
        "problems": problems,
        "ready": not problems,
    }


@frappe.whitelist()
def print_validation_labels(
    shipments: str | list[str] | None = None, printer: str | None = None
) -> Dict[str, Any]:
    """Check the sample set, then print every label in it.

    Refuses to print while ``check_validation_labels`` reports problems — a
    rejected submission costs another three-business-day evaluation cycle.
    """
    if isinstance(shipments, str):
        shipments = frappe.parse_json(shipments) if shipments.startswith("[") else [shipments]
    if not printer:
        frappe.throw(_("printer is required"))

    check = check_validation_labels(shipments)
    if not check["ready"]:
        frappe.throw(
            _("Sample set is not ready:\n{0}").format("\n".join(check["problems"])),
            title=_("FedEx label validation"),
        )

    from ..api.shipments_ui import print_shipment_label

    jobs = []
    for name in shipments:
        jobs.append(print_shipment_label(shipment_name=name, printer=printer, trigger="manual"))
    check["print_jobs"] = jobs
    return check
