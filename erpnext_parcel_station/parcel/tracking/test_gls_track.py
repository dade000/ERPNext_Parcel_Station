# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the GLS tracking poll: response flattening, cap, error isolation.

Why this needs tests: the ShipIT tracking endpoint answers one TrackID per
request, so a single refused parcel aborting the loop would silently starve
every parcel behind it in the queue; and the response shape varies between
ShipIT installations, so the tolerant field extraction is load-bearing.
Fixture: ``tests/fixtures/gls_parceldetails_response_sample.json`` — the
history of a delivered parcel as production answered it on 2026-10-02.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.tracking import gls_track
from erpnext_parcel_station.parcel.tracking import status_mapping as sm

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "gls_parceldetails_response_sample.json"
CATALOGUE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "gls_tracktrace_mapping.json"


def _payload():
    return json.loads(FIXTURE.read_text())


class _Settings(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


class TestResponseFlattening(FrappeTestCase):
    def _events(self, shipment="SHIPMENT-00002"):
        collected = gls_track._events_from_details("ZR1LAB5T", shipment, _payload())
        return sorted(collected, key=lambda event: event["event_timestamp"])

    def test_history_becomes_tracking_events(self):
        collected = self._events()
        self.assertEqual(len(collected), 7)
        newest = collected[-1]
        self.assertEqual(newest["event_timestamp"], "2026-10-02T09:39:07")
        self.assertEqual(newest["shipment"], "SHIPMENT-00002")
        self.assertTrue(newest["event_id"].startswith("GLS::ZR1LAB5T::"))

    def test_statuses_of_a_real_history(self):
        # The life of a delivered parcel as production reported it. Before the
        # fix every entry but the last read "In Transit": the customer saw "on
        # its way" for a parcel GLS did not have yet, and never "out for delivery".
        self.assertEqual(
            [(event["event_code"], event["canonical_status"]) for event in self._events()],
            [
                ("DATA_RECEIVED", sm.STATUS_ANNOUNCED),
                ("HUB", sm.STATUS_IN_TRANSIT),
                ("HUB", sm.STATUS_IN_TRANSIT),
                ("HUB", sm.STATUS_IN_TRANSIT),
                ("HUB", sm.STATUS_IN_TRANSIT),
                ("IN_DELIVERY", sm.STATUS_OUT_FOR_DELIVERY),
                ("DELIVERED", sm.STATUS_DELIVERED),
            ],
        )

    def test_place_comes_from_location_without_depot_codes(self):
        places = [(event["location_city"], event["location_country"]) for event in self._events()]
        self.assertEqual(places[0], ("Rankweil", "AT"))
        self.assertEqual(places[3], ("RUP München", "DE"))  # "RUP München DE R80 DE 080"
        self.assertEqual(places[-1], ("Ansfelden", "AT"))

    def test_parcel_status_is_stamped_onto_newest_event_only_when_known(self):
        payload = _payload()
        payload["UnitDetail"]["History"] = payload["UnitDetail"]["History"][-1:]  # only DATA_RECEIVED
        payload["UnitDetail"]["Status"] = "SOMETHINGNEW"
        only = gls_track._events_from_details("ZR1LAB5T", None, payload)[0]
        # An unrecognised word used to overwrite the entry with "In Transit".
        self.assertEqual(only["canonical_status"], sm.STATUS_ANNOUNCED)

        payload["UnitDetail"]["Status"] = "DELIVERED"
        only = gls_track._events_from_details("ZR1LAB5T", None, payload)[0]
        self.assertEqual(only["canonical_status"], sm.STATUS_DELIVERED)
        self.assertEqual(only["carrier_status"], "DELIVERED")

    def test_event_ids_are_deterministic(self):
        first = gls_track._events_from_details("ZR1LAB5T", None, _payload())
        second = gls_track._events_from_details("ZR1LAB5T", None, _payload())
        self.assertEqual([e["event_id"] for e in first], [e["event_id"] for e in second])

    def test_unitdetail_wrapper_is_unwrapped(self):
        # The live API answers {"UnitDetail": {...}}. Reading the top level
        # instead returns no events while the call reports HTTP 200 — a silent
        # no-op that looked like "nothing new" for a whole production run.
        self.assertTrue(set(_payload()) >= {"UnitDetail"})
        self.assertEqual(len(gls_track._events_from_details("ZR1LAB5T", None, _payload())), 7)

    def test_empty_or_shapeless_payload_yields_no_events(self):
        self.assertEqual(gls_track._events_from_details("X", None, {}), [])
        self.assertEqual(gls_track._events_from_details("X", None, None), [])
        self.assertEqual(gls_track._events_from_details("X", None, {"UnitDetail": {"History": ["x"]}}), [])


class TestParcelShopHistory(FrappeTestCase):
    """A home delivery that failed and ended at a ParcelShop, as production
    reported it on 2026-09-29. ShipIT has no code for either: both cases are
    only told apart by the description."""

    HISTORY = [
        ("2026-09-29T14:58:47+02:00", "DELIVERED", "The parcel has been delivered at the ParcelShop (see ParcelShop information)."),
        ("2026-09-29T14:57:04+02:00", "DELIVERY_DEPOT", "The parcel has reached the ParcelShop."),
        ("2026-09-29T14:55:37+02:00", "DELIVERY_DEPOT", "The parcel could not be delivered as the reception was closed."),
        ("2026-09-29T08:15:05+02:00", "IN_DELIVERY", "The parcel is expected to be delivered during the day."),
        ("2026-09-29T08:09:52+02:00", "DELIVERY_DEPOT", "The parcel has reached the parcel center."),
        ("2026-09-29T01:56:26+02:00", "HUB", "The parcel has reached the parcel center."),
    ]

    def _statuses(self):
        payload = {"UnitDetail": {"History": [
            {"Country": "AT", "Date": date, "StatusCode": code, "Description": text,
             "Location": "Zirl AT 510", "LocationCode": "AT 510"}
            for date, code, text in self.HISTORY
        ]}}
        collected = gls_track._events_from_details("ZE0Q079C", "SHIPMENT-00314", payload)
        collected.sort(key=lambda event: event["event_timestamp"])
        return [event["canonical_status"] for event in collected]

    def test_deposit_at_the_parcelshop_is_waiting_not_delivered(self):
        self.assertEqual(
            self._statuses(),
            [
                sm.STATUS_IN_TRANSIT,  # HUB
                sm.STATUS_IN_TRANSIT,  # DELIVERY_DEPOT, reached the parcel center
                sm.STATUS_OUT_FOR_DELIVERY,
                sm.STATUS_PROBLEM,  # could not be delivered
                sm.STATUS_IN_TRANSIT,  # reached the ParcelShop
                sm.STATUS_READY_FOR_PICKUP,  # delivered AT the ParcelShop
            ],
        )

    def test_plain_delivery_stays_delivered(self):
        self.assertEqual(sm.map_gls_event("DELIVERED", "The parcel has been delivered."), sm.STATUS_DELIVERED)
        # A later hand-over that only mentions the shop is not "waiting" again.
        self.assertEqual(
            sm.map_gls_event("DELIVERED", "The parcel has been picked up at the ParcelShop."), sm.STATUS_DELIVERED
        )

    def test_failure_wording_never_downgrades_a_delivered_code(self):
        self.assertEqual(sm.map_gls_event("DELIVERED", "could not be delivered earlier"), sm.STATUS_DELIVERED)


class TestGlsDescriptionCatalogue(FrappeTestCase):
    """GLS's own list of descriptions per status (about 190 texts,
    ``tests/fixtures/gls_tracktrace_mapping.json``). The rules in
    ``map_gls_event`` are fragments, so they are checked against every text
    GLS can send rather than against a few hand-picked ones."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rows = [tuple(row) for row in json.loads(CATALOGUE.read_text())]

    def _mapped(self, code=None):
        return [
            (row_code, text, sm.map_gls_event(row_code, text))
            for row_code, text in self.rows
            if code is None or row_code == code
        ]

    def test_catalogue_is_the_one_we_expect(self):
        self.assertGreater(len(self.rows), 180)
        self.assertEqual({code for code, _ in self.rows}, {"CANCELED", "DATA_RECEIVED", "DELIVERED", "HUB", "IN_DELIVERY"})

    def test_delivered_code_is_delivered_or_waiting_never_less(self):
        for _, text, status in self._mapped("DELIVERED"):
            self.assertIn(status, (sm.STATUS_DELIVERED, sm.STATUS_READY_FOR_PICKUP), text)
        waiting = {text for _, text, status in self._mapped("DELIVERED") if status == sm.STATUS_READY_FOR_PICKUP}
        self.assertEqual(waiting, {"Parcel available at ParcelShop.", "The parcel has been delivered at a post office."})

    def test_collection_from_shop_or_locker_is_delivered(self):
        # These mention the shop/locker too — and must end the waiting.
        for text in (
            "Handing over the parcel to the recipient at the GLS ParcelShop.",
            "The parcel has been collected by the recipient from the ParcelLocker.",
            "Information from Parcel Locker - collection confirmed",
        ):
            self.assertEqual(sm.map_gls_event("DELIVERED", text), sm.STATUS_DELIVERED, text)

    def test_deposit_in_a_locker_is_waiting_although_the_code_says_in_delivery(self):
        self.assertEqual(
            sm.map_gls_event("IN_DELIVERY", "The parcel has been delivered into the ParcelLocker."),
            sm.STATUS_READY_FOR_PICKUP,
        )
        # … while the failed attempt to do so is a problem, not a waiting parcel.
        self.assertEqual(
            sm.map_gls_event(
                "HUB",
                "The parcel could not be delivered into the ParcelLocker because there were no fitting compartments available.",
            ),
            sm.STATUS_PROBLEM,
        )

    def test_every_failed_delivery_text_is_a_problem(self):
        for code, text, status in self._mapped():
            lowered = text.lower()
            if code == "DELIVERED" or "new delivery date" in lowered or lowered.startswith("return shipment"):
                continue
            if "could not be delivered" in lowered or "cannot be delivered" in lowered:
                self.assertEqual(status, sm.STATUS_PROBLEM, text)

    def test_rescheduling_by_the_recipient_is_not_a_problem(self):
        self.assertEqual(
            sm.map_gls_event("HUB", "The parcel could not be delivered as a new delivery date has been agreed."),
            sm.STATUS_IN_TRANSIT,
        )

    def test_returns_and_cancellations(self):
        self.assertEqual(sm.map_gls_event("HUB", "Parcel in return process"), sm.STATUS_RETURNED)
        self.assertTrue(all(status == sm.STATUS_PROBLEM for _, _, status in self._mapped("CANCELED")))

    def test_pickup_order_texts_do_not_alarm(self):
        # "Could not be picked up" is about collecting a parcel from a sender
        # (a pickup order), not about our customer's delivery.
        for code, text, status in self._mapped():
            if code in ("DATA_RECEIVED", "HUB") and "could not be picked up" in text.lower():
                self.assertIn(status, (sm.STATUS_ANNOUNCED, sm.STATUS_IN_TRANSIT), text)

    def test_data_received_with_a_physical_scan_is_in_transit(self):
        self.assertEqual(
            sm.map_gls_event("DATA_RECEIVED", "The parcel was handed over by the shipper to GLS."), sm.STATUS_IN_TRANSIT
        )

    def test_summary_of_the_whole_catalogue(self):
        # A change of the rules shows up here as a shift between the classes.
        counts = {}
        for _, _, status in self._mapped():
            counts[status] = counts.get(status, 0) + 1
        self.assertEqual(
            counts,
            {
                sm.STATUS_ANNOUNCED: 12,
                sm.STATUS_IN_TRANSIT: 52,
                sm.STATUS_OUT_FOR_DELIVERY: 6,
                sm.STATUS_READY_FOR_PICKUP: 5,
                sm.STATUS_DELIVERED: 16,
                sm.STATUS_PROBLEM: 97,
                sm.STATUS_RETURNED: 2,
            },
        )


class TestStatusCodes(FrappeTestCase):
    def test_codes_are_read_with_and_without_underscores(self):
        for code, expected in (
            ("DATA_RECEIVED", sm.STATUS_ANNOUNCED),
            ("IN_DELIVERY", sm.STATUS_OUT_FOR_DELIVERY),
            ("INDELIVERY", sm.STATUS_OUT_FOR_DELIVERY),
            ("hub", sm.STATUS_IN_TRANSIT),
            ("PICKUP", sm.STATUS_IN_TRANSIT),
            ("DELIVERY_DEPOT", sm.STATUS_IN_TRANSIT),
            ("CANCELLED", sm.STATUS_PROBLEM),
            ("DELIVERED", sm.STATUS_DELIVERED),
            ("NOT_DELIVERED", sm.STATUS_PROBLEM),
            ("DELIVERED_PS", sm.STATUS_READY_FOR_PICKUP),
        ):
            self.assertEqual(sm.map_gls_status(code), expected, code)

    def test_unknown_code_is_in_transit_but_not_known(self):
        self.assertEqual(sm.map_gls_status("SOMETHINGNEW"), sm.STATUS_IN_TRANSIT)
        self.assertIsNone(sm.known_gls_status("SOMETHINGNEW"))
        self.assertIsNone(sm.known_gls_status(None))


class TestRemapStoredEvents(FrappeTestCase):
    def test_stored_events_get_status_and_place_from_their_payload(self):
        raw = '{"Country": "AT", "Location": "Rankweil AT 520", "LocationCode": "AT 520", "StatusCode": "DATA_RECEIVED"}'
        rows = [
            # as stored on production before the fix
            SimpleNamespace(name="E1", shipment="SHIPMENT-1", event_code="DATA_RECEIVED", carrier_status="DATA_RECEIVED",
                            description="", canonical_status=sm.STATUS_IN_TRANSIT, location_city="", location_country="", raw_payload=raw),
            SimpleNamespace(name="E2", shipment="SHIPMENT-1", event_code="IN_DELIVERY", carrier_status="IN_DELIVERY",
                            description="", canonical_status=sm.STATUS_IN_TRANSIT, location_city="Ansfelden", location_country="AT", raw_payload="{}"),
            # already right: untouched
            SimpleNamespace(name="E3", shipment="SHIPMENT-2", event_code="DELIVERED", carrier_status="DELIVERED",
                            description="", canonical_status=sm.STATUS_DELIVERED, location_city="Ansfelden", location_country="AT", raw_payload="not json"),
        ]
        with (
            mock.patch.object(gls_track.frappe, "get_all", return_value=rows),
            mock.patch.object(gls_track.frappe.db, "set_value") as set_value,
            mock.patch.object(gls_track.events, "update_shipment_statuses") as update,
        ):
            summary = gls_track.remap_stored_events()
        written = {call[0][1]: call[0][2] for call in set_value.call_args_list}
        self.assertEqual(
            written,
            {
                "E1": {"canonical_status": sm.STATUS_ANNOUNCED, "location_city": "Rankweil", "location_country": "AT"},
                "E2": {"canonical_status": sm.STATUS_OUT_FOR_DELIVERY},
            },
        )
        # reopen: a wrongly "Delivered" Shipment must be able to go back.
        update.assert_called_once_with({"SHIPMENT-1"}, reopen=True)
        self.assertEqual(summary, {"events": 3, "changed": 2, "shipments": 1})

    def test_parcelshop_deposit_stored_as_delivered_is_reopened(self):
        rows = [
            SimpleNamespace(name="E9", shipment="SHIPMENT-00314", event_code="DELIVERED", carrier_status="DELIVERED",
                            description="The parcel has been delivered at the ParcelShop (see ParcelShop information).",
                            canonical_status=sm.STATUS_DELIVERED, location_city="Zirl", location_country="AT", raw_payload="{}"),
        ]
        with (
            mock.patch.object(gls_track.frappe, "get_all", return_value=rows),
            mock.patch.object(gls_track.frappe.db, "set_value") as set_value,
            mock.patch.object(gls_track.events, "update_shipment_statuses") as update,
        ):
            gls_track.remap_stored_events()
        self.assertEqual(set_value.call_args[0][2], {"canonical_status": sm.STATUS_READY_FOR_PICKUP})
        update.assert_called_once_with({"SHIPMENT-00314"}, reopen=True)


class TestPolling(FrappeTestCase):
    def _rows(self, count):
        return [SimpleNamespace(name=f"S-{i}", awb_number=f"TRK{i}") for i in range(count)]

    def _run(self, rows, client, max_per_run=200):
        settings = _Settings(enable_gls_tracking=1, gls_max_parcels_per_run=max_per_run)
        with (
            mock.patch.object(gls_track.frappe, "get_single", return_value=settings),
            mock.patch.object(gls_track.GLSAPIClient, "from_settings", return_value=client),
            mock.patch.object(gls_track.events, "open_shipments", return_value=rows),
            mock.patch.object(gls_track.events, "update_shipment_statuses"),
            mock.patch.object(gls_track.frappe.db, "commit"),
            mock.patch.object(gls_track.frappe.db, "rollback"),
            mock.patch.object(gls_track.frappe, "log_error"),
        ):
            return gls_track.poll_gls_tracking()

    def test_volume_guard_caps_the_run(self):
        client = mock.MagicMock()
        client.get_parcel_details.return_value = {}
        summary = self._run(self._rows(10), client, max_per_run=4)
        self.assertEqual(client.get_parcel_details.call_count, 4)
        self.assertEqual(summary["shipments"], 4)

    def test_one_failing_parcel_does_not_abort_the_rest(self):
        client = mock.MagicMock()
        client.get_parcel_details.side_effect = [Exception("boom"), {}, {}]
        summary = self._run(self._rows(3), client)
        self.assertEqual(client.get_parcel_details.call_count, 3)
        self.assertEqual(summary["failed"], 1)

    def test_disabled_poll_returns_none_without_client(self):
        settings = _Settings(enable_gls_tracking=0)
        with (
            mock.patch.object(gls_track.frappe, "get_single", return_value=settings),
            mock.patch.object(gls_track.GLSAPIClient, "from_settings") as from_settings,
        ):
            self.assertIsNone(gls_track.poll_gls_tracking())
        from_settings.assert_not_called()
