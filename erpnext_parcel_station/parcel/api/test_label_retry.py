# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the label retry on a Shipment whose first label call failed.

The station keeps such a Shipment on purpose (a carrier timeout is no proof
that nothing was created there) and finishes it by asking for the label
again. The retry arrives through the same call as a new parcel, with the same
items — and those items are already recorded on the Shipment. Checking them
against "what is left" rejected every retry: the cause was fixed (a missing
state on a US address), but the parcel could not be finished.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from . import core

ITEMS = [{"delivery_note_item": "dni-1", "qty": 1}]


def _existing(tracking=None):
    return [frappe._dict(name="SHIPMENT-00147", docstatus=1, tracking=tracking)]


class TestLabelRetry(FrappeTestCase):
    def _create(self, existing, label_result=None, label_error=None):
        request_label = patch(
            "erpnext_parcel_station.parcel.api.shipments_ui.request_label",
            return_value=label_result,
            side_effect=label_error,
        )
        with (
            patch.object(core, "_resolve_delivery_note_from_barcode", return_value="MAT-DN-2026-00375"),
            patch.object(core.frappe, "get_doc", return_value=frappe._dict(name="MAT-DN-2026-00375")),
            patch.object(core.frappe.db, "get_value", return_value="MAT-DN-2026-00375"),
            patch.object(core, "_existing_shipments_for_delivery", return_value=existing),
            patch.object(core, "_prepare_selected_items") as prepare,
            patch.object(core.frappe, "log_error"),
            request_label as label,
        ):
            result = core.create_shipment_from_barcode("MAT-DN-2026-00375", items=ITEMS)
        return result, prepare, label

    def test_retry_reuses_the_shipment_without_taking_quantities_again(self):
        result, prepare, label = self._create(
            _existing(),
            label_result={"tracking_number": "794858428184", "label_file_url": "/files/l.zpl", "carrier": "FEDEX"},
        )
        prepare.assert_not_called()
        label.assert_called_once_with("SHIPMENT-00147", label_format="zpl")
        self.assertEqual(result["status"], "exists")
        self.assertEqual(result["shipment"], "SHIPMENT-00147")
        self.assertTrue(result["label_created"])
        self.assertEqual(result["tracking_number"], "794858428184")

    def test_failed_retry_reports_the_error_and_keeps_the_shipment(self):
        result, prepare, _ = self._create(_existing(), label_error=frappe.ValidationError("STATE.REQUIRED"))
        prepare.assert_not_called()
        self.assertEqual(result["shipment"], "SHIPMENT-00147")
        self.assertFalse(result["label_created"])
        self.assertIn("STATE.REQUIRED", result["label_error"])

    def test_retry_asks_for_the_format_of_the_station(self):
        with (
            patch.object(core, "_resolve_delivery_note_from_barcode", return_value="MAT-DN-2026-00375"),
            patch.object(core.frappe, "get_doc", return_value=frappe._dict(name="MAT-DN-2026-00375")),
            patch.object(core.frappe.db, "get_value", return_value="MAT-DN-2026-00375"),
            patch.object(core, "_existing_shipments_for_delivery", return_value=_existing()),
            patch("erpnext_parcel_station.parcel.api.shipments_ui.request_label", return_value={}) as label,
        ):
            core.create_shipment_from_barcode("MAT-DN-2026-00375", items=ITEMS, label_format="PDF")
        label.assert_called_once_with("SHIPMENT-00147", label_format="pdf")

    def test_labelled_shipment_is_returned_untouched(self):
        result, prepare, label = self._create(_existing(tracking="794858428184"))
        prepare.assert_not_called()
        label.assert_not_called()
        self.assertTrue(result["label_created"])
