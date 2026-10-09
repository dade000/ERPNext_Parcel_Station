# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards event storage, shipment matching and the canonical-status writer.

Why this needs tests: every failure mode here is silent in production. A
broken dedup fills the table with copies; a broken matcher stores everything
as orphans (tracking simply "stops working"); a broken terminal guard lets a
late re-delivered transit event un-deliver a parcel — none of these throw.
DB access is mocked (house style: no fixtures, SimpleNamespace fakes).
"""

from types import SimpleNamespace
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.carrier_routing import AUSTRIAN_POST, FEDEX, GLS
from erpnext_parcel_station.parcel.tracking import events
from erpnext_parcel_station.parcel.tracking import status_mapping as sm

_EVENT = {
    "carrier": AUSTRIAN_POST,
    "event_id": "AP::123",
    "tracking_number": "JJATA8219012000146048",
    "reference_ident_code": "1019012500384433902760",
    "event_timestamp": "2025-11-14 15:53:33",
    "canonical_status": sm.STATUS_IN_TRANSIT,
}


class TestUpsertEvents(FrappeTestCase):
    def test_known_event_id_is_skipped_without_insert(self):
        with (
            mock.patch.object(events.frappe.db, "exists", return_value=True) as exists,
            mock.patch.object(events.frappe, "get_doc") as get_doc,
        ):
            summary = events.upsert_events([_EVENT])
        exists.assert_called_once_with("Parcel Tracking Event", {"event_id": "AP::123"})
        get_doc.assert_not_called()
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["inserted"], 0)

    def test_new_event_is_inserted_and_matched(self):
        doc = mock.MagicMock()
        with (
            mock.patch.object(events.frappe.db, "exists", return_value=False),
            mock.patch.object(events, "find_shipment", return_value="SHIPMENT-00001") as finder,
            mock.patch.object(events.frappe, "get_doc", return_value=doc),
        ):
            summary = events.upsert_events([_EVENT])
        # Both codes must reach the matcher — IdentCode may be a partner code,
        # ReferenceIdentCode the Post's own; either can be the stored awb.
        finder.assert_called_once_with("JJATA8219012000146048", "1019012500384433902760")
        doc.insert.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["shipments"], {"SHIPMENT-00001"})

    def test_unmatched_event_is_stored_as_orphan(self):
        doc = mock.MagicMock()
        with (
            mock.patch.object(events.frappe.db, "exists", return_value=False),
            mock.patch.object(events, "find_shipment", return_value=None),
            mock.patch.object(events.frappe, "get_doc", return_value=doc),
        ):
            summary = events.upsert_events([_EVENT])
        doc.insert.assert_called_once()  # orphans are stored, never dropped
        self.assertEqual(summary["orphans"], 1)
        self.assertEqual(summary["shipments"], set())

    def test_concurrent_duplicate_insert_is_counted_not_raised(self):
        doc = mock.MagicMock()
        doc.insert.side_effect = events.frappe.UniqueValidationError
        with (
            mock.patch.object(events.frappe.db, "exists", return_value=False),
            mock.patch.object(events, "find_shipment", return_value=None),
            mock.patch.object(events.frappe, "get_doc", return_value=doc),
        ):
            summary = events.upsert_events([_EVENT])
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["inserted"], 0)


class TestUpdateShipmentStatus(FrappeTestCase):
    def _run(self, current_status, newest_event_status, reopen=False, description=""):
        newest = SimpleNamespace(
            canonical_status=newest_event_status,
            event_timestamp="2025-11-26 11:04:26",
            description=description,
            location_city="Altach",
            location_country="AT",
        )
        current = SimpleNamespace(tracking_status=current_status, awb_number="1019")
        with (
            mock.patch.object(events.frappe, "get_all", return_value=[newest]),
            mock.patch.object(events.frappe.db, "get_value", return_value=current),
            mock.patch.object(events, "_shipment_carrier", return_value=AUSTRIAN_POST),
            mock.patch.object(events.frappe.db, "set_value") as set_value,
        ):
            written = events.update_shipment_status("SHIPMENT-00001", reopen=reopen)
        return written, set_value

    def test_progress_to_delivered(self):
        written, set_value = self._run(sm.STATUS_IN_TRANSIT, sm.STATUS_DELIVERED)
        self.assertEqual(written, sm.STATUS_DELIVERED)
        values = set_value.call_args[0][2]
        self.assertEqual(values["tracking_status"], sm.STATUS_DELIVERED)
        self.assertIn("tracking_url", values)

    def test_terminal_is_sticky_against_transit_events(self):
        # Post files re-deliver old events; a transit event arriving after
        # delivery must not "un-deliver" the parcel.
        written, set_value = self._run(sm.STATUS_DELIVERED, sm.STATUS_IN_TRANSIT)
        self.assertIsNone(written)
        set_value.assert_not_called()

    def test_reopen_lifts_the_stickiness_for_corrected_events(self):
        # A ParcelShop deposit was stored as "Delivered"; once its event is
        # re-read as "Ready for Pickup" the Shipment has to follow.
        written, set_value = self._run(sm.STATUS_DELIVERED, sm.STATUS_READY_FOR_PICKUP, reopen=True)
        self.assertEqual(written, sm.STATUS_READY_FOR_PICKUP)
        self.assertEqual(set_value.call_args[0][2]["tracking_status"], sm.STATUS_READY_FOR_PICKUP)

    def test_terminal_to_terminal_is_allowed(self):
        written, _ = self._run(sm.STATUS_DELIVERED, sm.STATUS_RETURNED)
        self.assertEqual(written, sm.STATUS_RETURNED)

    def test_info_fits_erpnexts_varchar_140(self):
        # tracking_status_info is a core Data field; long carrier free text
        # made the UPDATE fail with "Data too long" (MariaDB 1406).
        _, set_value = self._run(sm.STATUS_IN_TRANSIT, sm.STATUS_READY_FOR_PICKUP, description="x" * 400)
        self.assertLessEqual(len(set_value.call_args[0][2]["tracking_status_info"]), 140)


class TestOpenShipments(FrappeTestCase):
    def test_carrier_filter_goes_through_resolve_carrier(self):
        # The Shipment.carrier column is free text ("Österreichische Post"),
        # so identity comparison must use the alias folding, not the raw name.
        rows = [
            {"name": "S-1", "awb_number": "A", "carrier": "Österreichische Post",
             "carrier_service": None, "custom_carrier_service": None, "delivery_type": None},
            {"name": "S-2", "awb_number": "B", "carrier": "GLS",
             "carrier_service": None, "custom_carrier_service": None, "delivery_type": None},
            {"name": "S-3", "awb_number": "C", "carrier": "FedEx",
             "carrier_service": None, "custom_carrier_service": None, "delivery_type": None},
        ]
        with (
            mock.patch.object(events.frappe, "get_all", return_value=rows),
            mock.patch.object(events, "_cutoff_days", return_value=30),
        ):
            self.assertEqual([r["name"] for r in events.open_shipments(AUSTRIAN_POST)], ["S-1"])
            self.assertEqual([r["name"] for r in events.open_shipments(GLS)], ["S-2"])
            self.assertEqual([r["name"] for r in events.open_shipments(FEDEX)], ["S-3"])

    def test_terminal_statuses_are_excluded_in_the_query(self):
        with (
            mock.patch.object(events.frappe, "get_all", return_value=[]) as get_all,
            mock.patch.object(events, "_cutoff_days", return_value=30),
        ):
            events.open_shipments(FEDEX)
        filters = get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["docstatus"], 1)
        self.assertEqual(filters["awb_number"], ("is", "set"))
        self.assertEqual(set(filters["tracking_status"][1]), set(sm.TERMINAL_STATUSES))


class TestTrackingUrl(FrappeTestCase):
    def test_urls_per_carrier(self):
        self.assertIn("post.at", events.tracking_url_for(AUSTRIAN_POST, "1019"))
        # The exact page: the former ".../paketverfolgung" redirects to GLS's 404.
        self.assertEqual(
            events.tracking_url_for(GLS, "ZE0Q2IE0"),
            "https://gls-group.com/AT/de/paket-verfolgen?match=ZE0Q2IE0",
        )
        self.assertIn("fedex", events.tracking_url_for(FEDEX, "Y"))

    def test_missing_inputs_yield_none(self):
        self.assertIsNone(events.tracking_url_for("DPD", "123"))
        self.assertIsNone(events.tracking_url_for(GLS, None))
