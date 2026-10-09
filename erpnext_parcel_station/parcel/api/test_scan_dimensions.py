# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the unit of the parcel dimensions.

The dimension scanner emits MILLIMETRES, while everything downstream is
centimetres: the Shipment Parcel fields are labelled "Length (cm)" and Austrian
Post's ColloRow carries no unit at all — it reads the bare numbers as cm. The
scan values used to be forwarded unconverted, which declared every parcel ten
times too large (the Post Label Center showed the mm figures as cm).
"""

from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from . import core


class TestScanDimensions(FrappeTestCase):
    def test_scanner_millimetres_become_centimetres(self):
        """"L:350W:250H:150" is a 35 x 25 x 15 cm parcel."""
        self.assertEqual(
            core._parse_scan_dimensions(length=350, width=250, height=150),
            {"length": 35.0, "width": 25.0, "height": 15.0},
        )

    def test_partial_scan_converts_what_is_present(self):
        self.assertEqual(core._parse_scan_dimensions(length=400), {"length": 40.0})

    def test_no_dimensions_stay_none(self):
        """Shipments without a dimension scan must send no L/W/H at all."""
        self.assertIsNone(core._parse_scan_dimensions())
        self.assertIsNone(core._parse_scan_dimensions(length="", width=None, height=""))

    def test_fractional_millimetres_survive_the_conversion(self):
        """355 mm is 35.5 cm — the parcel row keeps the decimal even though the
        carrier payload later rounds it to an xs:int."""
        self.assertEqual(core._parse_scan_dimensions(height=355), {"height": 35.5})

    def test_converted_values_reach_the_carrier_payload_as_centimetres(self):
        """End of the chain: what _parse_scan_dimensions produces is what the
        ColloRow carries, rounded to the integer the schema demands."""
        from . import carrier

        dims = core._parse_scan_dimensions(length=350, width=250, height=155)
        shipment = {"shipment_parcel": [dict(dims, weight=1.0, count=1)]}

        self.assertEqual(
            carrier._parcel_dimensions(shipment),
            {"length": 35, "width": 25, "height": 16},
        )
