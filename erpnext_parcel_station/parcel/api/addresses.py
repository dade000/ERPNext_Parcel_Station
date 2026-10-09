from __future__ import annotations
from typing import Any
import frappe
from frappe.utils import strip_html_tags


def _country_code(country_name: str | None) -> str:
    if not country_name:
        return "AT"
    code = frappe.db.get_value("Country", country_name, "code")
    return (code or "AT").upper()


def _get_address_fields(address_name: str | None) -> dict:
    if not address_name or not frappe.db.exists("Address", address_name):
        return {
            "address_line1": "",
            "city": "",
            "pincode": "",
            "country_code": "AT",
            "email": "",
            "phone": "",
        }
    ad = frappe.get_doc("Address", address_name)
    return {
        "address_line1": ad.address_line1 or "",
        "city": ad.city or "",
        "pincode": ad.pincode or "",
        "country_code": _country_code(ad.country),
        # Customs requires a sender phone OR email on international shipments —
        # Austrian Post rejects them with SN#10074 ("Telefonnummer oder
        # Emailadresse des Absenders für Zoll erforderlich") when both are absent.
        "email": ad.email_id or "",
        "phone": ad.phone or "",
    }


def _clean_address_display(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = value.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines)


def _normalize_display_from_html(value: str | None) -> str:
    if not value:
        return ""
    html = value.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    return _clean_address_display(strip_html_tags(html))
