# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the arithmetic behind pre-collected import charges.

The amount computed here is charged to a customer at checkout and then owed to a
foreign tax authority, so an error is not a display bug — it is either
under-collecting (we absorb the shortfall on every parcel) or over-collecting
from customers.

Two things in particular are easy to get subtly wrong and impossible to notice
from the outside:

  * import tax is assessed on the duty as well, not only on the goods, so the
    order of operations matters;
  * a consignment with nothing to advance must not be charged the carrier's
    minimum advancement fee.
"""

from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from . import import_charges


def _config(**overrides):
    """Import-charge configuration as it sits on a Carrier Service."""
    base = {
        "name": "CS-FEDEX-CH",
        "service_name": "FedEx International Economy Schweiz",
        "customs_item": "ZOLL-CH",
        "shipping_item": "FEDEX-INTL-ECON-CH",
        "duties_payment": "Sender (DDP)",
        "import_duty_percent": 0.0,
        "import_tax_percent": 8.1,
        "import_charge_base": "Goods plus shipping",
        "import_de_minimis_value": 0.0,
        "disbursement_fee_percent": 0.0,
        "disbursement_fee_minimum": 0.0,
    }
    base.update(overrides)
    return base


class TestEstimateImportCharges(FrappeTestCase):
    def _estimate(self, config, goods, shipping=0.0):
        with patch.object(import_charges, "get_charge_config", return_value=config):
            return import_charges.estimate_import_charges(
                "CS-FEDEX-CH", goods, shipping, currency="EUR"
            )

    def test_swiss_import_tax_on_goods_and_shipping(self):
        result = self._estimate(_config(), goods=178.0, shipping=22.0)
        self.assertTrue(result["applicable"])
        self.assertEqual(result["breakdown"]["charge_base"], 200.0)
        self.assertEqual(result["breakdown"]["import_tax"], 16.2)   # 8.1 % of 200
        self.assertEqual(result["amount"], 16.2)
        self.assertEqual(result["charge_item"], "ZOLL-CH")

    def test_goods_only_base_excludes_shipping(self):
        result = self._estimate(_config(import_charge_base="Goods only"), goods=178.0, shipping=22.0)
        self.assertEqual(result["breakdown"]["charge_base"], 178.0)

    def test_tax_is_assessed_on_the_duty_as_well(self):
        """Charging tax on goods alone under-collects by tax x duty on every
        parcel — invisible until the carrier's invoice arrives."""
        result = self._estimate(
            _config(import_duty_percent=10.0, import_tax_percent=8.1), goods=100.0
        )
        self.assertEqual(result["breakdown"]["duty"], 10.0)
        self.assertEqual(result["breakdown"]["import_tax"], 8.91)   # 8.1 % of 110, not of 100
        self.assertEqual(result["amount"], 18.91)

    def test_disbursement_fee_uses_the_higher_of_percent_and_minimum(self):
        rule = _config(disbursement_fee_percent=2.5, disbursement_fee_minimum=15.0)
        small = self._estimate(rule, goods=100.0)      # tax 8.10, 2.5 % = 0.20 -> minimum wins
        self.assertEqual(small["breakdown"]["disbursement_fee"], 15.0)

        large = self._estimate(rule, goods=10000.0)    # tax 810, 2.5 % = 20.25 -> percent wins
        self.assertEqual(large["breakdown"]["disbursement_fee"], 20.25)

    def test_no_fee_when_there_is_nothing_to_advance(self):
        """A zero-rated destination must not attract a minimum fee for advancing
        nothing."""
        rule = _config(import_tax_percent=0.0, import_duty_percent=0.0,
                     disbursement_fee_minimum=15.0)
        result = self._estimate(rule, goods=500.0)
        self.assertEqual(result["breakdown"]["disbursement_fee"], 0.0)
        self.assertEqual(result["amount"], 0.0)
        self.assertFalse(result["applicable"])

    def test_below_de_minimis_nothing_is_collected(self):
        result = self._estimate(_config(import_de_minimis_value=62.0), goods=50.0)
        self.assertFalse(result["applicable"])
        self.assertEqual(result["amount"], 0.0)
        self.assertIn("de-minimis", result["reason"])

    def test_at_the_de_minimis_threshold_the_charge_applies(self):
        result = self._estimate(_config(import_de_minimis_value=62.0), goods=62.0)
        self.assertTrue(result["applicable"])

    def test_service_without_a_charge_item_is_not_charged(self):
        """An EU service has no Import Charge Item, so nothing is collected."""
        with patch.object(import_charges, "get_charge_config", return_value=None):
            result = import_charges.estimate_import_charges("CS-FEDEX-DE", 500.0, 20.0)
        self.assertFalse(result["applicable"])
        self.assertEqual(result["amount"], 0.0)
        self.assertIsNone(result["charge_item"])
        self.assertIn("no Import Charge Item", result["reason"])


class TestDutiesPaymentIsReported(FrappeTestCase):
    """The estimate carries the duties regime so the webshop can show it.

    It is read, not derived: a Carrier Service covers exactly one country, and
    saving one with an Import Charge Item but without "Sender (DDP)" is refused
    outright — see events.carrier_service._validate_import_charges.
    """

    def test_ddp_service_reports_sender(self):
        with patch.object(import_charges, "get_charge_config", return_value=_config()):
            result = import_charges.estimate_import_charges("CS-FEDEX-CH", 178.0, 22.0, "EUR")
        self.assertEqual(result["duties_payment"], "Sender (DDP)")
        self.assertEqual(result["carrier_service"], "CS-FEDEX-CH")

    def test_service_without_charges_reports_no_regime(self):
        with patch.object(import_charges, "get_charge_config", return_value=None):
            result = import_charges.estimate_import_charges("CS-FEDEX-DE", 178.0, 22.0, "EUR")
        self.assertIsNone(result["duties_payment"])
