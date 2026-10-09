# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Seed the two pickup-reminder Email Templates and link them in settings.

Created only when missing, never overwritten: the wording is business text,
and a phrasing polished in the Desk must survive the next migrate. Changing
the seed here does nothing to an existing template — ship a renamed one or a
follow-up patch for that.

The templates render with the context built in
``parcel/tracking/pickup_reminder.render_context``: ``doc`` (the Shipment),
``customer_name``, ``company``, ``orders`` (Sales Order names behind the parcel) and
``pickup`` (carrier, tracking_number, location, ready_since_formatted,
days_waiting). Deliberately no carrier tracking link and no event free text
(see the module docstring there).
"""

from __future__ import annotations

import frappe

TEMPLATE_DE = "Paket-Abholerinnerung"
TEMPLATE_EN = "Parcel Pickup Reminder (EN)"

_BODY = """<div style="font-family: sans-serif; max-width: 600px; margin: auto; color: #333;">
    <h2 style="color: #000; border-bottom: 1px solid #eee; padding-bottom: 10px;">%(heading)s</h2>
    <p>%(greeting)s {{ customer_name | e }},</p>
    <p>%(intro)s</p>
    <table style="border-collapse: collapse; margin: 15px 0;">
        <tr><td style="padding: 3px 12px 3px 0; color: #666;">%(label_carrier)s</td><td style="padding: 3px 0;">{{ pickup.carrier | e }}</td></tr>
        <tr><td style="padding: 3px 12px 3px 0; color: #666;">%(label_tracking)s</td><td style="padding: 3px 0;"><b>{{ pickup.tracking_number | e }}</b></td></tr>
        {%% if pickup.location %%}<tr><td style="padding: 3px 12px 3px 0; color: #666;">%(label_location)s</td><td style="padding: 3px 0;">{{ pickup.location | e }}</td></tr>{%% endif %%}
        <tr><td style="padding: 3px 12px 3px 0; color: #666;">%(label_since)s</td><td style="padding: 3px 0;">{{ pickup.ready_since_formatted }}</td></tr>
        {%% if orders %%}<tr><td style="padding: 3px 12px 3px 0; color: #666;">%(label_orders)s</td><td style="padding: 3px 0;">{{ orders | join(", ") | e }}</td></tr>{%% endif %%}
    </table>
    <p>%(deadline)s</p>
    <p style="font-size: 13px; color: #666; margin-top: 25px;">%(crossed)s</p>
    <hr style="margin: 30px 0; border: 0; border-top: 1px solid #eee;">
    <p style="font-size: 12px; color: #999; text-align: center;">
        {{ company | e }}<br>
        %(closing)s
    </p>
</div>"""


def template_definitions() -> tuple[tuple[str, str, str], ...]:
    """(Name, Betreff, HTML) beider Sprachen."""
    return (
        (
            TEMPLATE_DE,
            "Ihr Paket wartet auf Sie – {{ pickup.carrier }} {{ pickup.tracking_number }}",
            _BODY
            % {
                "heading": "Ihr Paket liegt zur Abholung bereit",
                "greeting": "Guten Tag",
                "intro": (
                    "Ihr Paket konnte nicht direkt zugestellt werden und liegt seit "
                    "{{ pickup.days_waiting }} Tagen zur Abholung bereit. Bitte holen Sie es "
                    "in den nächsten Tagen ab – nach Ablauf der Aufbewahrungsfrist geht es "
                    "sonst an uns zurück."
                ),
                "label_carrier": "Zusteller",
                "label_tracking": "Sendungsnummer",
                "label_location": "Abholstelle",
                "label_since": "Abholbereit seit",
                "label_orders": "Bestellung",
                "deadline": (
                    "Zum Abholen genügen in der Regel die Sendungsnummer und ein Lichtbildausweis."
                ),
                "crossed": (
                    "Falls Sie das Paket inzwischen abgeholt haben, betrachten Sie diese "
                    "E-Mail bitte als gegenstandslos."
                ),
                "closing": "Bei Fragen antworten Sie einfach auf diese E-Mail.",
            },
        ),
        (
            TEMPLATE_EN,
            "Your parcel is waiting for you – {{ pickup.carrier }} {{ pickup.tracking_number }}",
            _BODY
            % {
                "heading": "Your parcel is ready for collection",
                "greeting": "Hi",
                "intro": (
                    "Your parcel could not be delivered directly and has been ready for "
                    "collection for {{ pickup.days_waiting }} days. Please pick it up within "
                    "the next few days – once the holding period ends it is returned to us."
                ),
                "label_carrier": "Carrier",
                "label_tracking": "Tracking number",
                "label_location": "Pickup location",
                "label_since": "Ready since",
                "label_orders": "Order",
                "deadline": "The tracking number and a photo ID are usually all you need at the counter.",
                "crossed": "If you have collected the parcel in the meantime, please disregard this email.",
                "closing": "If you have any questions, reply to this email.",
            },
        ),
    )


def execute() -> None:
    ensure_email_templates()
    link_templates_in_settings()


def ensure_email_templates() -> None:
    for name, subject, html in template_definitions():
        if frappe.db.exists("Email Template", name):
            continue
        frappe.get_doc(
            {
                "doctype": "Email Template",
                "name": name,
                "subject": subject,
                "use_html": 1,
                "response_html": html,
            }
        ).insert(ignore_permissions=True)


def link_templates_in_settings() -> None:
    """Fill the empty template links; a deliberate choice in the Desk stays.

    Written field by field rather than through ``settings.save()``: saving
    validates the whole Single, and a site whose settings were never filled in
    (``default_carrier`` is mandatory) would fail the migrate on a field this
    patch has nothing to do with.
    """
    current = frappe.db.get_singles_dict("Parcel Station Settings")
    for field, value in (
        ("pickup_reminder_email_template", TEMPLATE_DE),
        ("pickup_reminder_email_template_en", TEMPLATE_EN),
        ("pickup_reminder_days", 2),
    ):
        if not current.get(field):
            frappe.db.set_single_value("Parcel Station Settings", field, value)
