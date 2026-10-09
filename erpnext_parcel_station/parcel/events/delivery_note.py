"""In-process Delivery Note → Shipment integration.

Registered via ``doc_events`` in ``hooks.py`` for ``Delivery Note.on_submit``.

Replaces the HTTP webhook bridge that existed during the abandoned
standalone-decoupling experiment (``parcel_sync/`` on devich, ``parcel/api/webhook.py``
on parcel-station). In the merged single-site setup both apps share one Frappe
DB, so the data the old bridge transported is already in-process; we only need
to fire the Shipment-creation step downstream of every submitted Delivery Note.

The created Shipment is left in ``docstatus=0`` (Draft). Label generation is
intentionally not triggered here — it runs from ``Shipment.on_submit`` via
``gls_integration.create_carrier_label_on_submit``.
"""
from __future__ import annotations

import frappe
from frappe.contacts.doctype.address.address import get_company_address
from frappe.model.document import Document

from ..delivery_dates import committed_delivery_date


def _linked_sales_order(doc: Document) -> str | None:
    """Return the Sales Order linked from the DN's first item row, if any."""
    for row in doc.get("items") or []:
        so = row.get("against_sales_order")
        if so:
            return so
    return None


def _apply_expected_delivery_date(doc: Document) -> None:
    """Populate Delivery Note.custom_expected_delivery_date.

    The Delivery Note has no native delivery-date field, so we carry the
    expected date on a custom field for consistency with the Sales Order.

    Resolution (Carrier Service is the single source of truth):
      1. Recalculate from the DN's own custom_carrier_service
         (posting_date + max_delivery_days).
      2. Otherwise inherit the linked Sales Order's delivery_date.
    No-op (and no hardcoded fallback) when neither is available.
    """
    if not frappe.get_meta("Delivery Note").has_field("custom_expected_delivery_date"):
        return
    carrier = doc.get("custom_carrier_service")
    base = doc.get("posting_date") or frappe.utils.nowdate()
    expected = committed_delivery_date(carrier, base) if carrier else None
    if not expected:
        so = _linked_sales_order(doc)
        if so:
            expected = frappe.db.get_value("Sales Order", so, "delivery_date")
    if expected:
        doc.custom_expected_delivery_date = expected


def _carrier_service_rate(carrier_service_name) -> float:
    """Look up shipping amount via Carrier Service → shipping_item → rate.

    Resolution order (matches webshop ``get_carrier_services_for_country``):
      1. Item Price on the default Selling Price List with ``selling=1``.
      2. ``Item.standard_rate`` (fallback when no Item Price row exists).
      3. 0 (when any link is missing).

    Using Item Price first keeps the Shipment fee aligned with what the
    storefront displayed at checkout — ops were seeing 0.00 on Shipments
    because the original implementation only read ``standard_rate`` and the
    real prices are configured via Item Price.
    """
    if not carrier_service_name:
        return 0.0
    shipping_item = frappe.db.get_value(
        "Carrier Service", carrier_service_name, "shipping_item"
    )
    if not shipping_item:
        return 0.0

    default_price_list = (
        frappe.db.get_single_value("Selling Settings", "selling_price_list")
        or "Standard Selling"
    )
    rate = frappe.db.get_value(
        "Item Price",
        {
            "item_code": shipping_item,
            "price_list": default_price_list,
            "selling": 1,
        },
        "price_list_rate",
    )
    if rate:
        return float(rate)

    standard_rate = float(
        frappe.db.get_value("Item", shipping_item, "standard_rate") or 0
    )
    if standard_rate:
        return standard_rate

    # Neither Item Price nor standard_rate produced a non-zero figure.
    # Record an Error Log entry so ops sees the config gap in the desk
    # — the Shipment will still be placed but with shipment_amount=0.
    frappe.log_error(
        title="Shipping item has no resolvable rate",
        message=(
            f"Carrier Service {carrier_service_name!r} → shipping_item "
            f"{shipping_item!r} resolved to rate 0. Set Item Price on "
            f"'{default_price_list}' (selling=1) or Item.standard_rate to "
            f"restore correct order/shipment totals."
        ),
    )
    return 0.0


