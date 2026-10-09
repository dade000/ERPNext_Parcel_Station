# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the pickup reminder's selection and its one-mail-per-parcel promise.

Why this needs tests: every failure mode mails a customer wrongly or never.
A threshold off by one reminds people who collected this morning's parcel
yesterday; a clock that restarts on a re-delivered event never fires; a lost
marker mails the same customer every morning until the parcel is returned.
DB access is mocked (house style: no fixtures, SimpleNamespace fakes).
"""

from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.parcel.tracking import pickup_reminder as pr
from erpnext_parcel_station.parcel.tracking import status_mapping as sm

SETTINGS = frappe._dict(
    enabled=1, days=2, sender=None, email_template=None, email_template_en=None, cutoff_days=30
)


def _shipment(name="SHIPMENT-00123", **overrides):
    row = frappe._dict(
        name=name,
        awb_number="Z7M6ZVWB",
        carrier="GLS",
        carrier_service=None,
        custom_carrier_service=None,
        delivery_type=None,
        delivery_customer="Max Mustermann",
        delivery_contact_name="Max Mustermann-Max Mustermann",
        delivery_contact_email="kunde@example.com",
    )
    row.update(overrides)
    return row


def _event(shipment="SHIPMENT-00123", timestamp="2026-09-09 11:59:44", city="Bregenz-Schendlingen"):
    return frappe._dict(
        shipment=shipment,
        event_timestamp=timestamp,
        location_city=city,
        location_postal_code="6900",
        location_country="AT",
    )


class TestDueShipments(FrappeTestCase):
    def _due(self, shipments, events, today="2026-09-12"):
        with (
            mock.patch.object(pr, "_ready_for_pickup_shipments", return_value=shipments),
            mock.patch.object(pr.frappe, "get_all", return_value=events),
            mock.patch.object(pr, "nowdate", return_value=today),
            mock.patch.object(pr, "now_datetime", return_value=today + " 09:00:00"),
        ):
            return pr.due_shipments(SETTINGS)

    def test_strictly_more_than_threshold_days(self):
        # Deposited on the 9th: the 11th is exactly 2 days — not yet; the 12th is.
        self.assertEqual(self._due([_shipment()], [_event()], today="2026-09-11"), [])
        due = self._due([_shipment()], [_event()], today="2026-09-12")
        self.assertEqual([row.name for row in due], ["SHIPMENT-00123"])
        self.assertEqual(due[0].days_waiting, 3)
        self.assertEqual(due[0].pickup_location, "Bregenz-Schendlingen, AT")
        self.assertEqual(due[0].ready_since, "2026-09-09 11:59:44")

    def test_clock_starts_at_the_first_pickup_event(self):
        # GLS/FedEx re-deliver the full history; a later copy of the deposit
        # must not push the wait back under the threshold.
        events = [_event(timestamp="2026-09-08 09:00:00"), _event(timestamp="2026-09-11 09:00:00")]
        due = self._due([_shipment()], events, today="2026-09-11")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].ready_since, "2026-09-08 09:00:00")

    def test_status_without_event_is_not_due(self):
        self.assertEqual(self._due([_shipment()], []), [])

    def test_deposit_older_than_the_tracking_window_is_ignored(self):
        # Shipment creation is not the window — the deposit date is.
        self.assertEqual(self._due([_shipment()], [_event(timestamp="2026-07-01 10:00:00")]), [])

    def test_location_falls_back_to_postal_code(self):
        due = self._due([_shipment()], [_event(city="")], today="2026-09-20")
        self.assertEqual(due[0].pickup_location, "6900, AT")


class TestCandidateQuery(FrappeTestCase):
    def test_without_marker_field_nothing_is_selected(self):
        meta = SimpleNamespace(has_field=lambda field: False)
        with (
            mock.patch.object(pr.frappe, "get_meta", return_value=meta),
            mock.patch.object(pr.frappe, "get_all") as get_all,
            mock.patch.object(pr.frappe, "log_error") as log_error,
        ):
            self.assertEqual(pr._ready_for_pickup_shipments(), [])
        get_all.assert_not_called()
        log_error.assert_called_once()

    def test_query_filters_on_live_status_and_marker(self):
        meta = SimpleNamespace(has_field=lambda field: field == pr.SENT_AT_FIELD)
        with (
            mock.patch.object(pr.frappe, "get_meta", return_value=meta),
            mock.patch.object(pr.frappe, "get_all", return_value=[]) as get_all,
        ):
            pr._ready_for_pickup_shipments()
        filters = get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["tracking_status"], sm.STATUS_READY_FOR_PICKUP)
        self.assertEqual(filters["docstatus"], 1)
        self.assertEqual(filters[pr.SENT_AT_FIELD], ("is", "not set"))


class TestSendReminder(FrappeTestCase):
    def _row(self, **overrides):
        row = _shipment(**overrides)
        row.ready_since = "2026-09-09 11:59:44"
        row.days_waiting = 3
        row.pickup_location = "Bregenz-Schendlingen, AT"
        return row

    def _send(self, row, settings=SETTINGS, language="de"):
        context = {
            "doc": SimpleNamespace(name=row.name),
            "customer_name": "Max Mustermann",
            "company": "Musterfirma GmbH",
            "orders": ["SO-2026-00123"],
            "pickup": {
                "carrier": "GLS",
                "tracking_number": row.awb_number,
                "location": row.pickup_location,
                "ready_since": row.ready_since,
                "ready_since_formatted": "09.09.2026",
                "days_waiting": row.days_waiting,
            },
            "language": language,
        }
        with (
            mock.patch.object(pr, "_language", return_value=language),
            mock.patch.object(pr, "render_context", return_value=context),
            mock.patch.object(pr.frappe, "sendmail") as sendmail,
            mock.patch.object(pr.frappe.db, "set_value") as set_value,
            mock.patch.object(pr.frappe.db, "get_value", return_value=None),
        ):
            recipient = pr.send_reminder(row, settings)
        return recipient, sendmail, set_value

    def test_mails_contact_and_stamps_marker(self):
        recipient, sendmail, set_value = self._send(self._row())
        self.assertEqual(recipient, "kunde@example.com")
        kwargs = sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["kunde@example.com"])
        self.assertEqual(kwargs["reference_doctype"], "Shipment")
        self.assertEqual(kwargs["reference_name"], "SHIPMENT-00123")
        self.assertNotIn("sender", kwargs)  # default outgoing account
        self.assertIn("Z7M6ZVWB", kwargs["message"])
        self.assertIn("Bregenz-Schendlingen", kwargs["message"])
        self.assertIn("SO-2026-00123", kwargs["message"])
        # No carrier tracking link, ever — the customer journey stays on our pages.
        self.assertNotIn("http", kwargs["message"].lower())
        set_value.assert_called_once()
        self.assertEqual(set_value.call_args[0][:3], ("Shipment", "SHIPMENT-00123", pr.SENT_AT_FIELD))

    def test_configured_sender_is_used(self):
        settings = frappe._dict(SETTINGS, sender="info@example.com")
        _, sendmail, _ = self._send(self._row(), settings)
        self.assertEqual(sendmail.call_args.kwargs["sender"], "info@example.com")

    def test_english_fallback_for_english_customer(self):
        _, sendmail, _ = self._send(self._row(), language="en")
        self.assertIn("Your parcel", sendmail.call_args.kwargs["subject"])

    def test_no_address_means_no_mail_and_no_marker(self):
        row = self._row(delivery_contact_email=None, delivery_customer=None)
        recipient, sendmail, set_value = self._send(row)
        self.assertIsNone(recipient)
        sendmail.assert_not_called()
        set_value.assert_not_called()

    def test_template_is_preferred_over_fallback(self):
        settings = frappe._dict(SETTINGS, email_template="Paket-Abholerinnerung")
        rendered = {"subject": "Aus dem Template", "message": "<p>Z7M6ZVWB</p>"}
        with (
            mock.patch.object(pr.frappe.db, "exists", return_value=True),
            mock.patch(
                "frappe.email.doctype.email_template.email_template.get_email_template",
                return_value=rendered,
            ) as get_template,
        ):
            _, sendmail, _ = self._send(self._row(), settings)
        get_template.assert_called_once()
        self.assertEqual(get_template.call_args[0][0], "Paket-Abholerinnerung")
        self.assertEqual(sendmail.call_args.kwargs["subject"], "Aus dem Template")


class TestRecipientAndLanguage(FrappeTestCase):
    def test_contact_email_beats_customer_master(self):
        with mock.patch.object(pr.frappe.db, "get_value", return_value="master@example.com") as get_value:
            self.assertEqual(pr._recipient(_shipment()), "kunde@example.com")
            get_value.assert_not_called()
            self.assertEqual(pr._recipient(_shipment(delivery_contact_email="")), "master@example.com")

    def test_language_defaults_to_german(self):
        for stored, expected in (("de", "de"), ("de-AT", "de"), ("en", "en"), (None, "de"), ("fr", "en")):
            with mock.patch.object(pr.frappe.db, "get_value", return_value=stored):
                self.assertEqual(pr._language(_shipment()), expected, stored)


class TestRun(FrappeTestCase):
    def test_disabled_run_does_nothing(self):
        with (
            mock.patch.object(pr, "get_settings", return_value=frappe._dict(SETTINGS, enabled=0)),
            mock.patch.object(pr, "due_shipments") as due,
        ):
            self.assertIsNone(pr.send_pickup_reminders())
        due.assert_not_called()

    def test_one_failure_does_not_stop_the_run(self):
        rows = [_shipment("SHIPMENT-00001"), _shipment("SHIPMENT-00002")]
        with (
            mock.patch.object(pr, "get_settings", return_value=SETTINGS),
            mock.patch.object(pr, "due_shipments", return_value=rows),
            mock.patch.object(pr, "send_reminder", side_effect=[RuntimeError("smtp"), "x@example.com"]),
            mock.patch.object(pr, "_commit_if_available"),
            mock.patch.object(pr.frappe.db, "rollback"),
            mock.patch.object(pr.frappe, "log_error") as log_error,
        ):
            summary = pr.send_pickup_reminders()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], ["SHIPMENT-00002"])
        log_error.assert_called_once()
