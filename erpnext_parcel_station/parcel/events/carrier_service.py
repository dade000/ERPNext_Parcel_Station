"""Carrier Service validation hook.

Registered via ``doc_events`` in ``hooks.py`` for ``Carrier Service.validate``.

Carrier Service is now the single source of truth for shipping (Shipping
Rule was hard-removed in the v3 cleanup). The webshop checkout endpoint
``get_carrier_services_for_country`` skips Carrier Services that lack a
configured ``shipping_item`` because the SO needs that Item to build the
shipping line. Saving such a service would orphan it — config exists,
never selectable. This hook surfaces the misconfig at save time with a
clear error instead of letting it manifest as a silent dropout downstream.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint


def validate(doc: Document, method: str | None = None) -> None:
    """Require a unique Shipping Item, and enforce one Parcel Shop Pickup per
    (Carrier, Country)."""
    if doc.doctype != "Carrier Service":
        return

    if not getattr(doc, "shipping_item", None):
        frappe.throw(
            "Shipping Item is required on Carrier Service.\n\n"
            "The webshop drops Carrier Services without a Shipping Item from "
            "the checkout dropdown (it has no way to build the SO's shipping "
            "line), so saving without one would orphan this record. Set "
            "'Shipping Item' to a valid Item — for in-store-pickup style "
            "services use an Item with standard_rate=0."
        )

    _set_carrier_key(doc)
    _validate_unique_shipping_item(doc)
    _validate_single_pickup_per_carrier_country(doc)
    _validate_import_charges(doc)


#: Carriers whose payload actually carries a duties-payment instruction. Setting
#: one anywhere else collects money from the customer without telling the carrier
#: to bill us for it — see _validate_import_charges.
CARRIERS_SUPPORTING_DDP = {"FEDEX"}


def _set_carrier_key(doc: Document) -> None:
    """Store the resolved carrier identity on the record.

    The form uses it to show only the settings the chosen carrier reads. It is
    kept server-side rather than compared in the form's `depends_on`, because
    the Carrier master holds a free-text name — "Österreichische Post", umlaut
    and all — and a JS string comparison against that breaks on the first
    rename. `carrier_from_name` is the same resolver label dispatch uses.
    """
    from ..carrier_routing import carrier_from_name

    doc.carrier_key = carrier_from_name(getattr(doc, "carrier", None)) or ""


def _validate_unique_shipping_item(doc: Document) -> None:
    """Each Carrier Service must own its Shipping Item — it may not be shared
    with any other Carrier Service.

    The Shipping Item is the line ERPNext drops onto the Sales Order for this
    service's shipping fee. Sharing one Item across services makes the fee/tax
    config ambiguous and the per-service rate impossible to distinguish, so we
    require a 1:1 mapping between Carrier Service and Shipping Item.
    """
    shipping_item = getattr(doc, "shipping_item", None)
    if not shipping_item:
        return

    duplicate = frappe.db.exists(
        "Carrier Service",
        {"shipping_item": shipping_item, "name": ["!=", doc.name or ""]},
    )
    if duplicate:
        # Carrier Service names are random hashes, so report the human-readable
        # Service Name — the raw ID means nothing to whoever hits this message.
        label = frappe.db.get_value("Carrier Service", duplicate, "service_name") or duplicate
        frappe.throw(
            f"The Shipping Item '{shipping_item}' is already assigned to another "
            f"Carrier Service ({label}).<br><br>"
            "Each Carrier Service must have its own Shipping Item — it cannot be "
            "shared. To continue, select a different Shipping Item for this service.",
            title="Shipping Item Already In Use",
        )


def _validate_single_pickup_per_carrier_country(doc: Document) -> None:
    """For a given (Carrier, Country), only ONE Carrier Service may have
    Parcel Shop Pickup enabled (``is_parcelshop_pickup = 1``).

    Home-delivery services (Parcel Shop Pickup = No) are unlimited — a carrier
    can offer many of them in the same country. The Parcel Shop Pickup is the
    single country-specific pickup point, so a second one for the same
    Carrier + Country is rejected.

    Country scope comes from the service's ``allowed_countries`` child table.
    A pickup service with no countries listed has nothing to conflict on.
    """
    if not cint(doc.get("is_parcelshop_pickup")):
        # Home delivery — unrestricted, nothing to check.
        return

    carrier = getattr(doc, "carrier", None)
    if not carrier:
        # 'carrier' is required by the doctype; let that validation report it.
        return

    countries = [
        row.country for row in (doc.get("allowed_countries") or []) if getattr(row, "country", None)
    ]
    if not countries:
        return

    conflict = frappe.db.sql(
        """
        select coalesce(cs.service_name, cs.name) as service, csc.country as country
        from `tabCarrier Service` cs
        inner join `tabCarrier Service Country` csc on csc.parent = cs.name
        where cs.is_parcelshop_pickup = 1
          and cs.carrier = %(carrier)s
          and cs.name != %(self)s
          and csc.country in %(countries)s
        limit 1
        """,
        {
            "carrier": carrier,
            "self": doc.name or "",
            "countries": tuple(countries),
        },
        as_dict=True,
    )
    if conflict:
        c = conflict[0]
        frappe.throw(
            f"A Parcel Shop Pickup service already exists for carrier "
            f"'{carrier}' in {c.country} ({c.service}).<br><br>"
            "Only one Parcel Shop Pickup service is allowed per Carrier and "
            f"Country. To continue, either disable Parcel Shop Pickup for this "
            f"service or remove {c.country} from the Allowed Countries list.",
            title="Duplicate Parcel Shop Pickup",
        )


def _validate_import_charges(doc: Document) -> None:
    """Guard the two ways pre-collected import charges silently go wrong.

    1. Charging the customer at checkout while the carrier bills the recipient
       at the border makes them pay twice. The carrier is told what this field
       says, so it has to say "Sender (DDP)".
    2. Import duty and tax rates belong to ONE country. A Carrier Service in
       this system covers exactly one destination ("Paket Plus Schweiz"), which
       is what makes putting the rates here sound — a service listing several
       countries would apply one country's rates to all of them.

    Neither shows up until a parcel is already at a border, so both are refused
    at save time.
    """
    if not getattr(doc, "customs_item", None):
        _warn_about_uncollected_duties(doc)
        return

    if getattr(doc, "carrier_key", None) not in CARRIERS_SUPPORTING_DDP:
        frappe.throw(
            "Only {0} can be told who pays duty and tax, so pre-collected import "
            "charges cannot be configured on a {1} service.\n\n"
            "The charge would be taken from the customer at checkout while the "
            "carrier still billed the recipient at the border — the customer would "
            "pay twice and we would keep the difference.".format(
                ", ".join(sorted(CARRIERS_SUPPORTING_DDP)),
                getattr(doc, "carrier", None) or "this",
            )
        )

    if getattr(doc, "duties_payment", None) != "Sender (DDP)":
        frappe.throw(
            "This Carrier Service pre-collects import charges (Import Charge Item is set), "
            "so 'Duties & Taxes Paid By' must be 'Sender (DDP)'.\n\n"
            "Otherwise the customer pays the estimated duty and tax at checkout AND is "
            "billed again by the carrier on delivery."
        )

    countries = [row.country for row in (doc.get("allowed_countries") or []) if row.country]
    if len(countries) != 1:
        listed = ", ".join(countries) if countries else "none"
        frappe.throw(
            "A Carrier Service that pre-collects import charges must cover exactly one "
            f"country, because the duty and tax rates apply to that country only. "
            f"Allowed Countries currently lists: {listed}.\n\n"
            "Create one Carrier Service per destination, the way 'Paket Plus Schweiz' "
            "and 'Paket Plus Deutschland' are set up."
        )

    if getattr(doc, "customs_item", None) == getattr(doc, "shipping_item", None):
        frappe.throw(
            "Import Charge Item and Shipping Item must be different Items — the freight "
            "fee and the pre-collected duty are separate lines on the order and are "
            "booked to different accounts."
        )

    _warn_about_charge_item_setup(doc.customs_item)


def _warn_about_charge_item_setup(item_code: str) -> None:
    """Warn when the charge item is not configured as a pass-through.

    Pre-collected import tax is money owed to a foreign authority: it is neither
    our revenue nor subject to our VAT. Two settings on the Item decide that,
    and both fail quietly if left unset — the amount simply lands wherever the
    Item Group or the order's tax template happens to point.

    A warning rather than an error: which account and which zero-rate template
    are correct is an accounting decision, and blocking the Carrier Service
    until it is made would stop shipping over a bookkeeping detail.
    """
    if not item_code or not frappe.db.exists("Item", item_code):
        return

    item = frappe.get_doc("Item", item_code)
    problems = []

    if not [row for row in (item.item_defaults or []) if row.income_account]:
        problems.append(
            "no income account in Item Defaults — set the clearing account, so the "
            "carrier's invoice for the advanced duty can be booked against the same "
            "account instead of the amount showing up as revenue"
        )

    if not (item.taxes or []):
        problems.append(
            "no Item Tax Template — the line inherits the order's tax template, so on "
            "any order that resolves to a domestic VAT template the pass-through would "
            "be charged VAT on top. Add a 0 % template so the item can never attract it"
        )

    if problems:
        frappe.msgprint(
            "Import Charge Item {0} is not set up as a pass-through:<br>• {1}".format(
                frappe.bold(item_code), "<br>• ".join(problems)
            ),
            title="Check the import charge item",
            indicator="orange",
        )


def _warn_about_uncollected_duties(doc: Document) -> None:
    """Warn when we agree to pay the duty but never charge anyone for it.

    "Sender (DDP)" tells the carrier to bill duty and import tax to us. Without
    an Import Charge Item nothing is collected from the customer, so every
    parcel to that destination costs us the duty plus the carrier's advancement
    fee. That can be deliberate — absorbing it is a legitimate pricing choice —
    so this is a warning, not a refusal.
    """
    if getattr(doc, "duties_payment", None) != "Sender (DDP)":
        return
    if getattr(doc, "carrier_key", None) not in CARRIERS_SUPPORTING_DDP:
        return

    frappe.msgprint(
        "This service is set to <b>Sender (DDP)</b> but has no Import Charge Item, "
        "so the carrier bills the duty and import tax to us and nothing is collected "
        "from the customer. Deliberate is fine — otherwise set an Import Charge Item.",
        title="Duty is paid but not charged on",
        indicator="orange",
    )