def _delivery_type_for_address(address_name: str | None) -> str:
    """Derive Shipment.delivery_type from the shipping Address's
    ``is_parcel_shop`` flag. The Delivery Note has no ``delivery_type``
    field (it's only a custom field on Sales Order), so we can't copy
    it through the SO→DN→Shipment chain. The Address is the canonical
    parcel-shop marker, so we read it directly here.
    """
    if not address_name:
        return "Home Delivery"
    is_parcel_shop = frappe.db.get_value(
        "Address", address_name, "is_parcel_shop"
    )
    return "Parcelshop Delivery" if is_parcel_shop else "Home Delivery"


def before_validate(doc: Document, method: str | None = None) -> None:
    """Force Delivery Note currency to the Company's default currency.

    Runs before ERPNext's selling_controller computes totals, so amounts
    are interpreted in the forced currency from the start. The customer's
    party_account_currency never bleeds through into the shipping doc
    chain — Shipment is filed from the company's perspective, and the DN
    feeding it must match.

    No-op if the DN already matches its Company default currency. When we
    do override, ``conversion_rate`` is reset to 1.0 so totals don't get
    double-converted; assumes upstream rates are already in the company's
    currency (Sales Order built from EUR price list for an EUR company).
    """
    company = getattr(doc, "company", None)
    if not company:
        return
    company_currency = frappe.db.get_value("Company", company, "default_currency")
    if not company_currency:
        return
    if doc.get("currency") != company_currency:
        doc.currency = company_currency
        doc.conversion_rate = 1.0
    # Also force `party_account_currency` (the field that drives the
    # Advance Paid / Outstanding displays) so the DN never shows the
    # customer's legacy currency in those slots while everything else
    # is in the company's currency.
    if doc.get("party_account_currency") != company_currency:
        doc.party_account_currency = company_currency
    # Carry the carrier-driven expected delivery date onto the DN.
    _apply_expected_delivery_date(doc)
    # Suppress rounding so rounded_total stays out of the calculation
    # path entirely — grand_total is the final value displayed.
    if hasattr(doc, "disable_rounded_total"):
        doc.disable_rounded_total = 1


def _shipping_item_codes() -> set:
    """Return the set of Item codes configured on any Carrier Service as
    ``shipping_item`` **and** that are non-stock (service) Items.

    The non-stock guard is deliberate: a Carrier Service may have its
    ``shipping_item`` mis-configured to a real stock product (e.g. a
    ``GLS-EXPRESS`` service pointing at product ``00005-37``). Without this
    filter, any order containing that product would have the product's value
    mis-counted as shipping, dumping the whole order total into the
    Shipment's ``shipment_amount``. A genuine shipping line is always a
    service Item (``is_stock_item = 0``), so we only treat non-stock items as
    shipping. Mirrors the scan-filter in ``parcel/api/shipments_ui.py``.
    """
    rows = frappe.get_all("Carrier Service", pluck="shipping_item") or []
    codes = {code for code in rows if code}
    if not codes:
        return set()
    non_stock = (
        frappe.get_all(
            "Item",
            filters={"name": ["in", list(codes)], "is_stock_item": 0},
            pluck="name",
        )
        or []
    )
    return set(non_stock)


def _shipping_charge(doc: Document) -> float:
    """Return this document's GROSS shipping amount, derived from its items.

    The shipping cost has exactly one representation in the data: the line
    item carrying the selected Carrier Service's ``shipping_item`` (added on
    the Sales Order by ``sales_order._ensure_shipping_line_item`` and mapped
    through to the DN). We sum those lines rather than reading a mirrored
    custom field, so there is nothing to keep in sync and nothing to migrate.

    The figure is scaled to GROSS (VAT-inclusive) to match what the customer
    was quoted at webshop checkout — the storefront prices everything
    VAT-inclusive, so a €20-net carrier rate is €24 to the customer. A single
    VAT rate applies to the whole document, so scaling by the doc's own
    ``grand_total / net_total`` ratio is exact.

    Returns 0.0 when no shipping line is present, or when the Carrier Service
    lookup fails — a transient DB error must never abort DN submission, and a
    0 is visible to ops on the Shipment as "shipping cost not configured".
    """
    try:
        ship_codes = _shipping_item_codes()
    except Exception:
        frappe.logger().warning(
            "[parcel-station] could not resolve Carrier Service shipping items; "
            "falling back to shipping charge 0"
        )
        return 0.0

    shipping_net = sum(
        float(row.get("amount") or 0)
        for row in doc.get("items") or []
        if row.get("item_code") in ship_codes
    )
    if not shipping_net:
        return 0.0

    net_total = float(doc.get("net_total") or 0)
    grand_total = float(doc.get("grand_total") or 0)
    if net_total <= 0 or grand_total <= 0:
        return round(shipping_net, 2)
    return round(shipping_net * (grand_total / net_total), 2)


