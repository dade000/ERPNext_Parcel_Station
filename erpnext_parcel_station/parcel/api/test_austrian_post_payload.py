# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the element ORDER of the Austrian Post ImportShipment payload.

Why this test exists
--------------------
``ShipmentRow`` & friends are WCF DataContract types: their ``xs:sequence`` is in
ordinal-alphabetical order and the deserializer walks it STRICTLY FORWARD. An
element that arrives after an alphabetically later sibling is silently SKIPPED —
no SOAP fault, no ``errorMessage``, tracking number and label are still returned.

That is exactly how the ``ColloList`` regression stayed invisible: it was emitted
after ``DeliveryServiceThirdPartyID``, so Austrian Post dropped the whole list and
every parcel arrived WITHOUT weight and dimensions, while the shipment looked
perfectly fine in ERPNext. Verified against the carrier's acceptance system —
sending the same shipment with the old order made the response echo back an empty
``ColloRow``; with the fixed order it echoes ``Weight 1.8400, 20/40/30``.

Because the failure mode is completely silent, ordering cannot be checked by
observing the carrier's response in production — it has to be asserted here.

The expected sequences live in ``tests/fixtures/austrian_post_wsdl_sequences.json``
and are extracted from the carrier WSDL. Regenerate them with::

    curl -s 'https://abn-plc.post.at/DataService/Post.Webservice/ShippingService.svc?singleWsdl' -o wsdl.xml
    python3 - <<'EOF'
    import re, json
    w = open('wsdl.xml', encoding='utf-8').read()
    def seq(t):
        m = re.search(r'<xs:complexType name="%s">(.*?)</xs:complexType>' % t, w, re.S)
        return [re.search(r'name="([^"]+)"', e).group(1)
                for e in re.findall(r'<xs:element\b[^>]*/?>', m.group(1))]
    print(json.dumps({t: seq(t) for t in ("ShipmentRow", "ColloRow", "AddressRow", "PrinterRow")}, indent=2))
    EOF
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch
from xml.etree import ElementTree as ET

import frappe
from frappe.tests.utils import FrappeTestCase

from . import carrier

POST_NS = "{http://post.ondot.at}"

# Which WSDL complexType each element of the payload has to satisfy. The keys are
# the XML tags as they appear in the request; ColloRow/AddressRow repeat.
ELEMENT_TYPES = {
    "row": "ShipmentRow",
    "ColloRow": "ColloRow",
    "ColloArticleRow": "ColloArticleRow",
    "OURecipientAddress": "AddressRow",
    "OUShipperAddress": "AddressRow",
    "PrinterObject": "PrinterRow",
}

# Containers whose children are unbounded repetitions of one type rather than a
# sequence of distinct members — their child order carries no meaning.
REPEATING_CONTAINERS = {"ColloList", "ColloArticleList"}

SHIPMENT = {
    "name": "SHIPMENT-00130",
    "delivery_address_name": "Kunde-Versand",
    "pickup_address_name": "Firma-Adresse",
    "delivery_to": "Max Mustermann",
    "pickup_company": "Musterfirma GmbH",
    "description_of_content": "Holzschuh Classic x2, Einlegesohle x1",
    "total_weight": 1.84,
    "shipment_parcel": [{"weight": 1.84, "count": 1, "length": 40, "width": 30, "height": 20}],
    "shipment_delivery_note": [{"delivery_note": "MAT-DN-2026-00180"}],
}

# What _collect_article_rows returns for the shipment above: one position with a
# tariff number, one without (the realistic case — Item.customs_tariff_number is
# largely unmaintained), so the omit-and-log path is covered too.
ARTICLES = [
    {
        "ArticleName": "Holzschuh Classic",
        "ArticleNumber": "00042",
        "ConsumerUnitNetWeight": 0.85,
        "CountryOfOriginID": "AT",
        "CurrencyID": "EUR",
        "CustomsOptionID": 1,
        "HSTariffNumber": "64031910",
        "Quantity": 2.0,
        "UnitID": "PCE",
        "ValueOfGoodsPerUnit": 89.9,
    },
    {
        "ArticleName": "Einlegesohle",
        "ArticleNumber": "00099",
        "Quantity": 1.0,
        "UnitID": "PCE",
    },
]


