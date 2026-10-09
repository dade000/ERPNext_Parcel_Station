# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the carrier-code -> canonical-status tables.

Why this needs tests: a wrong mapping is invisible in production — the import
succeeds, the Shipment just carries a wrong status, and the digest either
cries wolf (healthy parcel reported as Problem) or stays silent on a real
return. The tables are also the one place where three carriers' vocabularies
must stay aligned with the Select options patched onto
``Shipment.tracking_status`` — drift there makes ``frappe.db.set_value``
write an option the field does not offer.
"""

import unittest

from erpnext_parcel_station.parcel.tracking import status_mapping as sm


class TestPostMapping(unittest.TestCase):
    def test_shipment_states(self):
        self.assertEqual(sm.map_post_event("AV", "AVI", "SE"), sm.STATUS_ANNOUNCED)
        self.assertEqual(sm.map_post_event("IV", "TRA", "TA"), sm.STATUS_IN_TRANSIT)
        self.assertEqual(sm.map_post_event("IZ", "AZT", "AT"), sm.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(sm.map_post_event("ZU", "ZUS", "ZU"), sm.STATUS_DELIVERED)

    def test_sorting_event_is_not_a_problem(self):
        # VER/VT ("Verteilung") appears on healthy transit parcels in real
        # POSTTRACK files — it must never map to Problem or Lost.
        self.assertEqual(sm.map_post_event("IV", "VER", "VT"), sm.STATUS_IN_TRANSIT)

    def test_deposited_at_post_office(self):
        # EB, delivered by the HIA/HO event: the parcel is at a post office
        # waiting to be collected.
        self.assertEqual(sm.map_post_event("EB", "HIA", "HO"), sm.STATUS_READY_FOR_PICKUP)

    def test_retained_and_return_acceptance_states(self):
        self.assertEqual(sm.map_post_event("RU", "ZUH", "SH"), sm.STATUS_IN_TRANSIT)
        self.assertEqual(sm.map_post_event("AN", "ANA", "AO"), sm.STATUS_IN_TRANSIT)

    def test_unknown_codes_fall_back_to_shipment_state(self):
        self.assertEqual(sm.map_post_event("ZU", "XXX", "??"), sm.STATUS_DELIVERED)
        self.assertEqual(sm.map_post_event(None, None, None), sm.STATUS_IN_TRANSIT)

    def test_damaged_delivery_is_a_problem_despite_delivered_state(self):
        # ZUS/BZ "beschädigt übergeben" arrives with ShipmentState ZU — the
        # parcel *was* handed over, so the state alone reads as a clean
        # delivery and the damage would never surface.
        self.assertEqual(sm.map_post_event("ZU", "ZUS", "BZ"), sm.STATUS_PROBLEM)

    def test_delivered_return_shipment_is_returned(self):
        # ZUS/ZR "Zustellung Retoursendung": what was delivered is the return,
        # i.e. the goods came back to us. Also ZU on the wire.
        self.assertEqual(sm.map_post_event("ZU", "ZUS", "ZR"), sm.STATUS_RETURNED)

    def test_ordinary_delivery_reasons_stay_delivered(self):
        # The other documented ZUS reasons are all successful hand-overs.
        for reason in ("ZN", "ZP", "ZU", "ZV", "ZW", "ZY"):
            self.assertEqual(sm.map_post_event("ZU", "ZUS", reason), sm.STATUS_DELIVERED, reason)

    def test_missing_state_falls_back_to_the_event_type(self):
        # ShipmentState is optional per the spec while the code fields are
        # mandatory; without the fallback a delivery with no state would be
        # reported as still in transit.
        self.assertEqual(sm.map_post_event("", "ZUS", "ZU"), sm.STATUS_DELIVERED)
        self.assertEqual(sm.map_post_event(None, "HIA", "HO"), sm.STATUS_READY_FOR_PICKUP)
        self.assertEqual(sm.map_post_event(None, "AVI", "SE"), sm.STATUS_ANNOUNCED)
        self.assertEqual(sm.map_post_event(None, "XXX", "??"), sm.STATUS_IN_TRANSIT)

    def test_official_list_overrides_a_state_that_only_knows_location(self):
        # From the Post's Event-/Reason-Liste. The state says where the parcel
        # is; these say what happened to it, and nothing in the state can.
        self.assertEqual(sm.map_post_event("IZ", "ZUH", "AV"), sm.STATUS_PROBLEM)  # Annahme verweigert
        self.assertEqual(sm.map_post_event("IZ", "ZUH", "EU"), sm.STATUS_PROBLEM)  # Empfänger unbekannt
        self.assertEqual(sm.map_post_event("IV", "CUD", "CQ"), sm.STATUS_PROBLEM)  # Zoll hält zurück
        self.assertEqual(sm.map_post_event("IV", "RTS", "AR"), sm.STATUS_RETURNED)  # Rücksendung
        self.assertEqual(sm.map_post_event("IV", "BNT", "2086"), sm.STATUS_RETURNED)

    def test_transport_events_keep_deferring_to_the_state(self):
        # The generated table must not swallow the ordinary lifecycle: delays,
        # customs steps and distribution stay on the state.
        self.assertEqual(sm.map_post_event("IV", "VER", "VT"), sm.STATUS_IN_TRANSIT)
        self.assertEqual(sm.map_post_event("EB", "HIA", "HO"), sm.STATUS_READY_FOR_PICKUP)
        self.assertEqual(sm.map_post_event("ZU", "ZUS", "ZU"), sm.STATUS_DELIVERED)
        self.assertEqual(sm.map_post_event("IV", "CUD", "CD"), sm.STATUS_IN_TRANSIT)

    def test_official_wording_is_available_as_a_description(self):
        self.assertEqual(sm.post_event_text("HIA", "HO"), "Sendung abholbereit")
        self.assertEqual(sm.post_event_text("ZUH", "BN"), "Sendung in Zustellung")
        self.assertEqual(sm.post_event_text("XXX", "??"), "")

    def test_unambiguous_wording_beats_the_stale_state(self):
        # The state is the parcel's state at FILE CREATION: a morning AZT scan
        # arrives in the noon file with ZU once delivery happened in between.
        # An "auf Zustelltour" row wearing a Delivered pill is the lie ops
        # spotted — the wording decides for classified pairs.
        self.assertEqual(sm.map_post_event("IZ", "AZT", "AT"), sm.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(sm.map_post_event("ZU", "AZT", "AT"), sm.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(sm.map_post_event("ZU", "ZUH", "BN"), sm.STATUS_OUT_FOR_DELIVERY)
        # "Sendung abholbereit" via ZUH/HS — the NL partner case from the
        # real files, previously swallowed by state ZU.
        self.assertEqual(sm.map_post_event("ZU", "ZUH", "HS"), sm.STATUS_READY_FOR_PICKUP)

    def test_state_decides_for_unclassified_wordings(self):
        # "Sendung in Verteilung" says nothing the state doesn't; there the
        # state remains the better signal.
        self.assertEqual(sm.map_post_event("IV", "VER", "VT"), sm.STATUS_IN_TRANSIT)
        self.assertEqual(sm.map_post_event("IZ", "VER", "VT"), sm.STATUS_OUT_FOR_DELIVERY)

    def test_case_and_whitespace_tolerant(self):
        self.assertEqual(sm.map_post_event(" zu ", None, None), sm.STATUS_DELIVERED)


class TestFedExMapping(unittest.TestCase):
    def test_lifecycle_codes(self):
        self.assertEqual(sm.map_fedex_code("IN"), sm.STATUS_ANNOUNCED)
        self.assertEqual(sm.map_fedex_code("PU"), sm.STATUS_IN_TRANSIT)
        self.assertEqual(sm.map_fedex_code("OD"), sm.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(sm.map_fedex_code("DL"), sm.STATUS_DELIVERED)
        self.assertEqual(sm.map_fedex_code("DE"), sm.STATUS_PROBLEM)
        self.assertEqual(sm.map_fedex_code("RS"), sm.STATUS_RETURNED)

    def test_hold_at_location_is_ready_for_pickup(self):
        # HL = deposited at a FedEx location for customer pickup — the
        # FedEx equivalent of "hinterlegt", not a transit state.
        self.assertEqual(sm.map_fedex_code("HL"), sm.STATUS_READY_FOR_PICKUP)

    def test_unknown_code_degrades_to_in_transit(self):
        self.assertEqual(sm.map_fedex_code("ZZ"), sm.STATUS_IN_TRANSIT)
        self.assertEqual(sm.map_fedex_code(None), sm.STATUS_IN_TRANSIT)


class TestGLSMapping(unittest.TestCase):
    def test_lifecycle_statuses(self):
        self.assertEqual(sm.map_gls_status("PREADVICE"), sm.STATUS_ANNOUNCED)
        self.assertEqual(sm.map_gls_status("INTRANSIT"), sm.STATUS_IN_TRANSIT)
        self.assertEqual(sm.map_gls_status("INDELIVERY"), sm.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(sm.map_gls_status("DELIVERED"), sm.STATUS_DELIVERED)
        self.assertEqual(sm.map_gls_status("NOTDELIVERED"), sm.STATUS_PROBLEM)
        self.assertEqual(sm.map_gls_status("RETURNED"), sm.STATUS_RETURNED)

    def test_parcelshop_delivery_is_ready_for_pickup_not_delivered(self):
        # DELIVEREDPS = parcel at the shop, waiting. Mapping it to Delivered
        # would terminate polling before the customer ever collected it —
        # and an uncollected parcel would never age into the digest.
        self.assertEqual(sm.map_gls_status("DELIVEREDPS"), sm.STATUS_READY_FOR_PICKUP)

    def test_spacing_variants(self):
        # ShipIT installations render some statuses with spaces ("IN TRANSIT").
        self.assertEqual(sm.map_gls_status("IN TRANSIT"), sm.STATUS_IN_TRANSIT)

    def test_unknown_status_degrades_to_in_transit(self):
        self.assertEqual(sm.map_gls_status("SOMETHINGNEW"), sm.STATUS_IN_TRANSIT)


class TestVocabularyAlignment(unittest.TestCase):
    """Every mapping output must be a valid Shipment.tracking_status option."""

    CANONICAL = {
        sm.STATUS_IN_PROGRESS,
        sm.STATUS_ANNOUNCED,
        sm.STATUS_IN_TRANSIT,
        sm.STATUS_OUT_FOR_DELIVERY,
        sm.STATUS_READY_FOR_PICKUP,
        sm.STATUS_DELIVERED,
        sm.STATUS_PROBLEM,
        sm.STATUS_RETURNED,
        sm.STATUS_LOST,
    }

    def test_ready_for_pickup_is_not_terminal(self):
        # A deposited parcel still moves: Delivered on pickup, Returned when
        # the deadline passes. Terminal would freeze it and stop the poll.
        self.assertNotIn(sm.STATUS_READY_FOR_PICKUP, sm.TERMINAL_STATUSES)

    def test_all_table_values_are_canonical(self):
        for table in (sm._POST_SHIPMENT_STATES, sm._POST_EVENT_OVERRIDES, sm._FEDEX_CODES, sm._GLS_STATUSES):
            for value in table.values():
                self.assertIn(value, self.CANONICAL)

    def test_terminal_is_subset(self):
        self.assertTrue(sm.TERMINAL_STATUSES <= self.CANONICAL)
        self.assertTrue(sm.PROBLEM_STATUSES <= self.CANONICAL)

    def test_patch_options_match_canonical(self):
        # The Property Setter writes what the Select accepts — drift between
        # the patch constant and these constants makes db.set_value write an
        # option the field does not offer.
        from erpnext_parcel_station.patches.v15.extend_shipment_tracking_status_options import (
            TRACKING_STATUS_OPTIONS,
        )

        options = {option for option in TRACKING_STATUS_OPTIONS.split("\n") if option}
        self.assertEqual(options, self.CANONICAL)