def on_submit(doc: Document, method: str | None = None) -> None:
    """Create a Draft Shipment for the just-submitted Delivery Note.

    Idempotent: if any non-cancelled Shipment already references this DN via
    its ``shipment_delivery_note`` child table, do nothing.

    Resilient: if either the Company-side pickup Address or the
    Customer-side shipping Address can't be resolved, we log a warning and
    SKIP Shipment creation rather than failing the DN submit. The operator
    can either fix the missing Address and re-run the integration manually,
    or create the Shipment by hand from the DN.
    """
    if doc.doctype != "Delivery Note":
        return

    if _existing_shipment_for(doc.name):
        return

    shipping_address = (
        doc.shipping_address_name
        or doc.customer_address
        or getattr(doc, "delivery_address_name", None)
    )
    pickup_address = _company_address(doc.company)

    if not pickup_address:
        frappe.logger().warning(
            f"[parcel-station] DN {doc.name}: no Company Address resolved for "
            f"'{doc.company}' (need an Address with is_your_company_address=1 "
            f"linked to the Company). Skipping auto-Shipment creation; create "
            f"manually if needed."
        )
        return

    if not shipping_address:
        frappe.logger().warning(
            f"[parcel-station] DN {doc.name}: no shipping Address on the DN "
            f"(shipping_address_name / customer_address / delivery_address_name "
            f"all empty). Skipping auto-Shipment creation."
        )
        return

    # Shipment amount is derived from this DN's shipping line item — the one
    # place the cost actually lives. Never derive it from
    # total_taxes_and_charges (that's a taxes sum, not shipping). A 0 means the
    # SO never resolved a carrier rate; ops sees that on the Shipment as
    # "shipping cost not configured" and can fix the upstream carrier setup.
    # Note this figure is informational only — the GLS label payload carries no
    # amount (see gls_api._build_payload).
    shipping_charge = _shipping_charge(doc)

    # Pickup-side contact: default to whoever submitted the DN so ops doesn't
    # have to hand-pick a User on every freshly-created Shipment. The
    # underlying field is `pickup_contact_person` (Link to User); setting it
    # is what lets ERPNext's fetch_from populate `pickup_contact_email` from
    # that user's email. We also set the display Small Text explicitly so
    # the form doesn't render an empty contact card before save.
    pickup_user, pickup_email, pickup_full_name = _pickup_contact_defaults()

    shipment = frappe.get_doc({
        "doctype": "Shipment",
        "pickup_from_type": "Company",
        "pickup_company": doc.company,
        "pickup_address_name": pickup_address,
        "pickup_contact_person": pickup_user,
        "pickup_contact_email": pickup_email,
        "pickup_contact": (
            f"{pickup_full_name}<br>{pickup_email}"
            if pickup_full_name and pickup_email
            else (pickup_email or "")
        ),
        "delivery_to_type": "Customer",
        "delivery_customer": doc.customer,
        "delivery_address_name": shipping_address,
        "delivery_contact_name": getattr(doc, "contact_person", None),
        "pickup_date": frappe.utils.today(),
        "description_of_content": _content_description(doc),
        "shipment_amount": shipping_charge,
        # delivery_type can't be inherited from the DN (no field there).
        # Derive it directly from the shipping Address — Address.is_parcel_shop
        # is the canonical source-of-truth that the SO's _sync_delivery_type_
        # from_address hook also reads.
        "delivery_type": _delivery_type_for_address(shipping_address),
        "custom_carrier_service": getattr(doc, "custom_carrier_service", None),
        "shipment_delivery_note": [{
            "delivery_note": doc.name,
            "grand_total": doc.grand_total or 0,
        }],
        "shipment_parcel": [_default_parcel_row(doc)],
    })

    # Force currency from the pickup Company's default currency — never the
    # DN's or customer's. Shipment is filed from the company's perspective,
    # so it's always quoted in the company's currency.
    pickup_currency = frappe.db.get_value(
        "Company", doc.company, "default_currency"
    )
    if pickup_currency and frappe.get_meta("Shipment").has_field("currency"):
        shipment.currency = pickup_currency
    # Carry the expected delivery date onto the Shipment so the full
    # lifecycle (SO → DN → Shipment) shows the same carrier-driven date.
    # Prefer the DN's stored value; recompute from the carrier otherwise.
    if frappe.get_meta("Shipment").has_field("custom_expected_delivery_date"):
        expected = getattr(doc, "custom_expected_delivery_date", None) or (
            committed_delivery_date(
                getattr(doc, "custom_carrier_service", None), frappe.utils.today()
            )
        )
        if expected:
            shipment.custom_expected_delivery_date = expected
    # Never let a Shipment-side failure (missing field, stale series counter,
    # validation hook on Shipment, etc.) roll back the DN submit. If the
    # insert fails, log it and continue — ops can create the Shipment by
    # hand from the DN, and re-running this on_submit (idempotent via
    # _existing_shipment_for) is safe.
    try:
        shipment.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(
            title=f"[parcel-station] auto-Shipment creation failed for DN {doc.name}",
            message=frappe.get_traceback(),
        )


