# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the FedEx tracking poll: batching, response flattening, isolation.

Why this needs tests: FedEx enforces the 30-numbers-per-request cap server
side — an over-full batch fails the whole request, and since the poll runs
unattended the only symptom would be an Error Log entry and silently stale
statuses. The response flattening likewise fails invisibly: a mis-read field
stores events with empty codes instead of crashing.
Fixture: ``tests/fixtures/fedex_track_response_sample.json``.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.tracking import fedex_track
from erpnext_parcel_station.parcel.tracking import status_mapping as sm

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "fedex_track_response_sample.json"


def _response():
    return json.loads(FIXTURE.read_text())


class TestResponseFlattening(FrappeTestCase):
    def test_scan_events_become_tracking_events(self):
        collected = fedex_track._events_from_response(_response(), {"794843185271": "SHIPMENT-00001"})
        self.assertEqual(len(collected), 3)  # error result contributes nothing
        delivered = next(e for e in collected if e["event_code"] == "DL")
        self.assertEqual(delivered["carrier"], "FEDEX")
        self.assertEqual(delivered["shipment"], "SHIPMENT-00001")
        self.assertEqual(delivered["tracking_number"], "794843185271")
        self.assertEqual(delivered["canonical_status"], sm.STATUS_DELIVERED)
        self.assertEqual(delivered["event_timestamp"], "2026-09-05 10:22:00")
        self.assertEqual(delivered["location_city"], "WOLFURT")
        self.assertTrue(delivered["event_id"].startswith("FEDEX::794843185271::"))

    def test_event_ids_are_deterministic(self):
        # Every poll returns the full history; the synthesized id must be
        # stable across polls or the table fills with copies.
        first = fedex_track._events_from_response(_response(), {})
        second = fedex_track._events_from_response(_response(), {})
        self.assertEqual([e["event_id"] for e in first], [e["event_id"] for e in second])

    def test_not_found_error_result_is_skipped_quietly(self):
        # TRACKING.TRACKINGNUMBER.NOTFOUND is normal right after label
        # creation — it must not produce events or raise.
        collected = fedex_track._events_from_response(_response(), {})
        self.assertFalse([e for e in collected if e["tracking_number"] == "794843999999"])

    def test_empty_response_yields_no_events(self):
        self.assertEqual(fedex_track._events_from_response({}, {}), [])
        self.assertEqual(fedex_track._events_from_response(None, {}), [])


class TestTimestampNormalization(FrappeTestCase):
    def test_offsets_are_stripped(self):
        self.assertEqual(fedex_track._normalize_timestamp("2026-09-05T14:20:00+02:00"), "2026-09-05 14:20:00")
        self.assertEqual(fedex_track._normalize_timestamp("2026-09-05T14:20:00-04:00"), "2026-09-05 14:20:00")
        self.assertEqual(fedex_track._normalize_timestamp("2026-09-05T14:20:00Z"), "2026-09-05 14:20:00")


class TestPolling(FrappeTestCase):
    def _rows(self, count):
        return [SimpleNamespace(name=f"S-{i}", awb_number=f"{700000000000 + i}") for i in range(count)]

    def test_batches_of_thirty(self):
        client = mock.MagicMock()
        client.track_shipments.return_value = {}
        with (
            mock.patch.object(fedex_track.frappe.db, "get_single_value", return_value=1),
            mock.patch.object(fedex_track.FedExAPIClient, "from_tracking_settings", return_value=client),
            mock.patch.object(fedex_track.events, "open_shipments", return_value=self._rows(65)),
            mock.patch.object(fedex_track.events, "update_shipment_statuses"),
            mock.patch.object(fedex_track.frappe.db, "commit"),
        ):
            summary = fedex_track.poll_fedex_tracking()
        self.assertEqual(client.track_shipments.call_count, 3)  # 30 + 30 + 5
        sizes = [len(call.args[0]) for call in client.track_shipments.call_args_list]
        self.assertEqual(sizes, [30, 30, 5])
        self.assertEqual(summary["shipments"], 65)

    def test_failed_batch_does_not_abort_the_run(self):
        client = mock.MagicMock()
        client.track_shipments.side_effect = [Exception("boom"), {}]
        with (
            mock.patch.object(fedex_track.frappe.db, "get_single_value", return_value=1),
            mock.patch.object(fedex_track.FedExAPIClient, "from_tracking_settings", return_value=client),
            mock.patch.object(fedex_track.events, "open_shipments", return_value=self._rows(40)),
            mock.patch.object(fedex_track.events, "update_shipment_statuses"),
            mock.patch.object(fedex_track.frappe.db, "commit"),
            mock.patch.object(fedex_track.frappe.db, "rollback"),
            mock.patch.object(fedex_track.frappe, "log_error") as log_error,
        ):
            summary = fedex_track.poll_fedex_tracking()
        self.assertEqual(client.track_shipments.call_count, 2)  # second batch still ran
        self.assertEqual(summary["failed_batches"], 1)
        log_error.assert_called_once()

    def test_disabled_poll_returns_none_without_client(self):
        with (
            mock.patch.object(fedex_track.frappe.db, "get_single_value", return_value=0),
            mock.patch.object(fedex_track.FedExAPIClient, "from_tracking_settings") as from_settings,
        ):
            self.assertIsNone(fedex_track.poll_fedex_tracking())
        from_settings.assert_not_called()
