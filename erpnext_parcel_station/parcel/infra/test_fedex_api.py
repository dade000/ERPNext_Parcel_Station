# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the FedEx payload decisions that were established against the live
sandbox and cannot be re-checked from the response alone.

The one that matters most is the customs split. FedEx accepts
``customsClearanceDetail`` on every shipment and decides for itself whether the
destination needs clearance — AT->DE answers ``requiredDocuments:
["AIR_WAYBILL"]``, AT->CH additionally demands
``COMMERCIAL_OR_PRO_FORMA_INVOICE``. Electronic Trade Documents, on the other
hand, must NOT be requested intra-EU, and when they are requested the document
has to be declared alongside them or FedEx rejects the whole shipment with
``SHIPPING.DOCUMENT.REQUIRED``.

Both halves of that rule are invisible in a successful response, so they are
asserted here rather than observed in production.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ..customs import crosses_customs_border, is_eu
from . import fedex_api


class _FakeResponse:
    """Stand-in for requests.Response carrying just what _error_message reads."""

    def __init__(self, payload=None, text="", status_code=400, reason="Bad Request"):
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.reason = reason

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _address(**fields):
    base = {
        "name": "ADDR-TEST",
        "address_line1": "Musterstrasse 1",
        "address_line2": None,
        "city": "Wien",
        "state": None,
        "pincode": "1010",
        "country": "Austria",
        "phone": "+4312345678",
        "email_id": "info@example.com",
    }
    base.update(fields)
    doc = SimpleNamespace(**base)
    doc.get = lambda key, _b=base: _b.get(key)
    return doc


def _shipment_doc():
    fields = {
        "name": "SHIPMENT-TEST",
        "doctype": "Shipment",
        "pickup_address_name": "PICKUP",
        "delivery_address_name": "DELIVERY",
        "pickup": None,
        "pickup_company": "Musterfirma GmbH",
        "pickup_contact_person": None,
        "pickup_contact_email": "info@example.com",
        "delivery_to": None,
        "delivery_customer": "Max Mustermann",
        "delivery_company": None,
        "delivery_contact_name": None,
        "delivery_contact_email": "kunde@example.com",
        "carrier_service": "CS-FEDEX",
        "custom_carrier_service": None,
        "currency": "EUR",
        "value_of_goods": 17.0,
        "description_of_content": "Holzschuhe",
    }
    doc = SimpleNamespace(**fields)
    doc.get = lambda key, _f=fields: _f.get(key)
    doc.as_dict = lambda _f=fields: dict(_f)
    return doc


def _credentials(**overrides):
    base = {
        "base_url": "https://apis-sandbox.fedex.com",
        "client_key": "key",
        "client_secret": "secret",
        "account_number": "740561073",
        "mode": "Sandbox",
    }
    base.update(overrides)
    return fedex_api.FedExCredentials(**base)


class TestCustomsBorder(FrappeTestCase):
    def test_eu_membership(self):
        for code in ("AT", "DE", "at", "NL"):
            self.assertTrue(is_eu(code), code)
        for code in ("CH", "GB", "US", "NO"):
            self.assertFalse(is_eu(code), code)

    def test_unknown_country_is_treated_as_no_customs(self):
        """Matches the historical GLS behaviour: never derive a customs
        declaration from missing data."""
        self.assertTrue(is_eu(None))
        self.assertTrue(is_eu(""))

    def test_border_crossing(self):
        self.assertFalse(crosses_customs_border("AT", "DE"))
        self.assertTrue(crosses_customs_border("AT", "CH"))
        self.assertTrue(crosses_customs_border("CH", "AT"))


class TestErrorMessage(FrappeTestCase):
    def test_code_and_message_are_both_kept(self):
        response = _FakeResponse(
            {"errors": [{"code": "PHONENUMBER.EMPTY", "message": "Phone number is empty."}]}
        )
        self.assertEqual(
            fedex_api._error_message(response), "PHONENUMBER.EMPTY: Phone number is empty."
        )

    def test_falls_back_to_status_when_body_is_not_json(self):
        response = _FakeResponse(None, text="", status_code=503, reason="Service Unavailable")
        self.assertEqual(fedex_api._error_message(response), "HTTP 503 Service Unavailable")

    def test_message_is_capped(self):
        response = _FakeResponse({"errors": [{"code": "X", "message": "y" * 400}]})
        self.assertLessEqual(len(fedex_api._error_message(response)), 250)


