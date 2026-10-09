# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards how tightly a Carrier Service is scoped — and when it is not.

Country scope is a means, not an end. A zone service covering several EU
countries with one shipping item is a perfectly good way to model shipping, and
nothing here should get in its way. The constraint exists only where a service
carries duty and tax rates: those belong to exactly one country, so applying one
country's rates to a five-country service would silently over- or under-collect
from customers in four of them.

The failure is invisible in every system involved — the order is placed, the
label prints, and the discrepancy surfaces as a customs bill weeks later — so
the boundary is asserted here rather than left to review.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from .carrier_service import _validate_import_charges


def _service(countries=(), **overrides):
    fields = {
        "doctype": "Carrier Service",
        "name": "CS-TEST",
        "carrier": "FedEx",
        "carrier_key": "FEDEX",
        "shipping_item": "FEDEX-REG-ECON-EU",
        "customs_item": None,
        "duties_payment": "Recipient (DAP)",
    }
    fields.update(overrides)
    doc = SimpleNamespace(**fields)
    rows = [SimpleNamespace(country=c) for c in countries]
    doc.get = lambda key, _r=rows: _r if key == "allowed_countries" else fields.get(key)
    return doc


class TestZoneServicesWithoutDuties(FrappeTestCase):
    """No duties, no country constraint."""

    def test_a_five_country_eu_zone_service_is_allowed(self):
        doc = _service(("Germany", "France", "Italy", "Netherlands", "Belgium"))
        _validate_import_charges(doc)  # must not raise

    def test_a_service_without_any_country_list_is_allowed(self):
        _validate_import_charges(_service(()))

    def test_a_single_country_service_is_allowed(self):
        _validate_import_charges(_service(("Austria",)))


class TestServicesThatPreCollectDuties(FrappeTestCase):
    """Duties pin the service to one country and to DDP."""

    def test_a_zone_service_may_not_carry_duty_rates(self):
        doc = _service(
            ("Germany", "France", "Italy", "Netherlands", "Belgium"),
            customs_item="ZOLL-CH",
            duties_payment="Sender (DDP)",
        )
        with self.assertRaises(frappe.ValidationError) as ctx:
            _validate_import_charges(doc)
        self.assertIn("exactly one country", str(ctx.exception))

    def test_duty_rates_without_any_country_are_refused(self):
        doc = _service((), customs_item="ZOLL-CH", duties_payment="Sender (DDP)")
        with self.assertRaises(frappe.ValidationError):
            _validate_import_charges(doc)

    def test_one_country_with_ddp_is_the_supported_shape(self):
        doc = _service(("Switzerland",), customs_item="ZOLL-CH", duties_payment="Sender (DDP)")
        _validate_import_charges(doc)  # must not raise

    def test_collecting_charges_while_billing_the_recipient_is_refused(self):
        """Otherwise the customer pays at checkout and again at the door."""
        doc = _service(("Switzerland",), customs_item="ZOLL-CH", duties_payment="Recipient (DAP)")
        with self.assertRaises(frappe.ValidationError) as ctx:
            _validate_import_charges(doc)
        self.assertIn("Sender (DDP)", str(ctx.exception))

    def test_the_charge_item_must_differ_from_the_shipping_item(self):
        doc = _service(
            ("Switzerland",),
            customs_item="FEDEX-REG-ECON-EU",
            shipping_item="FEDEX-REG-ECON-EU",
            duties_payment="Sender (DDP)",
        )
        with self.assertRaises(frappe.ValidationError) as ctx:
            _validate_import_charges(doc)
        self.assertIn("different Items", str(ctx.exception))


class TestCarriersThatCannotBeToldWhoPays(FrappeTestCase):
    """Pre-collecting only works where the carrier can be instructed.

    ``duties_payment`` reaches exactly one payload: FedEx's. Configuring an
    Import Charge Item on a GLS or Austrian Post service would take the money at
    checkout while the carrier went on billing the recipient at the border — the
    customer pays twice, and the difference stays with us. Nothing downstream
    would report it, so it is refused at save time.
    """

    def test_import_charges_are_refused_on_a_carrier_without_a_duties_instruction(self):
        doc = _service(
            ("Switzerland",),
            carrier="Österreichische Post",
            carrier_key="AUSTRIAN_POST",
            customs_item="ZOLL-CH",
            duties_payment="Sender (DDP)",
        )
        with self.assertRaises(frappe.ValidationError) as ctx:
            _validate_import_charges(doc)
        self.assertIn("pay twice", str(ctx.exception))

    def test_a_gls_service_may_still_ship_without_charges(self):
        _validate_import_charges(
            _service(("Austria",), carrier="GLS", carrier_key="GLS")
        )


class TestDutyPaidButNotCharged(FrappeTestCase):
    """DDP without an Import Charge Item costs us the duty on every parcel.

    A warning rather than a refusal: absorbing the duty is a legitimate pricing
    decision. Silently absorbing it because a field was left empty is not.
    """

    def test_ddp_without_a_charge_item_warns(self):
        doc = _service(("Switzerland",), duties_payment="Sender (DDP)")
        with patch.object(frappe, "msgprint") as msgprint:
            _validate_import_charges(doc)
        msgprint.assert_called_once()
        self.assertIn("nothing is collected", msgprint.call_args[0][0])

    def test_dap_without_a_charge_item_is_silent(self):
        doc = _service(("Switzerland",), duties_payment="Recipient (DAP)")
        with patch.object(frappe, "msgprint") as msgprint:
            _validate_import_charges(doc)
        msgprint.assert_not_called()
