"""The two shapes a carrier label can be requested in.

``zpl`` is the carrier's native printer language and goes to a label printer
unchanged (hardware bridge or CUPS raw queue). ``pdf`` is the fallback for a
station without a label printer: the carrier renders the label itself and the
browser prints it through its dialog.

The format is chosen when the label is REQUESTED — a carrier issues a label
once per parcel — so the station sends the format that fits the printer
selected there. Labels created without a station (Shipment submit in the
Desk, automation) are ZPL. We never convert one format into the other and
never rewrite a label: it is printed exactly as the carrier returned it.
"""

from __future__ import annotations

import frappe
from frappe import _

ZPL = "zpl"
PDF = "pdf"
FORMATS = (ZPL, PDF)

#: Attachment names of every carrier label contain this (the customs papers and
#: the FedEx commercial invoice do not), whatever the extension.
LABEL_FILE_LIKE = "%-Label%"


def normalize(value: str | None, default: str = ZPL) -> str:
    """``"PDF"`` / ``" pdf "`` -> ``"pdf"``; empty -> ``default``. An unknown
    format is rejected rather than silently printed as something else."""
    text = (value or "").strip().lower()
    if not text:
        return default
    if text not in FORMATS:
        frappe.throw(_("Unknown label format {0}. Use zpl or pdf.").format(value))
    return text


def from_filename(filename: str | None) -> str | None:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return PDF
    if name.endswith(".zpl"):
        return ZPL
    return None


def stored_label(shipment_name: str) -> frappe._dict | None:
    """The newest carrier label attached to a Shipment:
    ``{file_url, file_name, label_format}``, or None when there is none."""
    rows = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Shipment",
            "attached_to_name": shipment_name,
            "file_name": ("like", LABEL_FILE_LIKE),
        },
        fields=["file_url", "file_name"],
        order_by="creation desc",
    )
    for row in rows:
        label_format = from_filename(row.file_name)
        if label_format:
            return frappe._dict(file_url=row.file_url, file_name=row.file_name, label_format=label_format)
    return None
