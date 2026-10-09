# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the display facts the Parcel Station shows next to a Delivery Note.

Why this needs tests: the work list flags a parcel as "Zoll" or "Paketshop"
and prints a carrier mark from these values. A wrong flag sends the packer
looking for customs papers that do not exist — or lets a Swiss parcel go out
without them being expected. Nothing here drives the label call itself.
DB access is mocked (house style: no fixtures, SimpleNamespace fakes).
"""

from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.api import shipments_ui as ui


def _meta(has_shop_flag=True):
    return SimpleNamespace(has_field=lambda field: has_shop_flag and field == "is_parcel_shop")


class TestAddressFacts(FrappeTestCase):
    def _facts(self, address, country_code, has_shop_flag=True):
        def get_value(doctype, name, fields, as_dict=False):
            if doctype == "Address":
                return address
            if doctype == "Country":
                return country_code
            raise AssertionError(doctype)

        with (
            mock.patch.object(ui.frappe, "get_meta", return_value=_meta(has_shop_flag)),
            mock.patch.object(ui.frappe.db, "get_value", side_effect=get_value),
        ):
            return ui._address_facts("Some-Address")

    def test_eu_home_delivery_has_no_flags(self):
        address = frappe._dict(city="München", country="Germany", is_parcel_shop=0)
        self.assertEqual(
            self._facts(address, "de"),
            {"city": "München", "country_code": "DE", "is_parcel_shop": 0, "customs": 0},
        )

    def test_outside_the_customs_union_is_flagged(self):
        address = frappe._dict(city="Zürich", country="Switzerland", is_parcel_shop=0)
        self.assertEqual(self._facts(address, "ch")["customs"], 1)

    def test_parcel_shop_flag_is_carried(self):
        address = frappe._dict(city="Bregenz", country="Austria", is_parcel_shop=1)
        self.assertEqual(self._facts(address, "at")["is_parcel_shop"], 1)

    def test_site_without_the_parcel_shop_field(self):
        address = frappe._dict(city="Bregenz", country="Austria")
        self.assertEqual(self._facts(address, "at", has_shop_flag=False)["is_parcel_shop"], 0)

    def test_missing_address_yields_empty_facts_without_a_query(self):
        with mock.patch.object(ui.frappe.db, "get_value") as get_value:
            self.assertEqual(
                ui._address_facts(None),
                {"city": "", "country_code": "", "is_parcel_shop": 0, "customs": 0},
            )
        get_value.assert_not_called()

    def test_unknown_country_is_not_treated_as_customs(self):
        # No country on the address: "we don't know" must not demand papers.
        address = frappe._dict(city="", country=None, is_parcel_shop=0)
        self.assertEqual(self._facts(address, None)["customs"], 0)


class TestDeliveryNoteShippingFacts(FrappeTestCase):
    def _dn(self, **overrides):
        values = {
            "customer_name": "Max Mustermann",
            "custom_carrier_service": "Österreichische Post-30",
            "shipping_address_name": "Max Mustermann-Shipping",
            "items": [
                frappe._dict(against_sales_order="SAL-ORD-2026-01226"),
                frappe._dict(against_sales_order="SAL-ORD-2026-01226"),
                frappe._dict(against_sales_order=None),
            ],
        }
        values.update(overrides)
        return frappe._dict(values)

    def test_carrier_service_and_orders(self):
        service = frappe._dict(carrier="Österreichische Post", service_name="Paket Premium Select")
        with (
            mock.patch.object(ui.frappe.db, "get_value", return_value=service),
            mock.patch.object(ui, "_address_facts", return_value={"city": "Wien"}) as address_facts,
        ):
            facts = ui._delivery_note_shipping_facts(self._dn())
        address_facts.assert_called_once_with("Max Mustermann-Shipping")
        self.assertEqual(facts["carrier"], "Österreichische Post")
        self.assertEqual(facts["carrier_key"], "AUSTRIAN_POST")  # identity, not free text
        self.assertEqual(facts["service_name"], "Paket Premium Select")
        self.assertEqual(facts["sales_orders"], ["SAL-ORD-2026-01226"])
        self.assertEqual(facts["city"], "Wien")

    def test_delivery_note_without_carrier_service(self):
        with (
            mock.patch.object(ui.frappe.db, "get_value") as get_value,
            mock.patch.object(ui, "_address_facts", return_value={}),
        ):
            facts = ui._delivery_note_shipping_facts(self._dn(custom_carrier_service=None, items=[]))
        get_value.assert_not_called()
        self.assertIsNone(facts["carrier"])
        self.assertIsNone(facts["carrier_key"])
        self.assertEqual(facts["sales_orders"], [])
