# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the weight the desk sends along with create_shipment_from_barcode.

With the local hardware bridge the browser reads the scale itself and sends
the stable weight (source "scale"); without a bridge the operator can type it
in (source "manual"). Either way the server must use exactly that value and
must not contact the server-side scale, which may hang on another machine.
Without a client weight the old behaviour stays: server scale, then article
weights.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from . import core


class _Doc(SimpleNamespace):
    def append(self, table, row):
        getattr(self, table).append(SimpleNamespace(**row))


def _shipment(total=0.8, parcels=1):
    return _Doc(name="SHIP-TEST", total_weight=total,
                shipment_parcel=[SimpleNamespace(weight=total, count=1) for _ in range(parcels)])


class TestParseClientWeight(FrappeTestCase):
    def test_nothing_sent_means_server_decides(self):
        self.assertIsNone(core._parse_client_weight())
        self.assertIsNone(core._parse_client_weight("", "scale"))

    def test_scale_and_manual_are_accepted(self):
        self.assertEqual(core._parse_client_weight(1.56, "scale"), (1.56, "scale"))
        self.assertEqual(core._parse_client_weight("2.5", "MANUAL"), (2.5, "manual"))

    def test_rounded_to_grams(self):
        self.assertEqual(core._parse_client_weight(1.23456, "scale"), (1.235, "scale"))

    def test_zero_negative_and_absurd_weights_are_rejected(self):
        for bad in (0, -1, "abc", 1001):
            with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
                core._parse_client_weight(bad, "scale")

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            core._parse_client_weight(1.0, "guess")
        with self.assertRaises(frappe.ValidationError):
            core._parse_client_weight(1.0, None)


class TestApplyScaleWeight(FrappeTestCase):
    def test_client_weight_wins_and_server_scale_is_not_called(self):
        doc = _shipment(total=0.8, parcels=2)
        with patch.object(core, "read_scale_weight") as server_scale:
            breakdown = core._apply_scale_weight(doc, (1.56, "scale"))
        server_scale.assert_not_called()
        self.assertEqual(doc.total_weight, 1.56)
        self.assertEqual([p.weight for p in doc.shipment_parcel], [1.56, 1.56])
        self.assertEqual(breakdown["source"], "client_scale")
        self.assertEqual(breakdown["fallback_weight_kg"], 0.8)
        # Same shape as read_scale_weight(), so the desk message keeps working.
        self.assertEqual(breakdown["scale"]["status"], "success")

    def test_manual_weight_creates_parcel_row_when_missing(self):
        doc = _shipment(total=0, parcels=0)
        with patch.object(core, "read_scale_weight") as server_scale:
            breakdown = core._apply_scale_weight(doc, (3.0, "manual"))
        server_scale.assert_not_called()
        self.assertEqual(len(doc.shipment_parcel), 1)
        self.assertEqual(doc.shipment_parcel[0].weight, 3.0)
        self.assertEqual(breakdown["source"], "client_manual")

    def test_without_client_weight_server_scale_is_used_as_before(self):
        doc = _shipment(total=0.8)
        with patch.object(core, "read_scale_weight", return_value={"status": "success", "weight_kg": 2.2}):
            breakdown = core._apply_scale_weight(doc)
        self.assertEqual((doc.total_weight, breakdown["source"]), (2.2, "scale"))

    def test_without_anything_article_weights_stay(self):
        doc = _shipment(total=0.8)
        with patch.object(core, "read_scale_weight", return_value={"status": "error", "code": "network"}):
            breakdown = core._apply_scale_weight(doc)
        self.assertEqual((doc.total_weight, breakdown["source"]), (0.8, "item_metadata"))
