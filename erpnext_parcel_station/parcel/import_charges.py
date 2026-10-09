"""Pre-calculated import duty and tax for DDP shipments.

Some destinations are outside the EU customs union, and leaving the recipient to
pay import tax at the door is a poor experience — so the charges are estimated at
checkout, collected with the order, and the carrier is told to bill the actual
duty and tax back to us (``dutiesPayment.paymentType: SENDER``, configured as
"Sender (DDP)").

This module is the single place that turns an order value into that amount. The
webshop checkout, the Sales Order line and any later reconciliation all call
``estimate_import_charges`` — deliberately, because three carriers' worth of
duplicated routing logic already taught this codebase what happens when the same
decision is computed in several places.

Why the rates live on Carrier Service
-------------------------------------
Because a Carrier Service already IS the destination: every service in this
system covers exactly one country ("Paket Plus Schweiz", "Paket Plus
Deutschland", "GLS Österreich"), each with its own shipping item. Putting the
rates anywhere else would introduce a second country dimension alongside the one
the master already carries.

Rates change by legislation, not by release — Switzerland moved to 8.1 % import
tax in 2024 and abolished duty on industrial goods (HS 25–97) the same year — so
nothing in this file encodes any country's rate.

What it deliberately does NOT do
--------------------------------
Contact the carrier. FedEx's Rate API can return estimated duties and taxes, but
it is a separate API product (currently not enabled on our project, which answers
403) and it would put a live third-party call in the checkout path. The
arithmetic below is deterministic and offline; the Rate API is worth adding later
as a cross-check, not as the source of truth for what a customer is charged.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe.utils import cstr, flt

#: Charge base options on Carrier Service.
BASE_GOODS_ONLY = "Goods only"
BASE_GOODS_PLUS_SHIPPING = "Goods plus shipping"

#: Carrier Service value that means "we pay the duty, bill it to us".
DDP = "Sender (DDP)"


def get_charge_config(carrier_service: str | None) -> Optional[Dict[str, Any]]:
    """Return the import-charge configuration of a Carrier Service, or None.

    None means this service does not pre-collect — either it has no charge item
    or the record is gone. Callers must treat that as "add no charge line".
    """
    if not carrier_service or not frappe.db.exists("Carrier Service", carrier_service):
        return None

    doc = frappe.get_cached_doc("Carrier Service", carrier_service)
    if not doc.get("customs_item"):
        return None
    return doc.as_dict()


def charge_item_codes() -> set[str]:
    """Item codes used for import-charge lines, across all Carrier Services.

    Used by the customs declaration so a pre-collected tax line is never
    declared as goods — otherwise the destination levies import tax on the
    import tax.
    """
    return {code for code in frappe.get_all("Carrier Service", pluck="customs_item") if code}


@frappe.whitelist()
def estimate_import_charges(
    carrier_service: str | None = None,
    goods_value: float | str = 0,
    shipping_value: float | str = 0,
    currency: str | None = None,
) -> Dict[str, Any]:
    """Estimate what the destination will charge on import.

    ``goods_value`` and ``shipping_value`` are net amounts (excluding our own
    VAT) in ``currency``, which defaults to the company currency. Returns a
    result that is always safe to render:

        {applicable, amount, currency, charge_item, duties_payment,
         breakdown{...}, carrier_service, reason}

    ``applicable`` is False — with ``amount`` 0 and a ``reason`` — when the
    service does not pre-collect or the consignment stays under the de-minimis
    value. Callers must not add a charge line in that case.

    The sequence mirrors how customs authorities assess: duty on the charge
    base, then import tax on base + duty, then the carrier's fee for advancing
    both. Getting that order wrong under-collects, because tax applies to the
    duty as well.
    """
    goods = flt(goods_value)
    shipping = flt(shipping_value)
    currency = cstr(currency) or frappe.defaults.get_global_default("currency") or "EUR"

    empty = {
        "applicable": False,
        "amount": 0.0,
        "currency": currency,
        "charge_item": None,
        "carrier_service": carrier_service,
        "duties_payment": None,
        "breakdown": {},
    }

    config = get_charge_config(carrier_service)
    if not config:
        return {
            **empty,
            "reason": (
                f"Carrier Service {carrier_service or '(none)'} has no Import Charge Item; "
                f"import charges are not pre-collected for this destination."
            ),
        }

    duties_payment = config.get("duties_payment")
    empty = {**empty, "charge_item": config.get("customs_item"), "duties_payment": duties_payment}

    de_minimis = flt(config.get("import_de_minimis_value"))
    if de_minimis and goods < de_minimis:
        return {
            **empty,
            "reason": (
                f"Goods value {goods:.2f} {currency} is below the de-minimis value "
                f"{de_minimis:.2f} configured on {carrier_service}."
            ),
        }

    base = goods
    if config.get("import_charge_base") != BASE_GOODS_ONLY:
        base += shipping

    duty = base * flt(config.get("import_duty_percent")) / 100.0
    tax = (base + duty) * flt(config.get("import_tax_percent")) / 100.0

    advanced = duty + tax
    fee = advanced * flt(config.get("disbursement_fee_percent")) / 100.0
    minimum_fee = flt(config.get("disbursement_fee_minimum"))
    # The minimum only applies once there is something to advance; a consignment
    # with no duty and no tax must not be charged a fee for advancing nothing.
    fee = max(fee, minimum_fee) if advanced > 0 else 0.0

    amount = round(duty + tax + fee, 2)

    return {
        "applicable": amount > 0,
        "amount": amount,
        "currency": currency,
        "charge_item": config.get("customs_item"),
        "carrier_service": carrier_service,
        "duties_payment": duties_payment,
        "breakdown": {
            "charge_base": round(base, 2),
            "goods_value": round(goods, 2),
            "shipping_value": round(shipping, 2),
            "duty": round(duty, 2),
            "duty_percent": flt(config.get("import_duty_percent")),
            "import_tax": round(tax, 2),
            "import_tax_percent": flt(config.get("import_tax_percent")),
            "disbursement_fee": round(fee, 2),
        },
        "reason": None,
    }