class TestQuantityUnits(FrappeTestCase):
    def test_known_uoms_map_to_fedex_codes(self):
        self.assertEqual(fedex_api._quantity_units("Nos"), "PCS")
        self.assertEqual(fedex_api._quantity_units("Paar"), "PRS")
        self.assertEqual(fedex_api._quantity_units("Kg"), "KG")

    def test_unknown_uom_falls_back_rather_than_passing_through(self):
        """FedEx rejects unknown quantity units, so an unmapped UOM must never
        reach the payload verbatim."""
        self.assertEqual(fedex_api._quantity_units("Schachtel"), "PCS")
        self.assertEqual(fedex_api._quantity_units(None), "PCS")


class TestBuildPayload(FrappeTestCase):
    """Payload assembly with every DB read stubbed out."""

    def _build(self, destination_country="Germany", contents=None, service=None):
        """Builds a payload with every DB read stubbed, so the assertions do not
        depend on which Carrier Services happen to exist on the live site."""
        addresses = {
            "PICKUP": _address(name="PICKUP"),
            "DELIVERY": _address(
                name="DELIVERY",
                address_line1="Marienplatz 8",
                city="Muenchen",
                pincode="80331",
                country=destination_country,
                phone="+4989123456",
            ),
        }
        service = service if service is not None else SimpleNamespace(
            get=lambda k: {
                "service_code": "FEDEX_INTERNATIONAL_PRIORITY",
                "packaging_type": "YOUR_PACKAGING",
            }.get(k),
        )
        if service is not None:
            service.get = service.get if hasattr(service, "get") else (lambda k: None)

        with patch.object(fedex_api, "_address_doc", side_effect=lambda n: addresses.get(n)), \
             patch.object(fedex_api, "_carrier_service_doc", return_value=service), \
             patch.object(fedex_api, "collect_parcel_contents", return_value=contents or []), \
             patch.object(fedex_api, "_resolve_label_weight", return_value=1.84), \
             patch.object(fedex_api, "_parcel_dimensions", return_value={"length": 40, "width": 30, "height": 20}), \
             patch.object(fedex_api, "_country_code", side_effect=lambda c: {"Austria": "AT", "Germany": "DE", "Switzerland": "CH"}.get(c, "AT")):
            return fedex_api._build_payload(_shipment_doc(), _credentials())

    def test_customs_detail_is_sent_even_intra_eu(self):
        """FedEx evaluates the country pair itself and does not start a
        clearance for intra-EU parcels — sending the data is safe and keeps one
        code path for every destination."""
        payload = self._build("Germany")
        self.assertIn("customsClearanceDetail", payload["requestedShipment"])

    def test_etd_is_requested_only_outside_the_eu(self):
        eu = self._build("Germany")["requestedShipment"]
        self.assertNotIn("shipmentSpecialServices", eu)
        self.assertNotIn("shippingDocumentSpecification", eu)

        non_eu = self._build("Switzerland")["requestedShipment"]
        self.assertEqual(
            non_eu["shipmentSpecialServices"]["specialServiceTypes"],
            ["ELECTRONIC_TRADE_DOCUMENTS"],
        )

    def test_etd_always_declares_the_document_it_transmits(self):
        """ETD without a declared commercial invoice is rejected outright with
        SHIPPING.DOCUMENT.REQUIRED, so the two must never be separated."""
        rs = self._build("Switzerland")["requestedShipment"]
        self.assertIn("ELECTRONIC_TRADE_DOCUMENTS", rs["shipmentSpecialServices"]["specialServiceTypes"])
        self.assertIn(
            "COMMERCIAL_INVOICE", rs["shippingDocumentSpecification"]["shippingDocumentTypes"]
        )

    def test_package_and_commodity_weights_share_a_unit(self):
        """FedEx rejects a shipment whose commodity weight unit differs from the
        package weight unit."""
        contents = [{
            "item_code": "SCHUH-1", "item_name": "Holzschuh", "qty": 2.0, "uom": "Nos",
            "weight_per_unit": 0.9, "rate": 50.0, "currency": "EUR",
            "tariff": "6403", "origin_country": "Austria",
        }]
        rs = self._build("Switzerland", contents=contents)["requestedShipment"]
        package_unit = rs["requestedPackageLineItems"][0]["weight"]["units"]
        commodity_unit = rs["customsClearanceDetail"]["commodities"][0]["weight"]["units"]
        self.assertEqual(package_unit, commodity_unit)

    def test_customs_value_is_quantity_times_rate(self):
        contents = [{
            "item_code": "SCHUH-1", "item_name": "Holzschuh", "qty": 2.0, "uom": "Nos",
            "weight_per_unit": 0.9, "rate": 50.0, "currency": "EUR",
            "tariff": "6403", "origin_country": "Austria",
        }]
        rs = self._build("Switzerland", contents=contents)["requestedShipment"]
        commodity = rs["customsClearanceDetail"]["commodities"][0]
        self.assertEqual(commodity["unitPrice"]["amount"], 50.0)
        self.assertEqual(commodity["customsValue"]["amount"], 100.0)
        self.assertEqual(rs["customsClearanceDetail"]["totalCustomsValue"]["amount"], 100.0)

    def test_falls_back_to_a_single_commodity_without_contents(self):
        """customsClearanceDetail requires at least one commodity, so a Shipment
        with no scanned rows and no Delivery Note still declares something."""
        rs = self._build("Switzerland", contents=[])["requestedShipment"]
        commodities = rs["customsClearanceDetail"]["commodities"]
        self.assertEqual(len(commodities), 1)
        self.assertEqual(commodities[0]["description"], "Holzschuhe")

    def test_dimensions_are_integers(self):
        rs = self._build("Germany")["requestedShipment"]
        dims = rs["requestedPackageLineItems"][0]["dimensions"]
        for axis in ("length", "width", "height"):
            self.assertIsInstance(dims[axis], int)

    def test_duties_payment_comes_from_the_carrier_service(self):
        ddp = SimpleNamespace(get=lambda k: {
            "service_code": "FEDEX_INTERNATIONAL_PRIORITY",
            "packaging_type": "YOUR_PACKAGING",
            "duties_payment": "Sender (DDP)",
        }.get(k))
        rs = self._build("Switzerland", service=ddp)["requestedShipment"]
        self.assertEqual(rs["customsClearanceDetail"]["dutiesPayment"]["paymentType"], "SENDER")

    def test_duties_payment_defaults_to_recipient(self):
        rs = self._build("Switzerland")["requestedShipment"]
        self.assertEqual(rs["customsClearanceDetail"]["dutiesPayment"]["paymentType"], "RECIPIENT")


