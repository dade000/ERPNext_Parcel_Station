# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the Shipment's list of Delivery Notes.

ERPNext's ``make_shipment`` writes one ``Shipment Delivery Note`` row per
Delivery Note ITEM. A Delivery Note with six lines appeared six times on the
Shipment, the freight and customs charge lines among them, each with its own
amount — and the station then put the parcel's value into the first of them.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from . import core


class _Shipment:
    def __init__(self, rows):
        self.rows = [frappe._dict(row) for row in rows]

    def get(self, key):
        return self.rows if key == "shipment_delivery_note" else None

    def set(self, key, value):
        self.rows = list(value)

    def append(self, key, value):
        self.rows.append(frappe._dict(value))

    @property
    def shipment_delivery_note(self):
        return self.rows


class TestCollapseDeliveryNoteRows(FrappeTestCase):
    def test_one_row_per_delivery_note_with_the_amounts_added_up(self):
        doc = _Shipment(
            {"delivery_note": "LS2027000374", "grand_total": amount}
            for amount in (116.67, 116.67, 116.67, 175.0, 13.0, 43.58)
        )
        core._collapse_delivery_note_rows(doc)
        self.assertEqual(len(doc.rows), 1)
        self.assertEqual(doc.rows[0].delivery_note, "LS2027000374")
        self.assertAlmostEqual(doc.rows[0].grand_total, 581.59, places=2)

    def test_different_delivery_notes_stay_apart(self):
        doc = _Shipment(
            [
                {"delivery_note": "DN-1", "grand_total": 10.0},
                {"delivery_note": "DN-2", "grand_total": 5.0},
                {"delivery_note": "DN-1", "grand_total": 2.5},
            ]
        )
        core._collapse_delivery_note_rows(doc)
        self.assertEqual([(row.delivery_note, row.grand_total) for row in doc.rows], [("DN-1", 12.5), ("DN-2", 5.0)])

    def test_already_clean_rows_are_left_alone(self):
        doc = _Shipment([{"delivery_note": "DN-1", "grand_total": 10.0}])
        original = doc.rows
        core._collapse_delivery_note_rows(doc)
        self.assertIs(doc.rows, original)
