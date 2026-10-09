"""Single source of truth for "which carrier does this Shipment route to?".

Why this module exists
----------------------
Carrier routing used to be a *boolean* — ``is_gls`` — recomputed independently
at four call sites, each phrased as "GLS, or else Austrian Post":

  * ``api/shipments_ui.request_label``      (parcel-station label button)
  * ``infra/gls_integration.create_carrier_label_on_submit``  (Shipment.on_submit)
  * ``api/carrier.cancel_austrian_post_on_cancel``            (Shipment.on_cancel)
  * ``api/core.create_shipment_from_barcode``  (pre-flight service-ID check)

With exactly two carriers that happened to be correct. With a third it is
actively wrong, because "not GLS" no longer means "Austrian Post": a FedEx
shipment would be cancelled at Austrian Post, and its creation would be
blocked by a pre-flight check demanding an Austrian Post service ID.

So routing returns a carrier *identity* now, and every call site compares
against the carrier it actually handles instead of negating another one.

Resolution order (unchanged from the previous ``is_gls`` logic, so existing
Shipments route exactly as before):

  1. ``delivery_type == "Parcelshop Delivery"`` -> GLS. Parcel-shop delivery is
     a GLS-only product; the Address carries the shop, not the Carrier Service.
  2. ``Shipment.carrier`` — the carrier name copied from the Carrier Service
     master by ``copy_shipping_details_to_shipment``.
  3. The linked Carrier Service (``carrier_service``, or the legacy
     ``custom_carrier_service`` for pre-rework docs) -> its ``carrier`` field.
  4. ``DEFAULT_CARRIER`` — Austrian Post, matching the previous "everything
     else" branch.

Steps 2 and 3 tolerate an unrecognised carrier name (say a "DPD" master that
has no integration yet) by falling through, exactly as the old ``elif`` chain
did. Unknown names therefore land on ``DEFAULT_CARRIER`` and are logged.

Accepts either a Frappe Document or the plain ``as_dict()`` mapping, because
the Austrian Post payload path passes dicts around.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import frappe

#: Carrier identities. Values are internal keys, NOT the ``Carrier`` master's
#: display names — those are free text and get normalised via _CARRIER_ALIASES.
GLS = "GLS"
AUSTRIAN_POST = "AUSTRIAN_POST"
FEDEX = "FEDEX"

#: Where a Shipment lands when nothing identifies its carrier. Preserves the
#: previous "not GLS -> Austrian Post" fallback so existing docs are unaffected.
DEFAULT_CARRIER = AUSTRIAN_POST

#: delivery_type value that pins a Shipment to GLS regardless of Carrier Service.
PARCELSHOP_DELIVERY_TYPE = "Parcelshop Delivery"

#: Normalised ``Carrier`` master names -> carrier identity. Keys are the output
#: of _normalize, so "Austrian Post", "austrian_post" and "AUSTRIAN-POST" all
#: collapse to one entry.
#:
#: The live master is named "Österreichische Post". Accent folding turns that
#: into OSTERREICHISCHEPOST, but people also write it "Oesterreichische Post",
#: which folds to OESTERREICHISCHEPOST — both spellings are listed because a
#: carrier name that fails to match here silently falls back to DEFAULT_CARRIER.
_CARRIER_ALIASES = {
    "GLS": GLS,
    "AUSTRIANPOST": AUSTRIAN_POST,
    "OSTERREICHISCHEPOST": AUSTRIAN_POST,
    "OESTERREICHISCHEPOST": AUSTRIAN_POST,
    "POSTAT": AUSTRIAN_POST,
    "FEDEX": FEDEX,
    "FEDERALEXPRESS": FEDEX,
}


def _normalize(name: Any) -> str:
    """Collapse a carrier name to comparable form: accents folded, uppercased,
    alphanumerics only.

    Accent folding matters: the production Carrier master is "Österreichische
    Post", and simply stripping non-ASCII would eat the leading "Ö" and yield
    STERREICHISCHEPOST — a name that matches no alias and would quietly fall
    through to the default carrier.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _get(doc: Any, fieldname: str) -> Any:
    """Read a field from either a Document or a dict."""
    if isinstance(doc, dict):
        return doc.get(fieldname)
    return getattr(doc, fieldname, None)


def carrier_from_name(name: Any) -> str | None:
    """Map a ``Carrier`` master name to a carrier identity, or None if unknown."""
    return _CARRIER_ALIASES.get(_normalize(name))


def resolve_carrier(doc: Any) -> str:
    """Return the carrier identity (GLS / AUSTRIAN_POST / FEDEX) for a Shipment.

    Always returns one of the identity constants — never None — so call sites
    can compare directly without a null check.
    """
    if _get(doc, "delivery_type") == PARCELSHOP_DELIVERY_TYPE:
        return GLS

    carrier = carrier_from_name(_get(doc, "carrier"))
    if carrier:
        return carrier

    service_link = _get(doc, "carrier_service") or _get(doc, "custom_carrier_service")
    if service_link:
        try:
            service_carrier = frappe.db.get_value("Carrier Service", service_link, "carrier")
        except Exception:
            # A missing/renamed Carrier Service must not break label dispatch —
            # fall through to the default, same as the old try/except did.
            service_carrier = None
        carrier = carrier_from_name(service_carrier)
        if carrier:
            return carrier

    frappe.logger().info(
        f"[carrier-routing] Shipment={_get(doc, 'name')}: no known carrier "
        f"(carrier={_get(doc, 'carrier')!r}, carrier_service={service_link!r}, "
        f"delivery_type={_get(doc, 'delivery_type')!r}); "
        f"defaulting to {DEFAULT_CARRIER}."
    )
    return DEFAULT_CARRIER
