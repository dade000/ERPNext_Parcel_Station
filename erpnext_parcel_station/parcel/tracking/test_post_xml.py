# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the POSTTRACK XML parser against the quirks of real Post files.

Why this needs tests: a parsing regression is silent in production — the
scheduler logs a Failed file (or worse, imports events with empty codes) and
tracking simply stops updating. The fixture reproduces what real files
actually contain: a UTF-8 BOM, double-encoded umlauts ("GÃ¼nzburg"), city
names and hub labels inside EventPostalCode, partner-network IdentCodes, and
the same ParcelEventId delivered twice. Fixture:
``tests/fixtures/posttrack_sample.xml`` (sanitized, real-shaped).
"""

import unittest
from pathlib import Path

from erpnext_parcel_station.parcel.tracking import post_xml
from erpnext_parcel_station.parcel.tracking import status_mapping as sm

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "posttrack_sample.xml"


def _parse_fixture():
    return post_xml.parse(FIXTURE.read_bytes())


class TestPostXMLParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = _parse_fixture()
        cls.events = cls.parsed["events"]
        cls.by_id = {}
        for event in cls.events:
            cls.by_id.setdefault(event["event_id"], event)

    def test_bom_does_not_break_parsing(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(raw[:3], b"\xef\xbb\xbf")  # fixture really has a BOM
        self.assertTrue(self.events)

    def test_header_extracted(self):
        self.assertEqual(self.parsed["header"]["TrackingVersion"], "2")
        self.assertEqual(self.parsed["header"]["EventCount"], "11")

    def test_duplicate_event_ids_pass_through(self):
        # Dedup is upsert_events' job (unique event_id column); the parser
        # must hand over every occurrence.
        ids = [event["event_id"] for event in self.events]
        self.assertEqual(len(ids), 11)
        self.assertEqual(ids.count("AP::1000000005"), 2)

    def test_hinterlegung_is_ready_for_pickup(self):
        # HIA/HO with ShipmentState EB is the parcel sitting at a post office
        # waiting for the customer — the one state ops asked for and the only
        # event that carries a BranchKey (the pickup branch).
        deposited = self.by_id["AP::1000000008"]
        self.assertEqual(deposited["carrier_status"], "EB")
        self.assertEqual(deposited["canonical_status"], sm.STATUS_READY_FOR_PICKUP)
        self.assertEqual(deposited["event_code"], "HIA")
        self.assertEqual(deposited["reason_code"], "HO")
        self.assertEqual(deposited["location_city"], "Bregenz-Schendlingen")
        # The branch is kept so the pickup location survives in the raw payload.
        self.assertIn("69040710056490301", deposited["raw_payload"])

    def test_failed_delivery_attempt_stays_in_transit(self):
        # ZUH/SH -> RU is a retained parcel, not a return: in the real files
        # both such parcels were delivered within a day. Flagging it as a
        # problem would cry wolf daily; the digest's stuck check catches the
        # ones that genuinely stop moving.
        retained = self.by_id["AP::1000000009"]
        self.assertEqual(retained["carrier_status"], "RU")
        self.assertEqual(retained["canonical_status"], sm.STATUS_IN_TRANSIT)

    def test_event_meaning_beats_the_stale_shipment_state(self):
        # AZT/AT ("Sendung in Zustellung") arrives with IZ on one parcel and
        # ZU on another, because the state describes the parcel at
        # file-creation time. The row must show what the event MEANS — a
        # Delivered pill next to "Sendung in Zustellung" is a lie, even when
        # the parcel was indeed delivered later that morning.
        self.assertEqual(self.by_id["AP::1000000004"]["event_code"], "AZT")
        self.assertEqual(self.by_id["AP::1000000004"]["canonical_status"], sm.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(self.by_id["AP::1000000010"]["event_code"], "AZT")
        self.assertEqual(self.by_id["AP::1000000010"]["carrier_status"], "ZU")
        self.assertEqual(self.by_id["AP::1000000010"]["canonical_status"], sm.STATUS_OUT_FOR_DELIVERY)

    def test_event_field_mapping(self):
        delivered = self.by_id["AP::1000000007"]
        self.assertEqual(delivered["carrier"], "AUSTRIAN_POST")
        self.assertEqual(delivered["tracking_number"], "1019012500386270268443")
        self.assertEqual(delivered["reference_ident_code"], "1019012500386270268443")
        self.assertEqual(delivered["event_timestamp"], "2025-11-26 11:04:26")
        self.assertEqual(delivered["event_code"], "ZUS")
        self.assertEqual(delivered["reason_code"], "ZP")
        self.assertEqual(delivered["carrier_status"], "ZU")
        self.assertEqual(delivered["canonical_status"], sm.STATUS_DELIVERED)
        self.assertEqual(delivered["location_city"], "Altach")
        self.assertEqual(delivered["location_country"], "AT")
        # The description is the Post's own wording for the code pair, not the
        # free-text Remark — "ZUS/ZP" alone tells an operator nothing.
        self.assertEqual(delivered["description"], "Sendung wurde zugestellt")

    def test_remark_is_the_description_fallback_for_unknown_codes(self):
        event = post_xml._to_tracking_event(
            {"ParcelEventTypeCode": "XXX", "ParcelEventReasonCode": "??", "Remark": "Sonderfall"}
        )
        self.assertEqual(event["description"], "Sonderfall")

    def test_partner_network_ident_code_kept(self):
        partner = self.by_id["AP::1000000002"]
        self.assertEqual(partner["tracking_number"], "JJATA8219012000146048")
        self.assertEqual(partner["reference_ident_code"], "1019012500384433902760")
        self.assertEqual(partner["canonical_status"], sm.STATUS_IN_TRANSIT)

    def test_mojibake_repaired(self):
        broken = self.by_id["AP::1000000002"]
        self.assertEqual(broken["location_postal_code"], "Günzburg-8")
        delivered = self.by_id["AP::1000000005"]
        self.assertIn("Heidi Hänichen", delivered["raw_payload"])

    def test_dirty_postal_code_passes_through(self):
        hub = self.by_id["AP::1000000003"]
        self.assertEqual(hub["location_postal_code"], "Eurodis")

    def test_canonical_status_per_shipment_state(self):
        self.assertEqual(self.by_id["AP::1000000001"]["canonical_status"], sm.STATUS_ANNOUNCED)
        self.assertEqual(self.by_id["AP::1000000004"]["canonical_status"], sm.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(self.by_id["AP::1000000006"]["canonical_status"], sm.STATUS_IN_TRANSIT)

    def test_not_xml_raises_parse_error(self):
        with self.assertRaises(post_xml.PostTrackingParseError):
            post_xml.parse(b"this is not xml at all")


class TestMojibakeRepair(unittest.TestCase):
    def test_repairs_double_encoding(self):
        self.assertEqual(post_xml._repair_mojibake("GÃ¼nzburg"), "Günzburg")
        self.assertEqual(post_xml._repair_mojibake("KÃ¶ngen"), "Köngen")

    def test_leaves_clean_text_alone(self):
        self.assertEqual(post_xml._repair_mojibake("Günzburg"), "Günzburg")
        self.assertEqual(post_xml._repair_mojibake("Wolfurt"), "Wolfurt")

    def test_never_raises_on_unencodable_text(self):
        # Characters latin-1 cannot express -> repair impossible, keep as-is.
        value = "Ã…land — 5€"
        self.assertIsInstance(post_xml._repair_mojibake(value), str)
