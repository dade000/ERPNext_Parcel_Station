# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the SFTP fetch loop: filtering, dedup, per-file failure isolation.

Why this needs tests: the fetcher runs unattended three times a day against a
server we do not control. The failure that matters is systemic, not local —
one malformed file aborting the loop would also block every later (healthy)
file, and broken dedup would re-import the whole directory on every run
because files stay on the server by default. No real paramiko involved: the
SFTP client is a SimpleNamespace fake (house style).
"""

import contextlib
import io
from types import SimpleNamespace
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.tracking import post_sftp


class _Settings(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)

    def get_password(self, key, raise_exception=True):
        return getattr(self, key, None)


def _settings(**overrides):
    values = {
        "enable_post_tracking": 1,
        "sftp_host": "sftp.example",
        "sftp_port": 22,
        "sftp_username": "user",
        "sftp_password": "secret",
        "sftp_directory": "out",
        "sftp_filename_prefix": "POSTTRACK_",
        "sftp_move_processed": 0,
    }
    values.update(overrides)
    return _Settings(**values)


def _fake_sftp(files: dict[str, bytes]):
    def open_(path, mode="rb"):
        name = path.rsplit("/", 1)[-1]
        return contextlib.closing(io.BytesIO(files[name]))

    return SimpleNamespace(
        listdir=lambda directory: list(files),
        open=open_,
        rename=mock.MagicMock(),
        close=lambda: None,
    )


class TestListing(FrappeTestCase):
    def test_prefix_and_extension_filter_with_chronological_order(self):
        sftp = _fake_sftp(
            {
                "POSTTRACK_19012_20251126120418_TE.xml": b"",
                "POSTTRACK_19012_20251114170402_TE.xml": b"",
                "POSTTRACK_19012_20251126080418_TE.tmp": b"",  # wrong extension
                "OTHER_19012.xml": b"",  # wrong prefix
            }
        )
        names = post_sftp._list_tracking_files(sftp, _settings())
        # Name order == chronological order (timestamp embedded in the name).
        self.assertEqual(
            names,
            ["POSTTRACK_19012_20251114170402_TE.xml", "POSTTRACK_19012_20251126120418_TE.xml"],
        )


class TestFetchLoop(FrappeTestCase):
    def _run(self, files, already_imported=(), process_side_effect=None):
        sftp = _fake_sftp(files)
        transport = SimpleNamespace(close=lambda: None)
        process = mock.MagicMock(return_value=True)
        if process_side_effect:
            process.side_effect = process_side_effect
        with (
            mock.patch.object(post_sftp, "_get_settings", return_value=_settings()),
            mock.patch.object(post_sftp, "_connect", return_value=(transport, sftp)),
            mock.patch.object(post_sftp.frappe.db, "exists", side_effect=lambda dt, name: name in already_imported),
            mock.patch.object(post_sftp, "_process_file", process),
            mock.patch.object(post_sftp, "_log_failure") as log_failure,
            mock.patch.object(post_sftp.events, "rematch_orphan_events", return_value=set()),
            mock.patch.object(post_sftp.events, "update_shipment_statuses"),
            mock.patch.object(post_sftp.frappe.db, "commit"),
            mock.patch.object(post_sftp.frappe.db, "rollback") as rollback,
            mock.patch.object(post_sftp.frappe, "log_error"),
        ):
            summary = post_sftp.fetch_post_tracking_files()
        return summary, process, rollback, log_failure

    def test_already_imported_files_are_skipped_before_download(self):
        files = {"POSTTRACK_1.xml": b"a", "POSTTRACK_2.xml": b"b"}
        summary, process, _, _ = self._run(files, already_imported={"POSTTRACK_1.xml"})
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args[0][0], "POSTTRACK_2.xml")
        self.assertEqual(summary["files"], 1)

    def test_one_broken_file_does_not_block_the_next(self):
        files = {"POSTTRACK_1.xml": b"a", "POSTTRACK_2.xml": b"b"}
        summary, process, rollback, log_failure = self._run(
            files, process_side_effect=[Exception("boom"), True]
        )
        self.assertEqual(process.call_count, 2)  # second file still processed
        self.assertEqual(summary["failed"], ["POSTTRACK_1.xml"])
        rollback.assert_called_once()
        log_failure.assert_called_once()

    def test_not_configured_returns_none_without_connecting(self):
        with (
            mock.patch.object(
                post_sftp,
                "_get_settings",
                side_effect=post_sftp.PostTrackingNotConfiguredError("disabled"),
            ),
            mock.patch.object(post_sftp, "_connect") as connect,
        ):
            self.assertIsNone(post_sftp.fetch_post_tracking_files())
        connect.assert_not_called()


class TestProcessFile(FrappeTestCase):
    CONTENT = b"<xml/>"

    def test_same_bytes_under_new_name_logged_as_duplicate(self):
        summary = {"files": 1, "imported": 0, "duplicates": 0, "failed": [], "shipments": set()}
        with (
            mock.patch.object(post_sftp.frappe.db, "exists", return_value=True),
            mock.patch.object(post_sftp, "_create_log") as create_log,
            mock.patch.object(post_sftp.events, "upsert_events") as upsert,
        ):
            ok = post_sftp._process_file("POSTTRACK_X.xml", "out/POSTTRACK_X.xml", self.CONTENT, summary)
        self.assertTrue(ok)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(create_log.call_args.kwargs.get("status"), "Duplicate")
        upsert.assert_not_called()

    def test_unparseable_file_logged_as_failed(self):
        summary = {"files": 1, "imported": 0, "duplicates": 0, "failed": [], "shipments": set()}
        with (
            mock.patch.object(post_sftp.frappe.db, "exists", return_value=False),
            mock.patch.object(post_sftp, "_create_log") as create_log,
        ):
            ok = post_sftp._process_file("POSTTRACK_X.xml", "out/POSTTRACK_X.xml", b"not xml", summary)
        self.assertFalse(ok)
        self.assertEqual(summary["failed"], ["POSTTRACK_X.xml"])
        self.assertEqual(create_log.call_args.kwargs.get("status"), "Failed")

    def test_imported_file_writes_counters_and_source_link(self):
        summary = {"files": 1, "imported": 0, "duplicates": 0, "failed": [], "shipments": set()}
        log = mock.MagicMock()
        log.name = "POSTTRACK_X.xml"
        parsed = {"header": {}, "events": [{"event_id": "AP::1"}, {"event_id": "AP::2"}]}
        result = {"inserted": 1, "duplicates": 1, "matched": 1, "orphans": 0, "shipments": {"S-1"}}
        with (
            mock.patch.object(post_sftp.frappe.db, "exists", return_value=False),
            mock.patch.object(post_sftp, "_create_log", return_value=log),
            mock.patch.object(post_sftp.post_xml, "parse", return_value=parsed),
            mock.patch.object(post_sftp.events, "upsert_events", return_value=result) as upsert,
        ):
            ok = post_sftp._process_file("POSTTRACK_X.xml", "out/POSTTRACK_X.xml", self.CONTENT, summary)
        self.assertTrue(ok)
        self.assertEqual(summary["shipments"], {"S-1"})
        # every event is tagged with its source file for forensics
        for event in upsert.call_args[0][0]:
            self.assertEqual(event["source_file"], "POSTTRACK_X.xml")
        counters = log.db_set.call_args[0][0]
        self.assertEqual(counters["event_count"], 2)
        self.assertEqual(counters["duplicate_event_count"], 1)
