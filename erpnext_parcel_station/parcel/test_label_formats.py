# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the label format a station asks for.

A carrier issues a label once, in the format requested at that moment. Ask
for the wrong one and the parcel has a label nobody at that station can
print: ZPL at a desk without a label printer, or a PDF sent raw to a Zebra,
which prints its bytes as garbage. And the label must reach the printer as
the carrier returned it — nothing here may rewrite it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel import label_formats
from erpnext_parcel_station.parcel.api import carrier, shipments_ui
from erpnext_parcel_station.parcel.infra import fedex_api, gls_api


class TestNormalize(FrappeTestCase):
    def test_default_is_zpl(self):
        for empty in (None, "", "  "):
            self.assertEqual(label_formats.normalize(empty), "zpl")

    def test_case_and_whitespace_are_forgiven(self):
        self.assertEqual(label_formats.normalize(" PDF "), "pdf")
        self.assertEqual(label_formats.normalize("Zpl"), "zpl")

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            label_formats.normalize("png")


class TestStoredLabel(FrappeTestCase):
    def _stored(self, files):
        rows = [frappe._dict(file_name=name, file_url=f"/private/files/{name}") for name in files]
        with patch.object(label_formats.frappe, "get_all", return_value=rows) as get_all:
            stored = label_formats.stored_label("SHIPMENT-00150")
        self.assertEqual(get_all.call_args.kwargs["filters"]["file_name"], ("like", "%-Label%"))
        return stored

    def test_format_comes_from_the_newest_label_file(self):
        self.assertEqual(self._stored(["SHIPMENT-00150-Austria-Label49e183.zpl"]).label_format, "zpl")
        self.assertEqual(self._stored(["SHIPMENT-00150-GLS-Label.pdf"]).label_format, "pdf")

    def test_no_label_means_none(self):
        self.assertIsNone(self._stored([]))


class TestCarrierRequests(FrappeTestCase):
    def test_gls_printing_options(self):
        self.assertEqual(gls_api._return_labels("zpl"), {"TemplateSet": "ZPL_200", "LabelFormat": "ZEBRA"})
        self.assertEqual(gls_api._return_labels("pdf"), {"TemplateSet": "NONE", "LabelFormat": "PDF"})

    def test_fedex_format_overrides_the_settings_only_when_asked(self):
        creds = fedex_api.FedExCredentials(
            base_url="https://apis-sandbox.fedex.com",
            client_key="k",
            client_secret="s",
            account_number="1",
            mode="Sandbox",
            label_resolution=300,
        )
        self.assertIs(fedex_api._credentials_for_format(creds, None), creds)
        self.assertIs(fedex_api._credentials_for_format(creds, "zpl"), creds)
        pdf = fedex_api._credentials_for_format(creds, "pdf")
        self.assertEqual((pdf.label_image_type, pdf.label_stock_type, pdf.label_resolution), ("PDF", "STOCK_4X6", 203))
        # Settings on PDF, station with a label printer: the printer wins.
        archived = fedex_api._credentials_for_format(pdf, "zpl")
        self.assertEqual(archived.label_image_type, "ZPLII")

    def test_post_label_is_stored_as_returned(self):
        # The label used to be rewritten (weight field). It must reach the
        # printer byte for byte as the carrier sent it.
        self.assertFalse(hasattr(carrier, "_inject_weight_into_zpl"))
        zpl = "^XA^FO10,10^FD0,00 kg^FS^XZ"
        response = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            '<ImportShipmentResponse xmlns="http://post.ondot.at"><ImportShipmentResult>'
            "<ColloCodeRow><Code>1019012500001510269515</Code></ColloCodeRow>"
            "</ImportShipmentResult><pdfData>JVBERi0=</pdfData>"
            f"<zplLabelData>{zpl}</zplLabelData>"
            "</ImportShipmentResponse></s:Body></s:Envelope>"
        )
        parsed = carrier._parse_import_response(response)
        self.assertEqual(parsed["zpl"], zpl)
        self.assertEqual(parsed["pdf"], "JVBERi0=")
        self.assertTrue(parsed["pdf_present"])


class TestRequestLabel(FrappeTestCase):
    def test_result_reports_the_format_actually_stored(self):
        # Asked for ZPL, but this Shipment already has a PDF label.
        stored = frappe._dict(file_url="/private/files/l.pdf", file_name="l.pdf", label_format="pdf")
        with (
            patch.object(shipments_ui, "_request_label", return_value={"shipment": "SHIPMENT-1", "zpl_present": True}),
            patch.object(shipments_ui.label_formats, "stored_label", return_value=stored),
        ):
            result = shipments_ui.request_label("SHIPMENT-1", label_format="zpl")
        self.assertEqual(result["label_format"], "pdf")
        self.assertFalse(result["zpl_present"])
        self.assertTrue(result["pdf_present"])
        self.assertEqual(result["label_file_url"], "/private/files/l.pdf")

    def test_pdf_label_is_refused_for_a_raw_queue(self):
        stored = frappe._dict(file_url="/private/files/l.pdf", file_name="l.pdf", label_format="pdf")
        with patch.object(shipments_ui.label_formats, "stored_label", return_value=stored):
            with self.assertRaises(frappe.ValidationError):
                shipments_ui._load_latest_zpl("SHIPMENT-1")
