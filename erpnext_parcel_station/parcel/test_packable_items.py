# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""What the parcel station offers the operator to pack.

A Delivery Note carries more than goods: the freight fee is a line, and for a
customs destination so is the pre-collected import duty. Neither can be put in
a box. Offering them in the scanning list invites an operator to tick a charge
as if it were cargo — and the resulting parcel then declares a tax line as
merchandise.

The previous rule filtered only items registered as a Carrier Service's
``shipping_item`` AND flagged non-stock. That named the freight fee and nothing
else: the import-charge item, registered as ``customs_item``, was shown as
goods. The rule is now the property that actually decides shippability.
"""

from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from . import shipment_contents


class TestPackableItemCodes(FrappeTestCase):
    def _packable(self, codes, items, groups=None, goods_groups=()):
        """``items`` maps item_code -> is_stock_item; ``groups`` item_code -> item_group."""
        groups = groups or {}
        rows = [
            {"name": code, "is_stock_item": flag, "item_group": groups.get(code)}
            for code, flag in items.items()
        ]
        with patch.object(shipment_contents.frappe, "get_all", return_value=rows), patch.object(
            shipment_contents, "packable_non_stock_groups", return_value=set(goods_groups)
        ):
            return shipment_contents.packable_item_codes(codes)

    def test_a_repair_service_in_a_configured_group_is_packable(self):
        """The exception to the exception: the order line is a service, the
        parcel holds the repaired shoe."""
        self.assertEqual(
            self._packable(
                ["REPARATUR", "00011"],
                {"REPARATUR": 0, "00011": 0},
                groups={"REPARATUR": "Reparatur", "00011": "Versandkosten"},
                goods_groups={"Reparatur"},
            ),
            {"REPARATUR"},
        )

    def test_stock_items_are_packable(self):
        self.assertEqual(
            self._packable(["SCHUH-1"], {"SCHUH-1": 1}),
            {"SCHUH-1"},
        )

    def test_the_freight_fee_is_not(self):
        self.assertEqual(
            self._packable(["SCHUH-1", "00011"], {"SCHUH-1": 1, "00011": 0}),
            {"SCHUH-1"},
        )

    def test_the_pre_collected_import_duty_is_not(self):
        """The case that slipped through: a customs charge line is money owed to
        a foreign authority, not something to put in the box."""
        self.assertEqual(
            self._packable(
                ["SCHUH-1", "ZOLL-CH"], {"SCHUH-1": 1, "ZOLL-CH": 0}
            ),
            {"SCHUH-1"},
        )

    def test_any_other_service_line_is_not_either(self):
        """The rule is stock/non-stock, so a service nobody registered anywhere
        is excluded too — that is the point of not enumerating charge items."""
        self.assertEqual(
            self._packable(
                ["SCHUH-1", "REPARATUR"], {"SCHUH-1": 1, "REPARATUR": 0}
            ),
            {"SCHUH-1"},
        )

    def test_an_unknown_item_is_kept(self):
        """A missing Item master is a data problem. Dropping the line would hide
        it from the operator instead of showing something wrong."""
        self.assertEqual(
            self._packable(["SCHUH-1", "GHOST"], {"SCHUH-1": 1}),
            {"SCHUH-1", "GHOST"},
        )

    def test_empty_input_needs_no_query(self):
        with patch.object(shipment_contents.frappe, "get_all") as get_all:
            self.assertEqual(shipment_contents.packable_item_codes([]), set())
        get_all.assert_not_called()

    def test_blank_codes_are_ignored(self):
        self.assertEqual(self._packable(["SCHUH-1", None, ""], {"SCHUH-1": 1}), {"SCHUH-1"})


class TestContentDescription(FrappeTestCase):
    """What the carrier is told is inside the parcel.

    ``Shipment.description_of_content`` travels on as the customs description
    when a shipment carries no per-item declaration. Building it from every
    Delivery Note line puts the freight fee and the pre-collected import tax on
    a customs document as if they were merchandise.
    """

    def _describe(self, rows, packable, selected=None):
        from types import SimpleNamespace

        from .api import core

        shipment = SimpleNamespace(description_of_content=None)
        dn = SimpleNamespace(get=lambda key, default=None: rows if key == "items" else default)
        # _ensure_descriptions imports the helper inside the function, so the
        # patch has to land on its home module, not on core.
        with patch(
            "erpnext_parcel_station.parcel.shipment_contents.packable_item_codes",
            return_value=packable,
        ):
            core._ensure_descriptions(shipment, dn, selected)
        return shipment.description_of_content

    def _row(self, code, name, qty):
        from types import SimpleNamespace

        return SimpleNamespace(item_code=code, item_name=name, qty=qty)

    def test_service_lines_are_left_out_of_the_description(self):
        rows = [
            self._row("SCHUH-1", "Holzschuh", 2),
            self._row("FEDEX-CH", "FedEx International Economy Schweiz", 1),
            self._row("ZOLL-CH", "Einfuhrabgaben Schweiz (vorausbezahlt)", 1),
        ]
        text = self._describe(rows, {"SCHUH-1"})
        self.assertEqual(text, "Holzschuh x2")
        self.assertNotIn("Einfuhrabgaben", text)
        self.assertNotIn("FedEx", text)

    def test_an_explicit_selection_is_taken_as_given(self):
        """The operator can only pick packable lines, so a selection is already
        clean and must not be filtered a second time."""
        selected = [{"item_code": "SCHUH-1", "item_name": "Holzschuh", "qty": 1}]
        text = self._describe([], set(), selected=selected)
        self.assertEqual(text, "Holzschuh x1")