class TestContactValidation(FrappeTestCase):
    def test_missing_phone_names_the_address_to_fix(self):
        """FedEx answers PHONENUMBER.EMPTY, which says nothing about WHICH party
        or record is at fault — so we refuse earlier, with the address name."""
        address = _address(name="ADDR-NO-PHONE", phone=None)
        with patch.object(fedex_api, "_phone_from_contact", return_value=""):
            with self.assertRaises(frappe.ValidationError) as ctx:
                fedex_api._fedex_contact(
                    person_name="Max Mustermann",
                    company_name="",
                    address=address,
                    contact_name=None,
                    email=None,
                    role="recipient",
                )
        self.assertIn("ADDR-NO-PHONE", str(ctx.exception))

    def test_phone_falls_back_to_the_linked_contact(self):
        address = _address(name="ADDR-NO-PHONE", phone=None)
        with patch.object(fedex_api, "_phone_from_contact", return_value="+4366412345"):
            contact = fedex_api._fedex_contact(
                person_name="Max Mustermann",
                company_name="",
                address=address,
                contact_name="CONTACT-1",
                email=None,
                role="recipient",
            )
        self.assertEqual(contact["phoneNumber"], "+4366412345")


class TestLabelSpecification(FrappeTestCase):
    """Label geometry and the auxiliary Air Waybill label.

    Both are invisible in a successful FedEx response: a label generated for the
    wrong dpi still returns HTTP 200 and only misprints on paper, and a missing
    auxiliary label only surfaces three business days later as a rejected
    approval submission.
    """

    def _spec(self, destination_country="Germany", resolution=203):
        addresses = {
            "PICKUP": _address(name="PICKUP"),
            "DELIVERY": _address(
                name="DELIVERY", city="Muenchen", pincode="80331",
                country=destination_country, phone="+4989123456",
            ),
        }
        service = SimpleNamespace(get=lambda k: {
            "service_code": "FEDEX_INTERNATIONAL_PRIORITY",
            "packaging_type": "YOUR_PACKAGING",
        }.get(k))
        with patch.object(fedex_api, "_address_doc", side_effect=lambda n: addresses.get(n)), \
             patch.object(fedex_api, "_carrier_service_doc", return_value=service), \
             patch.object(fedex_api, "collect_parcel_contents", return_value=[]), \
             patch.object(fedex_api, "_resolve_label_weight", return_value=1.84), \
             patch.object(fedex_api, "_parcel_dimensions", return_value={}), \
             patch.object(fedex_api, "_country_code", side_effect=lambda c: {"Austria": "AT", "Germany": "DE"}.get(c, "AT")):
            payload = fedex_api._build_payload(
                _shipment_doc(), _credentials(label_resolution=resolution)
            )
        return payload["requestedShipment"]["labelSpecification"]

    def test_international_shipments_request_the_auxiliary_awb_label(self):
        spec = self._spec("Germany")
        labels = spec["customerSpecifiedDetail"]["additionalLabels"]
        self.assertEqual([entry["type"] for entry in labels], ["CONSIGNEE"])

    def test_domestic_shipments_do_not_request_it(self):
        spec = self._spec("Austria")
        self.assertNotIn("customerSpecifiedDetail", spec)

    def test_the_auxiliary_label_can_be_switched_off(self):
        """It doubles label consumption on every international parcel, so the
        setting has to actually reach the payload."""
        addresses = {"PICKUP": _address(name="PICKUP"),
                     "DELIVERY": _address(name="DELIVERY", country="Germany", phone="+4989123456")}
        service = SimpleNamespace(get=lambda k: {"service_code": "X", "packaging_type": "Y"}.get(k))
        with patch.object(fedex_api, "_address_doc", side_effect=lambda n: addresses.get(n)), \
             patch.object(fedex_api, "_carrier_service_doc", return_value=service), \
             patch.object(fedex_api, "collect_parcel_contents", return_value=[]), \
             patch.object(fedex_api, "_resolve_label_weight", return_value=1.0), \
             patch.object(fedex_api, "_parcel_dimensions", return_value={}), \
             patch.object(fedex_api, "_country_code", side_effect=lambda c: {"Austria": "AT", "Germany": "DE"}.get(c, "AT")):
            payload = fedex_api._build_payload(
                _shipment_doc(), _credentials(request_auxiliary_label=False)
            )
        self.assertNotIn(
            "customerSpecifiedDetail", payload["requestedShipment"]["labelSpecification"]
        )

    def test_default_resolution_is_left_to_fedex(self):
        """203 dpi is FedEx's own default; sending it adds nothing."""
        self.assertNotIn("resolution", self._spec(resolution=203))

    def test_non_default_resolution_is_sent_explicitly(self):
        self.assertEqual(self._spec(resolution=300)["resolution"], 300)


