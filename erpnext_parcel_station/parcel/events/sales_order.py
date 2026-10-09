"""Sales Order doc events.

One responsibility after the Shipping Rule hard-removal in the v3 cleanup:
mirror ``delivery_type`` from the shipping Address's ``is_parcel_shop`` flag
so admins don't have to set it by hand on the SO, and make sure the selected
Carrier Service's shipping Item is present as a line item.

The ``custom_products_subtotal`` / ``custom_shipping_cost`` mirror fields that
used to be maintained here were removed: no print format or downstream
integration read them (the GLS payload carries no amount at all), so the
shipping line item is the single representation of shipping cost. Anything
that needs the figure derives it from that line — see
``events.delivery_note._shipping_charge``.
"""

import frappe

from ..delivery_dates import committed_delivery_date


def _apply_carrier_delivery_date(doc) -> None:
    """Set delivery_date from the selected Carrier Service (authoritative).

    Carrier Service is the single source of truth: delivery_date =
    transaction_date + max_delivery_days. We set BOTH the header
    ``delivery_date`` AND every ``Sales Order Item.delivery_date`` row,
    because ERPNext's selling controller reconciles the header from the
    item rows during validate — setting only the header (as the webshop
    route did) gets silently overwritten. Runs in before_validate so the
    value is in place before that reconciliation.

    No-op when no carrier is selected or the carrier has no days
    configured (no hardcoded fallback — the existing value is left as-is).
    """
    carrier = doc.get("custom_carrier_service")
    if not carrier:
        return
    base = doc.get("transaction_date") or frappe.utils.nowdate()
    delivery_date = committed_delivery_date(carrier, base)
    if not delivery_date:
        return
    doc.delivery_date = delivery_date
    for row in doc.get("items") or []:
        row.delivery_date = delivery_date


def _derive_taxes_template(doc) -> str | None:
    """Pick the best Sales Taxes and Charges Template for ``doc``.

    Resolution order:

    1. ``doc.tax_category`` (or the customer's ``tax_category``) → look up
       a Sales Taxes and Charges Template tagged with that Tax Category for
       this company.
    2. Fall back to the company's ``is_default=1`` Sales Taxes and Charges
       Template.

    Returns ``None`` if nothing matches; caller treats that as "leave as-is".
    """
    if not doc.get("company"):
        return None

    tax_category = doc.get("tax_category")
    if not tax_category and doc.get("customer"):
        tax_category = frappe.db.get_value(
            "Customer", doc.customer, "tax_category"
        )

    if tax_category:
        template = frappe.db.get_value(
            "Sales Taxes and Charges Template",
            {
                "tax_category": tax_category,
                "company": doc.company,
                "disabled": 0,
            },
            "name",
        )
        if template:
            return template

    return frappe.db.get_value(
        "Sales Taxes and Charges Template",
        {"is_default": 1, "disabled": 0, "company": doc.company},
        "name",
    )


def _ensure_default_taxes_template(doc) -> None:
    """Make sure the SO has a ``taxes_and_charges`` template before ERPNext's
    own validate runs.

    Item-level ``item_tax_template`` overrides only apply on top of an
    existing template's tax rows — without a template, ERPNext leaves the
    Sales Taxes and Charges grid empty even when individual items (like a
    Carrier Service's ``shipping_item``) have their own tax mapping set.
    This shim populates a sensible default so those per-item overrides
    actually materialise as tax rows.
    """
    if doc.get("taxes_and_charges"):
        return
    template = _derive_taxes_template(doc)
    if not template:
        return
    doc.taxes_and_charges = template


def _force_party_account_currency(doc) -> None:
    """Force ``party_account_currency`` to the company's default currency.

    ERPNext normally derives ``party_account_currency`` from the customer's
    Party Account configuration (or, as a fallback, the customer's
    ``default_currency``). When a customer record was set up with a
    different currency than the operating company — common in test/staging
    sites where customers were imported from a non-EUR source — that
    foreign currency bleeds into customer-facing fields like
    ``Advance Paid`` and ``Outstanding Amount`` on the SO/DN, even
    though every other amount on the doc is in EUR.

    Forcing this field to match ``Company.default_currency`` keeps every
    customer-side display aligned with the company's reporting currency.
    Skip silently when company isn't set or the field doesn't exist
    (older ERPNext versions).
    """
    company = getattr(doc, "company", None)
    if not company:
        return
    company_currency = frappe.db.get_value("Company", company, "default_currency")
    if not company_currency:
        return
    if doc.get("party_account_currency") != company_currency:
        doc.party_account_currency = company_currency


def _sync_delivery_type_from_address(doc) -> None:
    """Set ``delivery_type`` from the shipping Address's parcel-shop flag.

    The Select field has exactly two valid options: ``Home Delivery`` and
    ``Parcelshop Delivery`` (lowercase ``s``, matching the downstream GLS
    routing checks). Driven entirely by whether the linked Address has
    ``is_parcel_shop=1`` — never set from the UI. Skip silently when no
    shipping address is set.
    """
    address = getattr(doc, "shipping_address_name", None)
    if not address:
        return
    is_parcel_shop = frappe.db.get_value("Address", address, "is_parcel_shop")
    desired = "Parcelshop Delivery" if is_parcel_shop else "Home Delivery"
    if doc.get("delivery_type") != desired:
        doc.delivery_type = desired


