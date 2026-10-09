# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards carrier routing — the decision that says which carrier API a Shipment
is handed to.

Why this test exists
--------------------
Routing used to be a boolean ``is_gls`` recomputed at five call sites, each
phrased as "GLS, or else Austrian Post". That is silently wrong the moment a
third carrier exists: a FedEx shipment satisfies "not GLS", so the cancel hook
would cancel its parcel AT AUSTRIAN POST, and the pre-flight check would refuse
to create it for lack of an Austrian Post service ID.

Both failures are invisible in normal use — the shipment looks fine locally, and
the wrong carrier just does nothing with an unknown tracking ID. So the
invariant has to be asserted here rather than observed in production:

    a Shipment that routes to one carrier must never resolve to another.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from .carrier_routing import (
    AUSTRIAN_POST,
    DEFAULT_CARRIER,
    FEDEX,
    GLS,
    PARCELSHOP_DELIVERY_TYPE,
    carrier_from_name,
    resolve_carrier,
)


def _shipment(**fields) -> SimpleNamespace:
    """A Shipment-like stand-in carrying only the fields routing reads."""
    base = {
        "name": "SHIPMENT-TEST",
        "delivery_type": None,
        "carrier": None,
        "carrier_service": None,
        "custom_carrier_service": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


class TestCarrierNameNormalisation(FrappeTestCase):
    def test_known_names_map_to_identities(self):
        for name, expected in [
            ("GLS", GLS),
            ("gls", GLS),
            ("Austrian Post", AUSTRIAN_POST),
            ("austrian_post", AUSTRIAN_POST),
            ("AUSTRIAN-POST", AUSTRIAN_POST),
            # The live Carrier master's actual name — accents must fold, not
            # get stripped (that yielded STERREICHISCHEPOST and no match).
            ("Österreichische Post", AUSTRIAN_POST),
            ("Oesterreichische Post", AUSTRIAN_POST),
            ("FedEx", FEDEX),
            ("FEDEX", FEDEX),
            ("Federal Express", FEDEX),
        ]:
            with self.subTest(name=name):
                self.assertEqual(carrier_from_name(name), expected)

    def test_unknown_and_empty_names_are_not_guessed(self):
        for name in ("DPD", "", None, "   "):
            with self.subTest(name=name):
                self.assertIsNone(carrier_from_name(name))


class TestResolveCarrier(FrappeTestCase):
    def test_parcelshop_delivery_pins_to_gls(self):
        """Parcel-shop delivery is a GLS-only product and outranks the carrier
        field — the shop lives on the Address, not on the Carrier Service."""
        doc = _shipment(delivery_type=PARCELSHOP_DELIVERY_TYPE, carrier="FedEx")
        self.assertEqual(resolve_carrier(doc), GLS)

    def test_carrier_field_wins_over_carrier_service_lookup(self):
        doc = _shipment(carrier="FedEx", carrier_service="CS-GLS")
        with patch("frappe.db.get_value") as get_value:
            self.assertEqual(resolve_carrier(doc), FEDEX)
        get_value.assert_not_called()

    def test_resolves_via_carrier_service_link(self):
        doc = _shipment(carrier_service="CS-FEDEX-DE")
        with patch("frappe.db.get_value", return_value="FedEx"):
            self.assertEqual(resolve_carrier(doc), FEDEX)

    def test_resolves_via_legacy_custom_carrier_service_link(self):
        """Pre-rework Shipments stored the link in custom_carrier_service."""
        doc = _shipment(custom_carrier_service="CS-GLS-AT")
        with patch("frappe.db.get_value", return_value="GLS"):
            self.assertEqual(resolve_carrier(doc), GLS)

    def test_accepts_a_plain_dict(self):
        """The Austrian Post payload path passes as_dict() mappings around."""
        self.assertEqual(resolve_carrier({"carrier": "Austrian Post"}), AUSTRIAN_POST)

    def test_unknown_carrier_falls_back_to_default(self):
        """An unrecognised Carrier master keeps the historical fallback rather
        than raising — a mis-configured master must not break label dispatch."""
        doc = _shipment(carrier="DPD")
        with patch("frappe.db.get_value", return_value=None):
            self.assertEqual(resolve_carrier(doc), DEFAULT_CARRIER)

    def test_missing_carrier_service_record_does_not_raise(self):
        doc = _shipment(carrier_service="CS-DELETED")
        with patch("frappe.db.get_value", side_effect=Exception("no such record")):
            self.assertEqual(resolve_carrier(doc), DEFAULT_CARRIER)

    def test_bare_shipment_keeps_pre_refactor_default(self):
        """Before the refactor everything unidentified went to Austrian Post.
        Existing in-flight Shipments must route exactly as they did."""
        self.assertEqual(resolve_carrier(_shipment()), AUSTRIAN_POST)


class TestCarrierIsolation(FrappeTestCase):
    """The regression this module exists to prevent: routing must never leak a
    shipment from one carrier into another carrier's API."""

    def test_fedex_shipment_never_resolves_to_austrian_post(self):
        # The cancel hook skips unless the carrier IS Austrian Post; under the
        # old "skip if GLS" guard this shipment would have been cancelled at
        # Austrian Post using a FedEx tracking number.
        for doc, lookup in [
            (_shipment(carrier="FedEx"), None),
            (_shipment(carrier_service="CS-FEDEX-CH"), "FedEx"),
        ]:
            with self.subTest(doc=doc):
                with patch("frappe.db.get_value", return_value=lookup):
                    self.assertEqual(resolve_carrier(doc), FEDEX)

    def test_each_shipment_resolves_to_exactly_one_carrier(self):
        cases = [
            (_shipment(delivery_type=PARCELSHOP_DELIVERY_TYPE), GLS),
            (_shipment(carrier="GLS"), GLS),
            (_shipment(carrier="Austrian Post"), AUSTRIAN_POST),
            (_shipment(carrier="FedEx"), FEDEX),
        ]
        for doc, expected in cases:
            with self.subTest(expected=expected):
                resolved = resolve_carrier(doc)
                self.assertEqual(resolved, expected)
                for other in (GLS, AUSTRIAN_POST, FEDEX):
                    if other != expected:
                        self.assertNotEqual(resolved, other)