class TestMultipleLabelDocuments(FrappeTestCase):
    def _response(self, labels, image_type="ZPLII"):
        import base64 as _b64
        return {
            "output": {
                "transactionShipments": [
                    {
                        "masterTrackingNumber": "794857982373",
                        "pieceResponses": [
                            {
                                "packageDocuments": [
                                    {"encodedLabel": _b64.b64encode(chunk).decode()}
                                    for chunk in labels
                                ]
                            }
                        ],
                    }
                ]
            }
        }

    def test_zpl_labels_for_several_packages_are_all_kept(self):
        """A multi-piece shipment returns one document per package; keeping only
        the first would silently lose the other parcels' labels."""
        tracking, label, _invoice = fedex_api._extract_label_and_tracking(
            self._response([b"^XAfirst^XZ", b"^XAsecond^XZ"]), "ZPLII"
        )
        self.assertEqual(tracking, "794857982373")
        self.assertEqual(label.count(b"^XA"), 2)

    def test_pdf_labels_are_not_concatenated(self):
        """PDFs cannot be joined byte-wise — keep the first rather than write a
        corrupt file."""
        with patch.object(fedex_api.frappe, "log_error"):
            _tracking, label, _invoice = fedex_api._extract_label_and_tracking(
                self._response([b"%PDF-1.4 first", b"%PDF-1.4 second"]), "PDF"
            )
        self.assertEqual(label, b"%PDF-1.4 first")

    def test_missing_label_data_is_an_error(self):
        with self.assertRaises(frappe.ValidationError):
            fedex_api._extract_label_and_tracking(self._response([]), "ZPLII")