def _ensure_parcelshop_carrier_service(doc) -> None:
    """When shipping to a parcel-shop address, lock ``custom_carrier_service``
    to a Carrier Service that is marked ``is_parcelshop_pickup=1`` and whose
    ``allowed_countries`` includes the shipping country.

    Cases:
      - Address is not a parcel-shop: no-op (home-delivery flow handles its
        own carrier-service selection unchanged).
      - Already valid: keep what the user / webshop chose.
      - Set but not a parcel-shop service (or country mismatch): override
        with the first matching parcel-shop service for the country.
      - Unset: same — pick the first match.
      - No match exists: leave the field as-is and let downstream code /
        the operator surface the configuration gap; do NOT pick a non-
        parcel-shop service as a fallback (that would silently corrupt the
        parcel-shop flow).
    """
    address = getattr(doc, "shipping_address_name", None)
    if not address:
        return
    addr = frappe.db.get_value(
        "Address", address, ["is_parcel_shop", "country"], as_dict=True
    )
    if not addr or not addr.is_parcel_shop or not addr.country:
        return

    current = doc.get("custom_carrier_service") or None
    if current and _carrier_service_matches_parcelshop(current, addr.country):
        return

    match = _find_parcelshop_carrier_service(addr.country)
    if match:
        doc.custom_carrier_service = match


def _carrier_service_matches_parcelshop(name: str, country: str) -> bool:
    """True iff Carrier Service ``name`` has is_parcelshop_pickup=1 AND
    its allowed_countries includes ``country``.
    """
    if not name or not country:
        return False
    row = frappe.db.get_value(
        "Carrier Service", name, ["is_parcelshop_pickup"], as_dict=True
    )
    if not row or not row.is_parcelshop_pickup:
        return False
    return bool(
        frappe.db.exists(
            "Carrier Service Country",
            {
                "parent": name,
                "parenttype": "Carrier Service",
                "country": country,
            },
        )
    )


def _find_parcelshop_carrier_service(country: str) -> str | None:
    """First Carrier Service with is_parcelshop_pickup=1 whose
    allowed_countries includes ``country``. Returns the name or None.
    Ordered by ``modified DESC`` so the most recently configured service
    is preferred — gives ops a sane way to swap default carriers without
    a code change.
    """
    if not country:
        return None
    rows = frappe.db.sql(
        """
        SELECT cs.name
        FROM `tabCarrier Service` cs
        JOIN `tabCarrier Service Country` csc
          ON csc.parent = cs.name AND csc.parenttype = 'Carrier Service'
        WHERE cs.is_parcelshop_pickup = 1
          AND csc.country = %s
        ORDER BY cs.modified DESC
        LIMIT 1
        """,
        (country,),
    )
    return rows[0][0] if rows else None


def _ensure_shipping_line_item(doc) -> None:
    """When ``custom_carrier_service`` is set, ensure the Carrier Service's
    ``shipping_item`` is present as a line in ``doc.items``.

    Idempotent: if a row with that ``item_code`` is already on the SO (e.g.
    the webshop submitted it explicitly), we leave it alone. Otherwise we
    append a qty-1 line and let ERPNext's selling_controller.set_missing_values
    populate ``rate`` from Item Price on the SO's selling_price_list (with
    ``Item.standard_rate`` as the fallback). That matches the precedence the
    webshop's ``get_carrier_services_for_country`` uses, so the customer's
    pre-checkout display matches the resulting SO total.

    Fail-soft: if the Carrier Service has no ``shipping_item`` configured we
    record an Error Log entry (so ops see the gap in the desk) and let the
    SO proceed without a shipping line. Trading a missing shipping charge
    (correctable post-hoc) against blocking checkout is required behaviour:
    a half-configured carrier must never strand a customer mid-checkout.
    """
    carrier = doc.get("custom_carrier_service")
    if not carrier:
        return

    shipping_item = frappe.db.get_value(
        "Carrier Service", carrier, "shipping_item"
    )
    if not shipping_item:
        frappe.log_error(
            title="Sales Order shipping_item missing",
            message=(
                f"Carrier Service {carrier!r} has no shipping_item "
                f"configured. Sales Order "
                f"{doc.get('name') or '<new>'} is proceeding without a "
                f"shipping line — set shipping_item on the Carrier Service "
                f"to restore correct order totals."
            ),
        )
        return

    for row in doc.get("items") or []:
        if row.get("item_code") == shipping_item:
            return

    doc.append("items", {"item_code": shipping_item, "qty": 1})


def before_validate(doc, method=None):
    # Runs before ERPNext's selling_controller.validate so the delivery_type
    # we set here flows through downstream tax/template selection, the
    # taxes_and_charges template is in place before
    # calculate_taxes_and_totals runs, and rounding is suppressed before
    # the grand_total is finalised.
    _sync_delivery_type_from_address(doc)
    _ensure_parcelshop_carrier_service(doc)
    _ensure_shipping_line_item(doc)
    # After the carrier service is finalised and all item rows (incl. the
    # shipping line) exist, derive delivery_date from the carrier config.
    _apply_carrier_delivery_date(doc)
    _ensure_default_taxes_template(doc)
    _force_party_account_currency(doc)
    if hasattr(doc, "disable_rounded_total"):
        doc.disable_rounded_total = 1
