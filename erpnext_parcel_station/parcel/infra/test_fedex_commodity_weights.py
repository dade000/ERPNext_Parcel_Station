# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the commodity weights of a FedEx customs shipment.

FedEx rejects a shipment whose commodity weights add up to more than the
package weight (COMMODITYWEIGHT.GREATERTHAN.PACKAGEWEIGHT) — even by one
hundredth of a kilogram. Items without a master weight get a share of the
measured parcel weight, and rounding each share on its own produced exactly
that hundredth: 3.47 kg over four pairs of shoes became 4 x 0.87 = 3.48 kg.
"""

from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from . import fedex_api


def _row(code, weight_per_unit=0.0, qty=1.0):
    return {
        "item_code": code, "item_name": code, "qty": qty, "uom": "Pair",
        "weight_per_unit": weight_per_unit, "rate": 100.0, "currency": "EUR",
        "tariff": "6403", "origin_country": "Austria",
    }


def _cents(values):
    return sum(int(round(value * 100)) for value in values)


class TestWeightShares(FrappeTestCase):
    def test_shares_add_up_to_the_package_weight_exactly(self):
        # Every case here used to come out one hundredth too heavy or too light.
        for parcel, count in ((3.47, 4), (2.0, 3), (1.0, 3), (0.99, 7), (12.345, 6), (5.0, 4)):
            shares = fedex_api._weight_shares(parcel, [], count)
            self.assertEqual(len(shares), count)
            self.assertEqual(_cents(shares), int(round(round(parcel, 2) * 100)), (parcel, count))
            self.assertLessEqual(max(shares) - min(shares), 0.0101, (parcel, count))

    def test_weighed_items_are_taken_off_first(self):
        shares = fedex_api._weight_shares(3.47, [1.2, 0.5], 2)
        self.assertEqual(_cents(shares), 347 - 120 - 50)

    def test_nothing_left_to_distribute(self):
        self.assertEqual(fedex_api._weight_shares(1.0, [1.0], 2), [])
        self.assertEqual(fedex_api._weight_shares(1.0, [1.5], 1), [])
        self.assertEqual(fedex_api._weight_shares(0.02, [], 3), [])
        self.assertEqual(fedex_api._weight_shares(2.0, [], 0), [])


class TestCommodityWeights(FrappeTestCase):
    def _weights(self, contents, parcel_weight, customs=True):
        with (
            patch.object(fedex_api, "collect_parcel_contents", return_value=contents),
            patch.object(fedex_api, "_country_code", return_value="AT"),
        ):
            commodities, _, _ = fedex_api._commodities(
                {}, None, customs_destination=customs, parcel_weight=parcel_weight
            )
        return [commodity.get("weight", {}).get("value") for commodity in commodities]

    def test_four_unweighed_items_never_exceed_the_parcel(self):
        # The production case: four pairs of shoes without master weights.
        weights = self._weights([_row("A"), _row("B"), _row("B"), _row("C")], 3.47)
        self.assertEqual(_cents(weights), 347)

    def test_mixed_weighed_and_unweighed(self):
        weights = self._weights([_row("A", 0.9, 2.0), _row("B"), _row("C")], 3.0)
        self.assertEqual(weights[0], 1.8)
        self.assertEqual(_cents(weights), 300)

    def test_intra_eu_parcels_declare_no_invented_weights(self):
        self.assertEqual(self._weights([_row("A"), _row("B", 0.9)], 2.0, customs=False), [None, 0.9])