class TestZplGeometry(FrappeTestCase):
    def test_blocks_are_measured_per_label(self):
        from .fedex_validation import _zpl_geometry

        zpl = "^XA^PW800^FO12,100^FS^FO400,1053^FS^XZ^XA^PW800^FO10,1165^FS^XZ"
        blocks = _zpl_geometry(zpl, 203)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["print_width_mm"], 100.1)
        self.assertEqual(blocks[0]["content_height_mm"], 131.8)
        self.assertEqual(blocks[1]["content_height_mm"], 145.8)

    def test_same_label_measures_larger_at_lower_dpi(self):
        """The dpi mismatch that makes a 300 dpi label overflow a 203 dpi
        printer is exactly this factor."""
        from .fedex_validation import _zpl_geometry

        zpl = "^XA^PW1200^FO10,1200^FS^XZ"
        at_300 = _zpl_geometry(zpl, 300)[0]["print_width_mm"]
        at_203 = _zpl_geometry(zpl, 203)[0]["print_width_mm"]
        self.assertLess(at_300, at_203)
        self.assertGreater(at_203, 105.0)  # would overflow the A6 stock


class TestCredentialsPerEnvironment(FrappeTestCase):
    """Sandbox and production credentials must not share fields.

    FedEx issues a separate key, secret and shipping account per environment.
    With one shared set, going live overwrites the sandbox configuration and
    development against the test environment stops being possible.
    """

    def _settings(self, **overrides):
        values = {
            "enable_fedex_integration": 1,
            "api_mode": "Sandbox",
            "sandbox_base_url": "https://apis-sandbox.fedex.com",
            "production_base_url": "https://apis.fedex.com",
            "sandbox_client_key": "sandbox-key",
            "sandbox_account_number": "740561073",
            "production_client_key": "production-key",
            "production_account_number": "123456789",
            "api_timeout": 30,
            "label_image_type": "ZPLII",
            "label_stock_type": "STOCK_4X6",
            "label_resolution": "203",
            "request_auxiliary_label": 1,
            "pickup_type": "USE_SCHEDULED_PICKUP",
            "default_service_type": "INTERNATIONAL_ECONOMY",
            "shipment_purpose": "SOLD",
            "terms_of_sale": "DAP",
        }
        values.update(overrides)
        secrets = {
            "sandbox_client_secret": "sandbox-secret",
            "production_client_secret": "production-secret",
        }
        doc = SimpleNamespace(**values)
        doc.get = lambda key, _v=values: _v.get(key)
        doc.get_password = lambda key, raise_exception=True, _s=secrets: _s.get(key)
        return doc

    def test_sandbox_mode_uses_the_sandbox_set(self):
        with patch.object(fedex_api.frappe, "get_single", return_value=self._settings()):
            creds = fedex_api._get_credentials()
        self.assertEqual(creds.base_url, "https://apis-sandbox.fedex.com")
        self.assertEqual(creds.client_key, "sandbox-key")
        self.assertEqual(creds.account_number, "740561073")

    def test_production_mode_uses_the_production_set(self):
        settings = self._settings(api_mode="Production")
        with patch.object(fedex_api.frappe, "get_single", return_value=settings):
            creds = fedex_api._get_credentials()
        self.assertEqual(creds.base_url, "https://apis.fedex.com")
        self.assertEqual(creds.client_key, "production-key")
        self.assertEqual(creds.account_number, "123456789")

    def test_tracking_credentials_use_their_own_key_pair(self):
        """The Track API lives in its own FedEx Developer Portal project —
        calling it with the Ship key answers 403, so the tracking client must
        read the tracking key pair, while account number and base URL stay
        shared with the Ship project."""
        settings = self._settings(sandbox_tracking_client_key="tracking-key")
        settings.get_password = lambda key, raise_exception=True: {
            "sandbox_client_secret": "sandbox-secret",
            "sandbox_tracking_client_secret": "tracking-secret",
        }.get(key)
        with patch.object(fedex_api.frappe, "get_single", return_value=settings):
            creds = fedex_api._get_credentials(for_tracking=True)
        self.assertEqual(creds.client_key, "tracking-key")
        self.assertEqual(creds.client_secret, "tracking-secret")
        self.assertEqual(creds.account_number, "740561073")  # shared with Ship
        self.assertEqual(creds.base_url, "https://apis-sandbox.fedex.com")

    def test_missing_tracking_credentials_are_refused_not_borrowed(self):
        """Falling back to the Ship key would just reproduce the 403 at
        FedEx — better to say which fields are missing."""
        with patch.object(fedex_api.frappe, "get_single", return_value=self._settings()):
            with self.assertRaises(fedex_api.FedExNotConfiguredError) as ctx:
                fedex_api._get_credentials(for_tracking=True)
        self.assertIn("Tracking", str(ctx.exception))

    def test_switching_mode_without_credentials_is_refused(self):
        """Going live with the sandbox set still in place must fail loudly
        rather than authenticate against the wrong environment."""
        settings = self._settings(
            api_mode="Production", production_client_key="", production_account_number=""
        )
        settings.get_password = lambda key, raise_exception=True: None
        with patch.object(fedex_api.frappe, "get_single", return_value=settings):
            with self.assertRaises(fedex_api.FedExNotConfiguredError) as ctx:
                fedex_api._get_credentials()
        self.assertIn("Production", str(ctx.exception))

    def test_token_cache_is_keyed_by_mode_and_key(self):
        """A cached sandbox token must never be replayed against production."""
        sandbox = fedex_api.FedExAPIClient(
            _credentials(mode="Sandbox", client_key="k1")
        )._token_cache_key
        production = fedex_api.FedExAPIClient(
            _credentials(mode="Production", client_key="k2")
        )._token_cache_key
        self.assertNotEqual(sandbox, production)


