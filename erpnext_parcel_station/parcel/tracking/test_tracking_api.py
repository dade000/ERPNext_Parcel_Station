# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the webshop tracking endpoint's response contract.

Why this needs tests: the response is a published contract
(``docs/tracking_api.md``) consumed by the shop's server routes — a renamed
or added field ships PII or breaks the customer overview without any error
on the ERP side. The critical invariant is the PII exclusion: carrier remark
fields and consignee names must never appear in the payload.
"""

from types import SimpleNamespace
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.tracking import api


class TestShipmentPayload(FrappeTestCase):
    ROW = SimpleNamespace(
        name="SHIPMENT-00001",
        docstatus=1,
        awb_number="1019012500386270268443",
        tracking_status="Delivered",
        carrier="Österreichische Post",
        carrier_service=None,
        custom_carrier_service=None,
        delivery_type=None,
    )
    EVENTS = [
        SimpleNamespace(
            event_timestamp="2025-11-26 11:04:26",
            canonical_status="Delivered",
            event_code="ZUS",
            location_city="Altach",
            location_country="AT",
        ),
        SimpleNamespace(
            event_timestamp="2025-11-26 08:50:32",
            canonical_status="Out for Delivery",
            event_code="AZT",
            location_city="",
            location_country="AT",
        ),
    ]

    def _payload(self):
        with mock.patch.object(api.frappe, "get_all", return_value=self.EVENTS):
            return api._shipment_payload(self.ROW)

    def test_contract_shape(self):
        payload = self._payload()
        self.assertEqual(payload["shipment"], "SHIPMENT-00001")
        self.assertEqual(payload["carrier"], "AUSTRIAN_POST")  # identity, not free text
        self.assertEqual(payload["tracking_status"], "Delivered")
        self.assertEqual(payload["last_event_at"], "2025-11-26 11:04:26")
        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(
            payload["events"][0],
            {
                "timestamp": "2025-11-26 11:04:26",
                "status": "Delivered",
                "code": "ZUS",
                "city": "Altach",
                "country": "AT",
            },
        )

    def test_no_pii_and_no_carrier_links_leak(self):
        # Deliberate exclusions per docs/tracking_api.md: descriptions carry
        # neighbour/recipient names, and only the customer's tracking page
        # (get_tracking_page) may link out to the carrier.
        payload = self._payload()
        self.assertNotIn("tracking_url", payload)
        self.assertNotIn("carrier_tracking_url", payload)
        for event in payload["events"]:
            self.assertEqual(
                set(event), {"timestamp", "status", "code", "city", "country"}
            )

    def test_empty_locations_become_none(self):
        payload = self._payload()
        self.assertIsNone(payload["events"][1]["city"])


class TestGetOrderTracking(FrappeTestCase):
    def test_unknown_sales_order_throws(self):
        with mock.patch.object(api.frappe.db, "exists", return_value=False):
            with self.assertRaises(Exception):
                api.get_order_tracking("SO-DOES-NOT-EXIST")

    def test_order_without_delivery_notes_yields_empty_shipments(self):
        with (
            mock.patch.object(api.frappe.db, "exists", return_value=True),
            mock.patch.object(api.frappe, "get_all", return_value=[]),
        ):
            result = api.get_order_tracking("SO-0001")
        self.assertEqual(result, {"sales_order": "SO-0001", "shipments": []})


class TestGetTrackingPage(FrappeTestCase):
    ORDER = SimpleNamespace(name="SO-0001", transaction_date="2026-09-04")
    ROW = SimpleNamespace(
        name="SHIPMENT-00001",
        docstatus=1,
        creation="2026-09-05 14:12:03",
        awb_number="1019012500386270268443",
        tracking_status="In Transit",
        carrier="Österreichische Post",
        carrier_service=None,
        custom_carrier_service=None,
        delivery_type="Home Delivery",
    )

    def _page(self, token, order=ORDER):
        meta = SimpleNamespace(has_field=lambda field: True)
        with (
            mock.patch.object(api.frappe, "get_meta", return_value=meta),
            mock.patch.object(api.frappe.db, "get_value", return_value=order) as get_value,
            mock.patch.object(api, "_order_shipments", return_value=[self.ROW]),
            mock.patch.object(api.frappe, "get_all", return_value=[]),
        ):
            return api.get_tracking_page(token), get_value

    def test_token_opens_the_order_with_carrier_link(self):
        page, get_value = self._page("T" * 32)
        self.assertEqual(get_value.call_args[0][1], {api.TOKEN_FIELD: "T" * 32})
        self.assertEqual(page["sales_order"], "SO-0001")
        self.assertEqual(page["order_date"], "2026-09-04")
        shipment = page["shipments"][0]
        self.assertEqual(shipment["carrier_name"], "Österreichische Post")
        self.assertEqual(
            shipment["carrier_tracking_url"],
            "https://www.post.at/sv/sendungsdetails?snr=1019012500386270268443",
        )
        self.assertEqual(shipment["shipped_at"], "2026-09-05 14:12:03")
        self.assertEqual(shipment["events"], [])

    def test_short_or_empty_token_never_reaches_the_database(self):
        # An empty token must not become "the first order whose token is empty".
        for token in ("", "   ", "abc", None):
            with (
                mock.patch.object(api.frappe.db, "get_value") as get_value,
                self.assertRaises(Exception),
            ):
                api.get_tracking_page(token)
            get_value.assert_not_called()

    def test_unknown_token_throws(self):
        with self.assertRaises(Exception):
            self._page("U" * 32, order=None)
