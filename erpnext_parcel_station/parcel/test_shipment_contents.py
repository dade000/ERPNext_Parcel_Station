# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards what does — and does not — get declared to customs.

A Carrier Service's shipping item is a genuine Delivery Note line, so without an
explicit exclusion it reaches the customs declaration as if it were cargo. Two
consequences, both silent:

  * the shipping fee is added to the declared goods value, so the recipient is
    taxed on freight that was already paid;
  * once import charges are pre-collected through their own item (the DDP flow
    for Switzerland), that line would be declared as goods value too — and the
    destination country would levy import VAT on the import VAT.

Neither shows up in a carrier response: the shipment is accepted, the label
prints, and the error only surfaces as an inflated bill at the border.
"""

from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from . import shipment_contents


class TestServiceItemExclusion(FrappeTestCase):
    def _collect(self, rows, service_items):
        with patch.object(shipment_contents.frappe.db, "table_exists", return_value=False), \
             patch.object(shipment_contents.frappe.db, "sql", return_value=rows), \
             patch.object(shipment_contents.frappe.db, "get_value", return_value=None), \
             patch.object(shipment_contents, "service_item_codes", return_value=service_items), \
             patch.object(shipment_contents, "components_by_line", return_value={}):
            return shipment_contents.collect_parcel_contents(
                {"name": "SHIPMENT-TEST", "shipment_delivery_note": [{"delivery_note": "DN-1"}]}
            )

    def test_shipping_item_is_not_declared_as_goods(self):
        rows = [
            {"item_code": "SCHUH-1", "item_name": "Holzschuh", "qty": 1.0, "uom": "Nos",
             "weight_per_unit": 0.9, "delivery_note": "DN-1", "delivery_note_item": None},
            {"item_code": "00011", "item_name": "Versand", "qty": 1.0, "uom": "Nos",
             "weight_per_unit": 0.0, "delivery_note": "DN-1", "delivery_note_item": None},
        ]
        contents = self._collect(rows, {"00011"})
        self.assertEqual([c["item_code"] for c in contents], ["SCHUH-1"])

    def test_import_charge_item_is_not_declared_as_goods(self):
        """Otherwise the destination country taxes the pre-collected import VAT."""
        rows = [
            {"item_code": "SCHUH-1", "item_name": "Holzschuh", "qty": 1.0, "uom": "Nos",
             "weight_per_unit": 0.9, "delivery_note": "DN-1", "delivery_note_item": None},
            {"item_code": "ZOLL-CH", "item_name": "Einfuhrabgaben CH", "qty": 1.0, "uom": "Nos",
             "weight_per_unit": 0.0, "delivery_note": "DN-1", "delivery_note_item": None},
        ]
        contents = self._collect(rows, {"00011", "ZOLL-CH"})
        self.assertEqual([c["item_code"] for c in contents], ["SCHUH-1"])

    def test_a_parcel_of_nothing_but_service_lines_declares_nothing(self):
        """Callers must treat an empty result as "declare nothing", not as an
        error — the FedEx builder falls back to the Shipment's own description."""
        rows = [
            {"item_code": "00011", "item_name": "Versand", "qty": 1.0, "uom": "Nos",
             "weight_per_unit": 0.0, "delivery_note": "DN-1", "delivery_note_item": None},
        ]
        self.assertEqual(self._collect(rows, {"00011"}), [])

    def test_goods_are_untouched_when_nothing_is_a_service_item(self):
        rows = [
            {"item_code": "SCHUH-1", "item_name": "Holzschuh", "qty": 2.0, "uom": "Nos",
             "weight_per_unit": 0.9, "delivery_note": "DN-1", "delivery_note_item": None},
        ]
        contents = self._collect(rows, set())
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["qty"], 2.0)
