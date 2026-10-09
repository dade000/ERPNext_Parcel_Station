"""Unit tests for the FedEx courier pickup (parcel/infra/fedex_pickup.py).

Everything that talks to the database or to FedEx is stubbed; these tests
pin the window arithmetic, the request/cancel bodies and the scheduler guard.

Run on the dev site with --skip-before-tests (see memory: run-tests wipes
Item Prices):
    bench --site unified.local run-tests --skip-before-tests \
        --module erpnext_parcel_station.parcel.infra.test_fedex_pickup
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from . import fedex_api, fedex_pickup
from .fedex_pickup import PickupSettings, PickupWindow, pickup_window


def _settings(**overrides) -> PickupSettings:
    base = dict(
        enabled=True,
        ready_time=time(12, 30),
        close_time=time(16, 0),
        carrier_code="FDXE",
        package_location="FRONT",
        remarks="Haupteingang Verkaufsraum",
        notification_email="",
    )
    base.update(overrides)
    return PickupSettings(**base)


def _address(**fields):
    base = {
        "name": "PICKUP",
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


def _shipment(name: str, weight: float, **fields):
    base = {
        "name": name,
        "doctype": "Shipment",
        "pickup": None,
        "pickup_company": "Musterfirma GmbH",
        "pickup_contact_person": None,
        "pickup_contact_email": "info@example.com",
        "pickup_address_name": "PICKUP",
        "total_weight": weight,
    }
    base.update(fields)
    doc = SimpleNamespace(**base)
    doc.get = lambda key, _b=base: _b.get(key)
    doc.as_dict = lambda _b=base: dict(_b)
    return doc


class TestPickupWindow(FrappeTestCase):
    """Wednesday 2026-09-23 is a plain business day."""

    WED = date(2026, 9, 23)

    def test_noon_run_books_the_configured_ready_time_today(self):
        w = pickup_window(_settings(), datetime.combine(self.WED, time(12, 0)))
        self.assertEqual(w, PickupWindow(self.WED, time(12, 30), time(16, 0), "SAME_DAY"))
        self.assertEqual(w.ready_timestamp, "2026-09-23T12:30:00Z")

    def test_manual_request_in_the_afternoon_moves_the_ready_time_forward(self):
        w = pickup_window(_settings(), datetime.combine(self.WED, time(14, 58)))
        self.assertEqual(w.pickup_date, self.WED)
        self.assertEqual(w.ready_time, time(15, 15))
        self.assertEqual(w.date_type, "SAME_DAY")

    def test_too_late_for_today_rolls_over_to_tomorrow(self):
        w = pickup_window(_settings(), datetime.combine(self.WED, time(15, 30)))
        self.assertEqual(w.pickup_date, date(2026, 9, 24))
        self.assertEqual(w.ready_time, time(12, 30))
        self.assertEqual(w.date_type, "FUTURE_DAY")

    def test_friday_evening_books_monday(self):
        fri = date(2026, 9, 25)
        w = pickup_window(_settings(), datetime.combine(fri, time(17, 0)))
        self.assertEqual(w.pickup_date, date(2026, 9, 28))
        self.assertEqual(w.date_type, "FUTURE_DAY")

    def test_weekend_books_monday_even_in_the_morning(self):
        sat = date(2026, 9, 26)
        w = pickup_window(_settings(), datetime.combine(sat, time(9, 0)))
        self.assertEqual(w.pickup_date, date(2026, 9, 28))

    def test_last_possible_same_day_slot(self):
        # 15:15 ready + 45 min window == 16:00 close: still today.
        w = pickup_window(_settings(), datetime.combine(self.WED, time(15, 0)))
        self.assertEqual((w.pickup_date, w.ready_time, w.date_type), (self.WED, time(15, 15), "SAME_DAY"))


class TestSettingsParsing(FrappeTestCase):
    def test_time_field_variants(self):
        default = time(12, 30)
        self.assertEqual(fedex_pickup._to_time(None, default), default)
        self.assertEqual(fedex_pickup._to_time(timedelta(hours=13, minutes=5), default), time(13, 5))
        self.assertEqual(fedex_pickup._to_time("16:00:00", default), time(16, 0))
        self.assertEqual(fedex_pickup._to_time("16:00", default), time(16, 0))
        self.assertEqual(fedex_pickup._to_time("garbage", default), default)

    def test_parse_names(self):
        self.assertIsNone(fedex_pickup._parse_names(None))
        self.assertIsNone(fedex_pickup._parse_names(""))
        self.assertEqual(fedex_pickup._parse_names('["A", "B"]'), ["A", "B"])
        self.assertEqual(fedex_pickup._parse_names("SHIPMENT-1"), ["SHIPMENT-1"])
        self.assertEqual(fedex_pickup._parse_names(["X"]), ["X"])


class TestPickupPayload(FrappeTestCase):
    def _build(self, shipments, destinations, settings=None, address=None):
        window = PickupWindow(date(2026, 9, 23), time(12, 30), time(16, 0), "SAME_DAY")
        codes = {"Austria": "AT", "Germany": "DE", "Switzerland": "CH"}
        with patch.object(fedex_pickup, "_resolve_label_weight", side_effect=lambda sh: sh["total_weight"]), \
             patch.object(fedex_pickup, "_country_code", side_effect=lambda c: codes.get(c, "AT")), \
             patch.object(fedex_api, "_country_code", side_effect=lambda c: codes.get(c, "AT")):
            return fedex_pickup.build_pickup_payload(
                shipments, address or _address(), window, settings or _settings(), "740561073",
                destination_countries=destinations,
            )

    def test_body_matches_the_pickup_api(self):
        payload = self._build([_shipment("S1", 1.2), _shipment("S2", 0.8)], ["DE", "CH"])
        self.assertEqual(payload["associatedAccountNumber"], {"value": "740561073"})
        self.assertEqual(payload["carrierCode"], "FDXE")
        self.assertEqual(payload["packageCount"], 2)
        self.assertEqual(payload["totalWeight"], {"units": "KG", "value": 2.0})
        self.assertEqual(payload["countryRelationships"], "INTERNATIONAL")
        self.assertEqual(payload["remarks"], "Haupteingang Verkaufsraum")
        origin = payload["originDetail"]
        self.assertEqual(origin["readyDateTimestamp"], "2026-09-23T12:30:00Z")
        self.assertEqual(origin["customerCloseTime"], "16:00:00")
        self.assertEqual(origin["pickupDateType"], "SAME_DAY")
        self.assertEqual(origin["packageLocation"], "FRONT")
        self.assertEqual(origin["pickupLocation"]["address"]["postalCode"], "1010")
        self.assertEqual(origin["pickupLocation"]["address"]["countryCode"], "AT")
        contact = origin["pickupLocation"]["contact"]
        self.assertEqual(contact["companyName"], "Musterfirma GmbH")
        self.assertEqual(contact["phoneNumber"], "+4312345678")
        self.assertNotIn("emailAddress", contact)

    def test_domestic_when_every_parcel_stays_in_the_origin_country(self):
        payload = self._build([_shipment("S1", 1.0)], ["AT"])
        self.assertEqual(payload["countryRelationships"], "DOMESTIC")

    def test_whole_body_is_ascii(self):
        settings = _settings(remarks=fedex_api._ascii_field("Hintereingang, bitte läuten"))
        address = _address(address_line1="Müllerstraße 1", city="Zürich", country="Switzerland")
        payload = self._build([_shipment("S1", 1.0, pickup_company="Gebrüder Größ")], ["CH"], settings, address)
        self.assertTrue(json.dumps(payload, ensure_ascii=False).isascii())
        self.assertEqual(payload["remarks"], "Hintereingang, bitte laeuten")
        self.assertEqual(payload["originDetail"]["pickupLocation"]["contact"]["companyName"], "Gebrueder Groess")

    def test_missing_phone_is_refused_before_calling_fedex(self):
        with self.assertRaises(fedex_api.FedExAPIError):
            self._build([_shipment("S1", 1.0)], ["DE"], address=_address(phone=None))


class TestCancelPayload(FrappeTestCase):
    def test_body(self):
        pickup = SimpleNamespace(
            confirmation_code="ABC123", carrier_code="FDXE", pickup_date=date(2026, 9, 23), location_code="VIEA"
        )
        with patch.object(fedex_api, "_country_code", return_value="AT"):
            payload = fedex_pickup.build_cancel_payload(pickup, "740561073", _address(), "Kunde holt selbst ab")
        self.assertEqual(payload["pickupConfirmationCode"], "ABC123")
        self.assertEqual(payload["carrierCode"], "FDXE")
        self.assertEqual(payload["scheduledDate"], "2026-09-23")
        self.assertEqual(payload["location"], "VIEA")
        self.assertEqual(payload["remarks"], "Kunde holt selbst ab")
        self.assertEqual(payload["accountAddressOfRecord"]["postalCode"], "1010")

    def test_location_is_omitted_when_fedex_returned_none(self):
        pickup = SimpleNamespace(confirmation_code="1", carrier_code="", pickup_date="2026-09-23", location_code="")
        payload = fedex_pickup.build_cancel_payload(pickup, "1", None)
        self.assertNotIn("location", payload)
        self.assertNotIn("accountAddressOfRecord", payload)
        self.assertEqual(payload["carrierCode"], "FDXE")


class TestScanGuard(FrappeTestCase):
    """A parcel FedEx already scanned is never booked into another pickup."""

    def test_no_event_and_label_created_still_wait(self):
        for status in (None, "", "In Progress", "Announced"):
            self.assertTrue(fedex_pickup.not_yet_scanned({"tracking_status": status}), status)

    def test_any_physical_scan_excludes(self):
        for status in ("In Transit", "Out for Delivery", "Ready for Pickup", "Delivered", "Problem", "Returned", "Lost"):
            self.assertFalse(fedex_pickup.not_yet_scanned({"tracking_status": status}), status)


class TestGrouping(FrappeTestCase):
    def test_one_pickup_per_pickup_address(self):
        rows = [
            {"name": "A", "pickup_address_name": "Shop"},
            {"name": "B", "pickup_address_name": "Warehouse"},
            {"name": "C", "pickup_address_name": "Shop"},
        ]
        groups = fedex_pickup._group_by_pickup_address(rows)
        self.assertEqual(list(groups), ["Shop", "Warehouse"])
        self.assertEqual([r["name"] for r in groups["Shop"]], ["A", "C"])

    def test_country_relationship(self):
        self.assertEqual(fedex_pickup.country_relationship("AT", ["AT", "AT"]), "DOMESTIC")
        self.assertEqual(fedex_pickup.country_relationship("AT", ["AT", "DE"]), "INTERNATIONAL")
        self.assertEqual(fedex_pickup.country_relationship("AT", [""]), "DOMESTIC")


class TestScheduler(FrappeTestCase):
    def test_disabled_switch_makes_no_call(self):
        with patch.object(fedex_pickup, "get_pickup_settings", return_value=_settings(enabled=False)), \
             patch.object(fedex_pickup, "request_pickup") as request:
            self.assertIsNone(fedex_pickup.request_pending_pickups())
        request.assert_not_called()

    def test_enabled_switch_runs_as_auto(self):
        with patch.object(fedex_pickup, "get_pickup_settings", return_value=_settings(enabled=True)), \
             patch.object(fedex_pickup, "request_pickup", return_value={"pickups": []}) as request:
            self.assertEqual(fedex_pickup.request_pending_pickups(), {"pickups": []})
        request.assert_called_once_with(trigger="auto")

    def test_unconfigured_integration_is_skipped_quietly(self):
        with patch.object(fedex_pickup, "get_pickup_settings", return_value=_settings(enabled=True)), \
             patch.object(fedex_pickup, "request_pickup", side_effect=fedex_api.FedExNotConfiguredError("off")):
            self.assertIsNone(fedex_pickup.request_pending_pickups())