def _existing_shipment_for(delivery_note_name: str) -> bool:
    rows = frappe.db.sql(
        """
        SELECT s.name
        FROM `tabShipment` s
        JOIN `tabShipment Delivery Note` sdn ON sdn.parent = s.name
        WHERE sdn.delivery_note = %s AND s.docstatus < 2
        LIMIT 1
        """,
        delivery_note_name,
    )
    return bool(rows)


def _pickup_contact_defaults() -> tuple[str | None, str | None, str | None]:
    """Pick a sensible default pickup-contact user for the new Shipment.

    Order of preference:
      1. The User whose email matches Parcel Station Settings' `sender_email`
         (the canonical company sender — survives session changes).
      2. The currently-logged-in user (whoever submitted the DN).

    Looks up the candidate against BOTH `User.name` and `User.email`, because
    `frappe.session.user` carries a User's `name` (e.g. "Administrator") which
    isn't always the same as the User's `email`. Returns
    ``(user_id, email, full_name)``; any of them may be None if no enabled
    User account matches.
    """

    def _lookup(value: str) -> tuple[str | None, str | None, str | None]:
        for field in ("name", "email"):
            row = frappe.db.get_value(
                "User",
                {field: value, "enabled": 1},
                ["name", "email", "full_name"],
                as_dict=True,
            )
            if row:
                return row.name, row.email or row.name, row.full_name
        return None, None, None

    sender_email = frappe.db.get_single_value(
        "Parcel Station Settings", "sender_email"
    )
    if sender_email:
        hit = _lookup(sender_email)
        if hit[0]:
            return hit

    if frappe.session and frappe.session.user and frappe.session.user != "Guest":
        hit = _lookup(frappe.session.user)
        if hit[0]:
            return hit

    return None, None, None


def _company_address(company: str | None) -> str | None:
    if not company:
        return None
    try:
        info = get_company_address(company)
    except Exception:
        return None
    return getattr(info, "company_address", None) if info else None


def _content_description(dn: Document) -> str:
    """Shipment.description_of_content is mandatory in stock ERPNext.

    Default to a short summary of the DN's line items; fall back to the DN
    name if items can't be summarised.
    """
    parts: list[str] = []
    for it in (dn.get("items") or [])[:3]:
        item_name = it.get("item_name") or it.get("item_code")
        if item_name:
            qty = it.get("qty") or 0
            parts.append(f"{item_name} x{qty}")
    if not parts:
        return f"Goods from {dn.name}"
    suffix = " (+more)" if len(dn.get("items") or []) > 3 else ""
    return ", ".join(parts) + suffix


def _default_parcel_row(dn: Document) -> dict:
    """Shipment.on_submit validation in core ERPNext requires at least one
    Shipment Parcel row with a non-zero count. We populate a single placeholder
    row so the Draft Shipment can be submitted by the operator without manual
    pre-editing. Weight defaults to the DN's net weight (or 1.0 kg if missing).
    The operator can edit / split rows before submit if needed.
    """
    weight = float(dn.get("total_net_weight") or 1.0)
    return {
        "count": 1,
        "weight": max(weight, 0.1),
        "length": 10,
        "width": 10,
        "height": 10,
    }