def _load_sequences() -> dict[str, list[str]]:
    path = os.path.join(
        frappe.get_app_path("erpnext_parcel_station"),
        "..",
        "tests",
        "fixtures",
        "austrian_post_wsdl_sequences.json",
    )
    with open(os.path.normpath(path), encoding="utf-8") as fh:
        return json.load(fh)["sequences"]


class TestAustrianPostPayloadOrder(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sequences = _load_sequences()

    def _build(
        self, shipment: dict | None = None, articles: list | None = None, label_format: str = "zpl"
    ) -> ET.Element:
        """Build the real ImportShipment payload without touching the DB."""
        # Two settings sources since the Austrian Post split: the API identity
        # lives in Austrian Post Settings, the sender fallback block in Parcel
        # Station Settings.
        ap_settings = frappe._dict(
            client_id="21181807",
            org_unit_guid="fbe3ec24-aa9e-4cd2-8754-1c38654244f5",
            org_unit_id="1400024",
        )
        ps_settings = frappe._dict()
        address = {
            "address_line1": "Musterstrasse 1",
            "city": "Wien",
            "country_code": "AT",
            "pincode": "1010",
            "email": "office@example.at",
            "phone": "+43 1 234567",
        }
        with (
            patch.object(carrier, "_get_ap_settings", return_value=ap_settings),
            patch.object(carrier, "_get_ps_settings", return_value=ps_settings),
            patch.object(carrier, "_get_address_fields", return_value=address),
            patch.object(carrier, "_resolve_delivery_service_id", return_value="30"),
            patch.object(
                carrier,
                "_collect_article_rows",
                return_value=ARTICLES if articles is None else articles,
            ),
        ):
            xml = carrier._build_import_shipment_xml(shipment or SHIPMENT, label_format=label_format)
        return ET.fromstring(xml)

    def _printer(self, label_format: str) -> dict[str, str]:
        printer = self._build(label_format=label_format).find(f".//{POST_NS}PrinterObject")
        self._check_container(printer, "PrinterRow")
        return {child.tag.replace(POST_NS, ""): child.text for child in printer}

    def test_label_printer_gets_zpl(self):
        self.assertEqual(self._printer("zpl"), {"LabelFormatID": "100x150", "LanguageID": "ZPL2"})

    def test_pdf_label_comes_on_a_page_of_its_own_size(self):
        # Without PaperLayoutID the carrier puts the label into the corner of
        # an A4 landscape sheet; the WSDL order is checked by _printer.
        self.assertEqual(
            self._printer("pdf"),
            {"LabelFormatID": "100x150", "LanguageID": "pdf", "PaperLayoutID": "A6"},
        )

    def _check_container(self, element: ET.Element, complex_type: str):
        order = self.sequences[complex_type]
        seen: list[tuple[str, int]] = []
        for child in element:
            tag = child.tag.replace(POST_NS, "")
            self.assertIn(
                tag,
                order,
                f"<{tag}> is not a member of {complex_type} — unknown elements are "
                f"silently ignored by the carrier",
            )
            seen.append((tag, order.index(tag)))

        for (previous, previous_idx), (current, current_idx) in zip(seen, seen[1:]):
            self.assertLess(
                previous_idx,
                current_idx,
                f"{complex_type}: <{current}> must be emitted BEFORE <{previous}> "
                f"(WSDL sequence). Out-of-order elements are dropped without any error.",
            )

    def test_every_container_follows_the_wsdl_sequence(self):
        """Walk the whole payload and check each container against the WSDL."""
        root = self._build()
        checked = []

        def walk(element: ET.Element):
            tag = element.tag.replace(POST_NS, "")
            if tag in ELEMENT_TYPES:
                self._check_container(element, ELEMENT_TYPES[tag])
                checked.append(tag)
            elif tag in REPEATING_CONTAINERS:
                pass
            for child in element:
                walk(child)

        walk(root)
        self.assertEqual(
            sorted(checked),
            [
                "ColloArticleRow",
                "ColloArticleRow",
                "ColloRow",
                "OURecipientAddress",
                "OUShipperAddress",
                "PrinterObject",
                "row",
            ],
            "payload no longer contains the expected containers",
        )

    def test_collo_list_precedes_delivery_service_third_party_id(self):
        """The exact regression: ColloList after DeliveryServiceThirdPartyID made
        Austrian Post drop weight AND dimensions, silently."""
        row = self._build().find(f".//{POST_NS}row")
        tags = [child.tag.replace(POST_NS, "") for child in row]
        self.assertLess(
            tags.index("ColloList"),
            tags.index("DeliveryServiceThirdPartyID"),
            "ColloList must precede DeliveryServiceThirdPartyID or the carrier drops it",
        )

    def test_weight_and_dimensions_are_inside_collo_row(self):
        collo_row = self._build().find(f".//{POST_NS}ColloList/{POST_NS}ColloRow")
        values = {child.tag.replace(POST_NS, ""): child.text for child in collo_row}
        self.assertEqual(values["Weight"], "1.84")
        self.assertEqual(values["Height"], "20")
        self.assertEqual(values["Length"], "40")
        self.assertEqual(values["Width"], "30")

    def test_shipper_reference_uses_the_schema_element_name(self):
        """The schema knows OUShipperReference1/2 — a bare "OUShipperReference"
        is an unknown element and is discarded without an error."""
        row = self._build().find(f".//{POST_NS}row")
        tags = [child.tag.replace(POST_NS, "") for child in row]
        self.assertIn("OUShipperReference1", tags)
        self.assertNotIn("OUShipperReference", tags)

    def test_article_list_is_the_first_member_of_collo_row(self):
        """ColloArticleList precedes Height/Length/Weight/Width in the WSDL
        sequence — emitted after them it would be dropped, exactly like the
        ColloList regression."""
        collo_row = self._build().find(f".//{POST_NS}ColloList/{POST_NS}ColloRow")
        tags = [child.tag.replace(POST_NS, "") for child in collo_row]
        self.assertEqual(tags[0], "ColloArticleList")
        self.assertEqual(tags, ["ColloArticleList", "Height", "Length", "Weight", "Width"])

    def test_articles_carry_the_full_customs_data(self):
        article = self._build().find(
            f".//{POST_NS}ColloArticleList/{POST_NS}ColloArticleRow"
        )
        values = {child.tag.replace(POST_NS, ""): child.text for child in article}
        self.assertEqual(
            values,
            {
                "ArticleName": "Holzschuh Classic",
                "ArticleNumber": "00042",
                "ConsumerUnitNetWeight": "0.85",
                "CountryOfOriginID": "AT",
                "CurrencyID": "EUR",
                "CustomsOptionID": "1",
                "HSTariffNumber": "64031910",
                "Quantity": "2",
                "UnitID": "PCE",
                "ValueOfGoodsPerUnit": "89.9",
            },
        )

    def test_article_without_tariff_number_omits_the_field(self):
        """Missing Item.customs_tariff_number must skip HSTariffNumber, not send it
        empty and not block the shipment."""
        articles = self._build().findall(
            f".//{POST_NS}ColloArticleList/{POST_NS}ColloArticleRow"
        )
        self.assertEqual(len(articles), 2)
        tags = [child.tag.replace(POST_NS, "") for child in articles[1]]
        self.assertNotIn("HSTariffNumber", tags)
        self.assertEqual(tags, ["ArticleName", "ArticleNumber", "Quantity", "UnitID"])

    def test_no_articles_omits_the_list_entirely(self):
        """A shipment whose contents cannot be resolved must still produce a valid
        payload — an empty ColloArticleList element would declare "no contents"."""
        collo_row = self._build(articles=[]).find(
            f".//{POST_NS}ColloList/{POST_NS}ColloRow"
        )
        tags = [child.tag.replace(POST_NS, "") for child in collo_row]
        self.assertNotIn("ColloArticleList", tags)
        self.assertEqual(tags[0], "Height")

    def test_article_sequence_constant_matches_the_wsdl(self):
        """_COLLO_ARTICLE_SEQUENCE drives the emit order, so it must itself be a
        subsequence of the WSDL order — this catches a member inserted in the wrong
        spot even when no shipment in the test data happens to carry it."""
        wsdl_order = self.sequences["ColloArticleRow"]
        for member in carrier._COLLO_ARTICLE_SEQUENCE:
            self.assertIn(member, wsdl_order, f"{member} is not a ColloArticleRow member")
        positions = [wsdl_order.index(m) for m in carrier._COLLO_ARTICLE_SEQUENCE]
        self.assertEqual(
            positions,
            sorted(positions),
            f"_COLLO_ARTICLE_SEQUENCE is out of WSDL order: {carrier._COLLO_ARTICLE_SEQUENCE}",
        )

    def test_customs_option_default_is_sale_of_goods(self):
        """Per the carrier's Customs Option table: 1 = Sale of goods. The value is
        documented, not guessed — 5 for instance would declare "Return of goods"."""
        self.assertEqual(carrier.DEFAULT_CUSTOMS_OPTION_ID, 1)

    def test_contact_data_for_customs_is_sent_on_both_addresses(self):
        """Customs requires a phone OR email for sender AND recipient — without them
        the carrier rejects international shipments with SN#10074 / SN#10075. Email
        sits before Name1 and Tel1 AFTER PostalCode in the AddressRow sequence, so
        the order matters as much as the presence."""
        root = self._build()
        for tag in (f"{POST_NS}OURecipientAddress", f"{POST_NS}OUShipperAddress"):
            block = root.find(f".//{tag}")
            tags = [child.tag.replace(POST_NS, "") for child in block]
            self.assertEqual(
                tags,
                ["AddressLine1", "City", "CountryID", "Email", "Name1", "PostalCode", "Tel1"],
                f"{tag}: contact fields missing or out of WSDL sequence",
            )

    def test_unit_id_maps_erpnext_uoms_onto_verified_carrier_codes(self):
        """An unknown UnitID makes the carrier fail the ImportShipment with an
        opaque SqlException, so only verified codes may be emitted and anything
        else has to resolve to None (= omit the field)."""
        self.assertEqual(carrier._unit_id("Nos"), "PCE")
        self.assertEqual(carrier._unit_id("nos"), "PCE")
        # PR is undocumented but accepted, and prints correctly on the customs
        # papers — see _UNIT_ID_BY_UOM.
        self.assertEqual(carrier._unit_id(" Pair "), "PR")
        self.assertEqual(carrier._unit_id("Cubic Meter"), "MTQ")
        self.assertEqual(carrier._unit_id("Kg"), "KGM")
        self.assertEqual(carrier._unit_id("Box"), "BX")
        self.assertEqual(carrier._unit_id("Gram"), "GRM")
        self.assertIsNone(carrier._unit_id("Litre"))  # no documented counterpart
        self.assertIsNone(carrier._unit_id(""))
        self.assertIsNone(carrier._unit_id(None))
        # Every mapped code must be one the carrier actually accepts.
        # Documented "System Units" plus PR, which is verified-accepted and prints
        # correctly on the customs papers.
        documented = {"BE", "BL", "BN", "BO", "BR", "BX", "DMT", "FTQ", "GRM",
                      "KGM", "KTM", "MTQ", "MTR", "PCE", "SA", "PR"}
        self.assertTrue(
            set(carrier._UNIT_ID_BY_UOM.values()) <= documented,
            f"undocumented unit codes: {set(carrier._UNIT_ID_BY_UOM.values()) - documented}",
        )

    def test_content_description_is_sent_as_customs_description(self):
        row = self._build().find(f".//{POST_NS}row")
        values = {child.tag.replace(POST_NS, ""): child.text for child in row}
        self.assertEqual(values["CustomsDescription"], "Holzschuh Classic x2, Einlegesohle x1")

    def test_shipment_without_dimensions_still_sends_weight(self):
        """Shipments created before the dimension scan existed carry no L/W/H —
        they must still send Weight, and stay in sequence."""
        shipment = dict(SHIPMENT, shipment_parcel=[{"weight": 2.5, "count": 1}])
        collo_row = self._build(shipment).find(f".//{POST_NS}ColloList/{POST_NS}ColloRow")
        tags = [child.tag.replace(POST_NS, "") for child in collo_row]
        self.assertEqual(tags, ["ColloArticleList", "Weight"])
        self.assertEqual(collo_row.find(f"{POST_NS}Weight").text, "2.50")
