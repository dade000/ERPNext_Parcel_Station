# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""A Product Bundle on a Delivery Note is packed as its components.

The bundle line is a non-stock Item priced as one; the goods are the Packed
Item rows under it. Before this, the packing list hid the bundle line as a
"service" and never looked at the packed rows, so a bundle-only Delivery Note
showed nothing to pack and nothing could be shipped.
"""

from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from . import bundles


def _packed(name, item_code, qty, rate=0.0, line="LINE-BUNDLE"):
    return {
        "name": name, "parent_detail_docname": line, "parent_item": "SOCKEN-5ER-BUNDLE",
        "item_code": item_code, "item_name": item_code, "description": "",
        "qty": qty, "uom": "Pair", "rate": rate,
    }


class TestSplitLineValue(FrappeTestCase):
    def test_equal_components_share_the_line_value_by_quantity(self):
        """Five pairs for 39.00 are 7.80 a pair, and five times that is the line."""
        rates = bundles.split_line_value(39.0, [_packed("P1", "SOCKE", 5)])
        self.assertAlmostEqual(rates["P1"], 7.8)

    def test_two_components_without_rates_split_by_quantity(self):
        rates = bundles.split_line_value(30.0, [_packed("P1", "A", 2), _packed("P2", "B", 1)])
        self.assertAlmostEqual(rates["P1"], 10.0)
        self.assertAlmostEqual(rates["P2"], 10.0)
        self.assertAlmostEqual(rates["P1"] * 2 + rates["P2"] * 1, 30.0)

    def test_packed_rates_weight_the_split_when_present(self):
        """A shoe and a sock in one bundle: the shoe carries most of the value."""
        rates = bundles.split_line_value(
            100.0, [_packed("P1", "SCHUH", 1, rate=90.0), _packed("P2", "SOCKE", 1, rate=10.0)]
        )
        self.assertAlmostEqual(rates["P1"], 90.0)
        self.assertAlmostEqual(rates["P2"], 10.0)

    def test_nothing_to_split_is_zero_not_a_crash(self):
        self.assertEqual(bundles.split_line_value(39.0, []), {})
        self.assertEqual(bundles.split_line_value(39.0, [_packed("P1", "A", 0)]), {"P1": 0.0})


class TestComponentsByLine(FrappeTestCase):
    def _components(self, packed_rows, dn_items, item_weights=None):
        def fake_get_all(doctype, **kwargs):
            if doctype == "Packed Item":
                return packed_rows
            if doctype == "Item":
                return [
                    {"name": code, "weight_per_unit": w} for code, w in (item_weights or {}).items()
                ]
            raise AssertionError(doctype)

        with patch.object(bundles.frappe, "get_all", side_effect=fake_get_all):
            return bundles.components_by_line("DN-1", dn_items)

    def test_bundle_line_yields_its_packed_components(self):
        line = {"name": "LINE-BUNDLE", "item_code": "SOCKEN-5ER-BUNDLE",
                "item_name": "Socken 5er-Bundle", "qty": 1, "amount": 39.0, "net_amount": 32.5}
        result = self._components([_packed("P1", "SOCKE-39-42", 5)], [line], {"SOCKE-39-42": 0.05})

        self.assertEqual(list(result), ["LINE-BUNDLE"])
        comp = result["LINE-BUNDLE"][0]
        self.assertEqual(comp["name"], "P1")
        self.assertEqual(comp["packed_item"], "P1")
        self.assertEqual(comp["delivery_note_item"], "LINE-BUNDLE")
        self.assertEqual(comp["item_code"], "SOCKE-39-42")
        self.assertEqual(comp["qty"], 5.0)
        self.assertAlmostEqual(comp["rate"], 7.8)
        self.assertAlmostEqual(comp["net_rate"], 6.5)
        self.assertEqual(comp["weight_per_unit"], 0.05)
        self.assertEqual(comp["bundle_item_name"], "Socken 5er-Bundle")

    def test_a_note_without_bundles_has_no_components(self):
        self.assertEqual(self._components([], [{"name": "LINE-1", "item_code": "SCHUH"}]), {})

    def test_packed_rows_of_an_unknown_line_are_ignored(self):
        """Nothing to inherit value from, so nothing is offered rather than a
        component at zero value."""
        result = self._components([_packed("P1", "SOCKE", 5, line="ELSEWHERE")],
                                  [{"name": "LINE-1", "item_code": "SCHUH"}])
        self.assertEqual(result, {})


class TestExpandLines(FrappeTestCase):
    def test_bundle_lines_are_replaced_and_other_rows_pass_through(self):
        shoe = {"name": "LINE-1", "item_code": "SCHUH"}
        bundle = {"name": "LINE-BUNDLE", "item_code": "SOCKEN-5ER-BUNDLE"}
        comps = {"LINE-BUNDLE": [{"name": "P1", "item_code": "SOCKE"}]}
        self.assertEqual(
            bundles.expand_lines([shoe, bundle], comps),
            [shoe, {"name": "P1", "item_code": "SOCKE"}],
        )


class TestComponentQtyForBundles(FrappeTestCase):
    def test_packed_qty_is_the_whole_line_so_per_bundle_is_divided(self):
        """Two bundles on the line pack 10 pairs; sending one bundle is 5."""
        self.assertEqual(bundles.component_qty_for_bundles({"qty": 10}, line_qty=2, bundles=1), 5.0)
        self.assertEqual(bundles.component_qty_for_bundles({"qty": 10}, line_qty=2, bundles=2), 10.0)
