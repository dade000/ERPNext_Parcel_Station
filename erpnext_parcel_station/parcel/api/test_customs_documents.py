# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Covers the customs-documents path: parsing ``shipmentDocuments`` out of the
carrier response and turning it into an attachment.

The carrier returns the CN23 customs papers as a base64 A4 PDF in
``shipmentDocuments``, unrequested, but ONLY when the customs declaration is
complete — an incomplete one (e.g. a missing ``HSTariffNumber``) answers SN#10076
and returns tracking and label but no documents. Both shapes are asserted here so
a domestic shipment never grows an empty PDF attachment.
"""

from __future__ import annotations

import base64

import re

from frappe.tests.utils import FrappeTestCase

from . import carrier
from . import shipments_ui

PDF_BYTES = b"%PDF-1.4 fake customs declaration"
PDF_B64 = base64.b64encode(PDF_BYTES).decode()


def _response(documents_element: str) -> str:
    return f"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
    <ImportShipmentResponse xmlns="http://post.ondot.at">
      <ImportShipmentResult xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
        <ColloRow>
          <ColloCodeList><ColloCodeRow><Code>1019012500001403907562</Code>
          <NumberTypeID>213</NumberTypeID></ColloCodeRow></ColloCodeList>
          <Weight>1.8400</Weight>
        </ColloRow>
      </ImportShipmentResult>
      <zplLabelData>^XA^FDlabel^FS^XZ</zplLabelData>
      <pdfData xmlns:i="http://www.w3.org/2001/XMLSchema-instance" i:nil="true"/>
      {documents_element}
    </ImportShipmentResponse></s:Body></s:Envelope>"""


class TestCustomsDocumentsParsing(FrappeTestCase):
    def test_customs_pdf_is_returned_as_data_not_just_a_flag(self):
        """The attachment step needs the bytes, so the parser must hand back the
        payload — reporting only "present" was what made the PDF unusable."""
        parsed = carrier._parse_import_response(_response(f"<shipmentDocuments>{PDF_B64}</shipmentDocuments>"))

        self.assertTrue(parsed["shipment_documents_present"])
        self.assertEqual(parsed["shipment_documents"], PDF_B64)
        self.assertEqual(base64.b64decode(parsed["shipment_documents"]), PDF_BYTES)
        # The label must still come through untouched alongside the documents.
        self.assertEqual(parsed["codes"], ["1019012500001403907562"])
        self.assertIn("^XA", parsed["zpl"])

    def test_nil_documents_yield_no_payload(self):
        """Domestic/EU shipments answer with i:nil — no attachment may be created."""
        parsed = carrier._parse_import_response(
            _response('<shipmentDocuments xmlns:i="http://www.w3.org/2001/XMLSchema-instance" i:nil="true"/>')
        )

        self.assertFalse(parsed["shipment_documents_present"])
        self.assertIsNone(parsed["shipment_documents"])

    def test_empty_documents_element_yields_no_payload(self):
        parsed = carrier._parse_import_response(_response("<shipmentDocuments></shipmentDocuments>"))

        self.assertFalse(parsed["shipment_documents_present"])
        self.assertIsNone(parsed["shipment_documents"])

    def test_missing_documents_element_yields_no_payload(self):
        parsed = carrier._parse_import_response(_response(""))

        self.assertFalse(parsed["shipment_documents_present"])
        self.assertIsNone(parsed["shipment_documents"])

    def test_lookup_pattern_matches_the_stored_filename(self):
        """save_file inserts a random hash BEFORE the extension, so the SQL LIKE
        used by the print/download paths has to wildcard in the middle. Matching on
        the plain suffix silently found nothing — the PDF was attached but neither
        printable nor downloadable."""
        stored = f"SHIPMENT-00134{shipments_ui.CUSTOMS_DOCUMENTS_SUFFIX}".replace(
            ".pdf", "9b3c83.pdf"
        )
        self.assertEqual(stored, "SHIPMENT-00134-Customs-Documents9b3c83.pdf")

        as_regex = "^" + re.escape(shipments_ui.CUSTOMS_DOCUMENTS_LIKE).replace("%", ".*") + "$"
        self.assertRegex(stored, as_regex)
        # And the un-hashed name must still match, for files stored without a hash.
        self.assertRegex(f"SHIPMENT-00134{shipments_ui.CUSTOMS_DOCUMENTS_SUFFIX}", as_regex)
        # It must not swallow the ZPL label of the same shipment.
        self.assertNotRegex("SHIPMENT-00134-Austria-Label4bf678.zpl", as_regex)

    def test_unparsable_response_still_reports_the_keys(self):
        """The failure branch must keep the same shape, or request_label would hit a
        KeyError instead of simply attaching nothing."""
        parsed = carrier._parse_import_response("not xml at all")

        self.assertFalse(parsed["shipment_documents_present"])
        self.assertIsNone(parsed["shipment_documents"])