class TestDutiesPaymentResolution(FrappeTestCase):
    """The payload must bill duty to whoever actually pays it.

    A Carrier Service covers one destination country, so its ``duties_payment``
    is unambiguous and is used directly. The consistency guarantee lives at save
    time: a service with an Import Charge Item that is not "Sender (DDP)" is
    rejected, so a customer cannot be charged at checkout and billed again at
    the border.
    """

    def _resolve(self, service_setting):
        service = SimpleNamespace(get=lambda k, _s=service_setting: {"duties_payment": _s}.get(k))
        return fedex_api._resolve_duties_payment(service, "Switzerland")

    def test_ddp_service_bills_the_sender(self):
        self.assertEqual(self._resolve("Sender (DDP)"), "SENDER")

    def test_dap_service_bills_the_recipient(self):
        self.assertEqual(self._resolve("Recipient (DAP)"), "RECIPIENT")

    def test_unset_carrier_service_defaults_to_recipient(self):
        """Never advance duty for a customer we did not charge for it."""
        self.assertEqual(self._resolve(None), "RECIPIENT")


class TestAsciiFields(FrappeTestCase):
    """FedEx: "do not feed non ASCII characters in to your Create Shipment
    request". Non-ASCII input comes back as garbage on the ZPL label, so every
    text field is transliterated first."""

    def test_german_umlauts_use_the_ae_oe_ue_ss_convention(self):
        self.assertEqual(fedex_api._ascii_field("Müller Straße Öl Äpfel"), "Mueller Strasse Oel Aepfel")

    def test_all_caps_words_stay_all_caps(self):
        self.assertEqual(fedex_api._ascii_field("MÜLLER GMBH"), "MUELLER GMBH")
        self.assertEqual(fedex_api._ascii_field("Ü"), "UE")

    def test_other_diacritics_are_stripped(self):
        self.assertEqual(fedex_api._ascii_field("Café Señor Brønnøysund Łódź"), "Cafe Senor Broennoeysund Lodz")

    def test_characters_without_an_ascii_form_are_dropped(self):
        self.assertEqual(fedex_api._ascii_field("Holz ★ Schuh 日本"), "Holz Schuh")
        self.assertEqual(fedex_api._ascii_field("Preis 10 €"), "Preis 10 EUR")

    def test_ascii_input_passes_through_cleaned(self):
        self.assertEqual(fedex_api._ascii_field("\tMarienplatz  8 "), "Marienplatz 8")
        self.assertEqual(fedex_api._ascii_field(None), "")

    def test_the_whole_payload_is_ascii(self):
        """Umlauts in address, name and commodity all leave as ASCII, and the
        35-character cap is applied AFTER "ü" became "ue"."""
        import json as _json

        long_street = "Müllerstraße " + "x" * 22  # 35 chars with umlauts, 37 without
        addresses = {
            "PICKUP": _address(name="PICKUP", city="Wien"),
            "DELIVERY": _address(
                name="DELIVERY", address_line1=long_street, city="Zürich",
                pincode="8001", country="Switzerland", phone="+41442345678",
            ),
        }
        contents = [{
            "item_code": "KSL-1", "item_name": "Kässpätzleschöpfer", "qty": 1.0, "uom": "Nos",
            "weight_per_unit": 0.5, "rate": 16.0, "currency": "EUR",
            "tariff": "4419", "origin_country": "Austria",
        }]
        shipment = _shipment_doc()
        fields = shipment.as_dict()
        fields["delivery_customer"] = "Jörg Größmann"
        shipment.get = lambda key, _f=fields: _f.get(key)
        shipment.as_dict = lambda _f=fields: dict(_f)
        service = SimpleNamespace(get=lambda k: {"service_code": "FEDEX_INTERNATIONAL_PRIORITY"}.get(k))

        with patch.object(fedex_api, "_address_doc", side_effect=lambda n: addresses.get(n)), \
             patch.object(fedex_api, "_carrier_service_doc", return_value=service), \
             patch.object(fedex_api, "collect_parcel_contents", return_value=contents), \
             patch.object(fedex_api, "_resolve_label_weight", return_value=1.0), \
             patch.object(fedex_api, "_parcel_dimensions", return_value={}), \
             patch.object(fedex_api, "_country_code", side_effect=lambda c: {"Austria": "AT", "Switzerland": "CH"}.get(c, "AT")):
            payload = fedex_api._build_payload(shipment, _credentials())

        self.assertTrue(_json.dumps(payload, ensure_ascii=False).isascii())
        recipient = payload["requestedShipment"]["recipients"][0]
        self.assertEqual(recipient["address"]["city"], "Zuerich")
        self.assertEqual(recipient["address"]["streetLines"][0], ("Muellerstrasse " + "x" * 22)[:35])
        self.assertEqual(recipient["contact"]["personName"], "Joerg Groessmann")
        commodity = payload["requestedShipment"]["customsClearanceDetail"]["commodities"][0]
        self.assertEqual(commodity["description"], "Kaesspaetzleschoepfer")
