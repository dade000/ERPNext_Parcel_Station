# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Which GLS services go out per delivery type.

Home deliveries are booked with FlexDeliveryService (recipient gets notified
and can reschedule/redirect). Parcel-shop deliveries must not carry it — the
parcel is going to a shop, not to the recipient's door.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from . import gls_api


def _doc(delivery_type):
    return SimpleNamespace(doctype="Shipment", delivery_type=delivery_type, get=lambda *_: None)


_CS = SimpleNamespace(name="GLS-PARCEL", service_code="PARCEL")


class TestGLSServices(FrappeTestCase):
    def test_home_delivery_books_flex_delivery(self):
        services = gls_api._build_services(_doc("Home Delivery"), _CS, receiver_country_code="AT")
        self.assertEqual(services, [{"Service": {"ServiceName": "service_flexdelivery"}}])

    def test_home_delivery_ignores_a_shop_delivery_service_code(self):
        services = gls_api._build_services(
            _doc("Home Delivery"), _CS, service_code_override="service_shopdelivery",
            receiver_country_code="AT",
        )
        self.assertEqual(services, [{"Service": {"ServiceName": "service_flexdelivery"}}])

    def test_parcelshop_delivery_has_no_flex_delivery(self):
        with patch.object(gls_api, "_canonical_shipping_address_name", return_value="ADDR-1"), \
             patch.object(gls_api, "_parcel_shop_id_from_canonical_address", return_value="GLS_AT-1"):
            services = gls_api._build_services(_doc("Parcelshop Delivery"), _CS, receiver_country_code="AT")
        self.assertEqual(
            services,
            [{"ShopDelivery": {"ServiceName": "service_shopdelivery", "ParcelShopID": "GLS_AT-1"}}],
        )

    def test_flex_delivery_code_uses_the_generic_service_element(self):
        # Shipit has no FlexDeliveryService element; it 400s on that key.
        self.assertEqual(gls_api._map_service_code_to_type("service_flexdelivery"), "Service")
