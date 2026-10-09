# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the shipping notification's selection, grouping and marker promise.

Why this needs tests: every failure mode mails a customer wrongly or never.
A missing start timestamp mails the whole backlog on the day the switch is
turned on; a lost marker announces the same parcel twice a day; a broken
grouping key sends three mails for three parcels; and a tracking link built
without a token points every customer at the same dead page.
DB access is mocked (house style: no fixtures, SimpleNamespace fakes).
"""

from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.tracking import shipping_notification as sn

SETTINGS = frappe._dict(
    enabled=1,
    start="2026-10-01 08:00:00",
    sender=None,
    email_template=None,
    email_template_en=None,
    attach_delivery_note=1,
    print_format=None,
    tracking_page_url="https://shop.example.com/{language}/tracking/{token}",
)


def _shipment(name="SHIPMENT-00150", **overrides):
    row = frappe._dict(
        name=name,
        awb_number="1019012500001500269518",
        carrier="Österreichische Post",
        carrier_service=None,
        custom_carrier_service=None,
        delivery_type="Home Delivery",
        delivery_customer="Max Mustermann",
        delivery_contact_name="Max Mustermann-Max Mustermann",
        delivery_contact_email="kunde@example.com",
        delivery_address_name="Max Mustermann-Shipping",
    )
    row.update(overrides)
    return row


def _context(group, **overrides):
    context = {
        "doc": SimpleNamespace(name=group.shipments[0].name),
        "customer_name": "Max Mustermann",
        "company": "Musterfirma GmbH",
        "shipments": [
            {
                "name": row.name,
                "carrier": "Österreichische Post",
                "tracking_number": row.awb_number,
                "orders": ["SO-2026-00123"],
                "items": [{"item_code": "00005-23", "item_name": "Holzschuh <Rindfell>", "qty": 1, "uom": "Pair"}],
                "address_lines": ["Max Mustermann", "Beispielgasse 2", "1020 Wien", "Österreich"],
                "is_parcel_shop": False,
                "tracking_url": "https://shop.example.com/de/tracking/TOKEN",
            }
            for row in group.shipments
        ],
        "parcel_count": len(group.shipments),
        "orders": ["SO-2026-00123"],
        "tracking_links": [{"order": "SO-2026-00123", "url": "https://shop.example.com/de/tracking/TOKEN"}],
        "delivery_notes": ["MAT-DN-2026-00381"],
        "has_attachment": False,
        "language": group.language,
    }
    context.update(overrides)
    return context


class TestSettings(FrappeTestCase):
    def _settings(self, values):
        with mock.patch.object(sn.frappe.db, "get_singles_dict", return_value=values):
            return sn.get_settings()

    def test_attachment_defaults_to_on_when_never_saved(self):
        # A Check on a Single has no row until its first save; reading it as 0
        # would drop the Delivery Note from every mail.
        self.assertEqual(self._settings({}).attach_delivery_note, 1)
        self.assertEqual(self._settings({"shipping_notification_attach_delivery_note": "0"}).attach_delivery_note, 0)

    def test_disabled_and_unstarted_by_default(self):
        settings = self._settings({})
        self.assertEqual(settings.enabled, 0)
        self.assertIsNone(settings.start)
        self.assertIsNone(settings.tracking_page_url)


class TestCandidateQuery(FrappeTestCase):
    def test_without_start_nothing_is_selected(self):
        with mock.patch.object(sn.frappe, "get_all") as get_all:
            self.assertEqual(sn.pending_shipments(frappe._dict(SETTINGS, start=None)), [])
        get_all.assert_not_called()

    def test_without_marker_field_nothing_is_selected(self):
        meta = SimpleNamespace(has_field=lambda field: False)
        with (
            mock.patch.object(sn.frappe, "get_meta", return_value=meta),
            mock.patch.object(sn.frappe, "get_all") as get_all,
            mock.patch.object(sn.frappe, "log_error") as log_error,
        ):
            self.assertEqual(sn.pending_shipments(SETTINGS), [])
        get_all.assert_not_called()
        log_error.assert_called_once()

    def test_query_needs_tracking_number_marker_and_start(self):
        meta = SimpleNamespace(has_field=lambda field: field == sn.SENT_AT_FIELD)
        with (
            mock.patch.object(sn.frappe, "get_meta", return_value=meta),
            mock.patch.object(sn.frappe, "get_all", return_value=[]) as get_all,
        ):
            sn.pending_shipments(SETTINGS)
        filters = get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["docstatus"], 1)
        self.assertEqual(filters["awb_number"], ("is", "set"))
        self.assertEqual(filters["delivery_to_type"], "Customer")
        self.assertEqual(filters["creation"], (">=", "2026-10-01 08:00:00"))
        self.assertEqual(filters[sn.SENT_AT_FIELD], ("is", "not set"))


class TestGrouping(FrappeTestCase):
    def _groups(self, shipments):
        with mock.patch.object(sn, "_language", return_value="de"):
            return sn.group_by_recipient(shipments)

    def test_one_group_per_recipient_regardless_of_case(self):
        groups = self._groups(
            [
                _shipment("SHIPMENT-00001"),
                _shipment("SHIPMENT-00002", delivery_contact_email="other@example.com"),
                _shipment("SHIPMENT-00003", delivery_contact_email="KUNDE@Example.com "),
            ]
        )
        self.assertEqual(
            [[row.name for row in group.shipments] for group in groups],
            [["SHIPMENT-00001", "SHIPMENT-00003"], ["SHIPMENT-00002"]],
        )
        self.assertEqual(groups[0].recipient, "kunde@example.com")

    def test_shipment_without_address_is_left_out(self):
        row = _shipment(delivery_contact_email=None, delivery_customer=None)
        self.assertEqual(self._groups([row]), [])


class TestSendNotification(FrappeTestCase):
    def _group(self, *names, language="de"):
        names = names or ("SHIPMENT-00150",)
        return frappe._dict(
            recipient="kunde@example.com", language=language, shipments=[_shipment(name) for name in names]
        )

    def _send(self, group, settings=SETTINGS, attachments=None):
        with (
            mock.patch.object(sn, "render_context", return_value=_context(group)),
            mock.patch.object(sn, "delivery_note_attachments", return_value=attachments or []) as attach,
            mock.patch.object(sn.frappe, "sendmail") as sendmail,
            mock.patch.object(sn.frappe.db, "set_value") as set_value,
        ):
            sn.send_notification(group, settings)
        return sendmail, set_value, attach

    def test_one_mail_and_a_marker_per_shipment(self):
        sendmail, set_value, _ = self._send(self._group("SHIPMENT-00150", "SHIPMENT-00151"))
        sendmail.assert_called_once()
        kwargs = sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["kunde@example.com"])
        self.assertEqual(kwargs["reference_name"], "SHIPMENT-00150")
        self.assertNotIn("sender", kwargs)  # default outgoing account
        self.assertEqual(
            [call[0][:3] for call in set_value.call_args_list],
            [
                ("Shipment", "SHIPMENT-00150", sn.SENT_AT_FIELD),
                ("Shipment", "SHIPMENT-00151", sn.SENT_AT_FIELD),
            ],
        )

    def test_mail_carries_number_contents_and_our_link_only(self):
        sendmail, _, _ = self._send(self._group())
        message = sendmail.call_args.kwargs["message"]
        self.assertIn("1019012500001500269518", message)
        self.assertIn("SO-2026-00123", message)
        self.assertIn("https://shop.example.com/de/tracking/TOKEN", message)
        # Item names come from the checkout; they must not reach the mail raw.
        self.assertIn("Holzschuh &lt;Rindfell&gt;", message)
        # No link out to the carrier.
        for host in ("post.at", "gls-group", "fedex.com"):
            self.assertNotIn(host, message)

    def test_attachments_are_passed_and_optional(self):
        pdf = {"fname": "MAT-DN-2026-00381.pdf", "fcontent": b"%PDF"}
        sendmail, _, _ = self._send(self._group(), attachments=[pdf])
        self.assertEqual(sendmail.call_args.kwargs["attachments"], [pdf])

        sendmail, _, attach = self._send(self._group(), frappe._dict(SETTINGS, attach_delivery_note=0))
        attach.assert_not_called()
        self.assertNotIn("attachments", sendmail.call_args.kwargs)

    def test_failed_pdf_does_not_stop_the_mail(self):
        with (
            mock.patch.object(sn.frappe.db, "get_value", return_value="de"),
            mock.patch.object(sn.frappe, "attach_print", side_effect=RuntimeError("wkhtmltopdf")),
            mock.patch.object(sn.frappe, "log_error") as log_error,
        ):
            self.assertEqual(sn.delivery_note_attachments(["MAT-DN-2026-00381"], SETTINGS, "de"), [])
        log_error.assert_called_once()

    def test_no_marker_when_sending_fails(self):
        group = self._group()
        with (
            mock.patch.object(sn, "render_context", return_value=_context(group)),
            mock.patch.object(sn, "delivery_note_attachments", return_value=[]),
            mock.patch.object(sn.frappe, "sendmail", side_effect=RuntimeError("smtp")),
            mock.patch.object(sn.frappe.db, "set_value") as set_value,
        ):
            with self.assertRaises(RuntimeError):
                sn.send_notification(group, SETTINGS)
        set_value.assert_not_called()

    def test_english_fallback_and_configured_sender(self):
        sendmail, _, _ = self._send(self._group(language="en"), frappe._dict(SETTINGS, sender="info@example.com"))
        self.assertIn("Your order is on its way", sendmail.call_args.kwargs["subject"])
        self.assertEqual(sendmail.call_args.kwargs["sender"], "info@example.com")


class TestTrackingLink(FrappeTestCase):
    def test_token_is_created_once_and_reused(self):
        meta = SimpleNamespace(has_field=lambda field: field == sn.TOKEN_FIELD)
        with (
            mock.patch.object(sn.frappe, "get_meta", return_value=meta),
            mock.patch.object(sn.frappe.db, "exists", return_value=True),
            mock.patch.object(sn.frappe.db, "get_value", return_value=None),
            mock.patch.object(sn.frappe.db, "set_value") as set_value,
        ):
            token = sn.ensure_tracking_token("SO-2026-00123")
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(set_value.call_args[0][:4], ("Sales Order", "SO-2026-00123", sn.TOKEN_FIELD, token))

        with (
            mock.patch.object(sn.frappe, "get_meta", return_value=meta),
            mock.patch.object(sn.frappe.db, "exists", return_value=True),
            mock.patch.object(sn.frappe.db, "get_value", return_value="EXISTING-TOKEN"),
            mock.patch.object(sn.frappe.db, "set_value") as set_value,
        ):
            self.assertEqual(sn.ensure_tracking_token("SO-2026-00123"), "EXISTING-TOKEN")
        set_value.assert_not_called()

    def test_url_fills_token_and_language(self):
        with mock.patch.object(sn, "ensure_tracking_token", return_value="TOKEN"):
            self.assertEqual(
                sn.tracking_page_url("SO-2026-00123", "en", SETTINGS),
                "https://shop.example.com/en/tracking/TOKEN",
            )

    def test_no_link_without_configured_page_or_token(self):
        with mock.patch.object(sn, "ensure_tracking_token", return_value="TOKEN") as ensure:
            self.assertIsNone(sn.tracking_page_url("SO-1", "de", frappe._dict(SETTINGS, tracking_page_url=None)))
            # A URL without the placeholder would send every customer to the same page.
            self.assertIsNone(
                sn.tracking_page_url("SO-1", "de", frappe._dict(SETTINGS, tracking_page_url="https://shop.example.com/tracking"))
            )
        ensure.assert_not_called()
        with mock.patch.object(sn, "ensure_tracking_token", return_value=None):
            self.assertIsNone(sn.tracking_page_url("SO-1", "de", SETTINGS))


class TestParcelItems(FrappeTestCase):
    def test_charges_are_dropped_but_bundles_stay(self):
        lines = [
            frappe._dict(item_code="00011", item_name="Versandkosten", qty=1, uom="Nos"),
            frappe._dict(item_code="00005-23", item_name="Holzschuh", qty=1, uom="Pair"),
            frappe._dict(item_code="00005-23", item_name="Holzschuh", qty=2, uom="Pair"),
            frappe._dict(item_code="SOCKEN-5ER", item_name="Socken 5er", qty=1.5, uom="Nos"),
        ]

        def get_all(doctype, **kwargs):
            if doctype == "Delivery Note Item":
                return lines
            if doctype == "Item":
                return ["00011", "SOCKEN-5ER"]  # non-stock
            if doctype == "Product Bundle":
                return ["SOCKEN-5ER"]
            raise AssertionError(doctype)

        with (
            mock.patch.object(sn.frappe.db, "table_exists", return_value=False),
            mock.patch.object(sn.frappe, "get_all", side_effect=get_all),
        ):
            items = sn.parcel_items("SHIPMENT-00150", ["MAT-DN-2026-00381"])
        self.assertEqual(
            [(item["item_code"], item["qty"]) for item in items],
            [("00005-23", 3), ("SOCKEN-5ER", 1.5)],
        )

    def test_scanned_rows_win_over_the_delivery_note(self):
        scanned = [frappe._dict(item_code="00005-23", item_name="Holzschuh", qty=1, uom="Pair")]
        with (
            mock.patch.object(sn.frappe.db, "table_exists", return_value=True),
            mock.patch.object(sn.frappe.db, "sql", return_value=scanned),
            mock.patch.object(sn.frappe, "get_all") as get_all,
        ):
            items = sn.parcel_items("SHIPMENT-00150", ["MAT-DN-2026-00381"])
        get_all.assert_not_called()
        self.assertEqual(items[0]["qty"], 1)


class TestRun(FrappeTestCase):
    def test_disabled_run_does_nothing(self):
        with (
            mock.patch.object(sn, "get_settings", return_value=frappe._dict(SETTINGS, enabled=0)),
            mock.patch.object(sn, "pending_shipments") as pending,
        ):
            self.assertIsNone(sn.send_shipping_notifications())
        pending.assert_not_called()

    def test_enabled_without_start_starts_the_clock_and_sends_nothing(self):
        with (
            mock.patch.object(sn, "get_settings", return_value=frappe._dict(SETTINGS, start=None)),
            mock.patch.object(sn.frappe.db, "set_single_value") as set_single_value,
            mock.patch.object(sn, "_commit_if_available"),
            mock.patch.object(sn, "pending_shipments") as pending,
        ):
            self.assertIsNone(sn.send_shipping_notifications())
        pending.assert_not_called()
        self.assertEqual(
            set_single_value.call_args[0][:2], (sn.SETTINGS_DOCTYPE, "shipping_notification_start")
        )

    def test_one_failure_does_not_stop_the_run(self):
        rows = [
            _shipment("SHIPMENT-00001", delivery_contact_email="a@example.com"),
            _shipment("SHIPMENT-00002", delivery_contact_email="b@example.com"),
            _shipment("SHIPMENT-00003", delivery_contact_email=None, delivery_customer=None),
        ]
        with (
            mock.patch.object(sn, "get_settings", return_value=SETTINGS),
            mock.patch.object(sn, "pending_shipments", return_value=rows),
            mock.patch.object(sn, "_language", return_value="de"),
            mock.patch.object(sn, "send_notification", side_effect=[RuntimeError("smtp"), "b@example.com"]),
            mock.patch.object(sn, "_commit_if_available"),
            mock.patch.object(sn.frappe.db, "rollback"),
            mock.patch.object(sn.frappe, "log_error") as log_error,
        ):
            summary = sn.send_shipping_notifications()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], ["SHIPMENT-00002"])
        self.assertEqual(summary["skipped"], ["SHIPMENT-00003"])
        log_error.assert_called_once()
