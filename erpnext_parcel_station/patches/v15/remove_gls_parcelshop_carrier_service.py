"""Remove the seeded GLS Parcel-Shop Carrier Service.

An earlier version of ``seed_carrier_service_replacements`` created a
``GLS Parcel-Shop`` Carrier Service (linked to ``SHIP-GLS-PARCELSHOP``)
to back the legacy GLS parcel-shop checkout flow. Daniel decided that
flow is not used at holzschuhe.at; this patch deletes the Carrier
Service from any environment where the earlier seed ran.

The companion ``SHIP-GLS-PARCELSHOP`` Item is also removed when nothing
references it. If existing transactions (Sales Orders, Delivery Notes,
Sales Invoices) reference it, Frappe refuses the delete — we fall back
to setting ``disabled=1`` so the Item stops appearing in lookups while
the historical references stay intact.

Idempotent — no-op if both records are already gone.
"""
from __future__ import annotations

import frappe


_CARRIER_SERVICE_NAME = "GLS Parcel-Shop"
_ITEM_CODE = "SHIP-GLS-PARCELSHOP"


def execute() -> None:
    _try_delete_or_disable("Carrier Service", _CARRIER_SERVICE_NAME)
    _try_delete_or_disable("Item", _ITEM_CODE)


def _try_delete_or_disable(doctype: str, name: str) -> None:
    """Delete the record; if Frappe refuses due to link references, fall
    back to setting ``disabled=1`` (when the doctype exposes that field)
    or log a warning so ops can hand-cleanup."""
    if not frappe.db.exists(doctype, name):
        return

    try:
        frappe.delete_doc(doctype, name, ignore_permissions=True)
        return
    except frappe.LinkExistsError:
        pass
    except Exception as exc:
        frappe.logger().warning(
            f"[remove_gls_parcelshop_carrier_service] {doctype} {name!r} "
            f"delete raised {type(exc).__name__}: {exc} — trying disable."
        )

    meta = frappe.get_meta(doctype)
    if meta.has_field("disabled"):
        frappe.db.set_value(doctype, name, "disabled", 1)
        frappe.logger().info(
            f"[remove_gls_parcelshop_carrier_service] {doctype} {name!r} "
            f"is referenced by existing docs — disabled instead of deleted."
        )
    else:
        frappe.logger().warning(
            f"[remove_gls_parcelshop_carrier_service] {doctype} {name!r} "
            f"could not be deleted (referenced) and has no `disabled` "
            f"field — manual cleanup required."
        )
