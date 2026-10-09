# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Seed the two shipping-notification Email Templates and link them in settings.

Created only when missing, never overwritten: the wording is business text,
and a phrasing polished in the Desk must survive the next migrate. Changing
the seed here does nothing to an existing template — ship a renamed one or a
follow-up patch for that.

The templates render with the context built in
``parcel/tracking/shipping_notification.render_context``: ``customer_name``,
``company``, ``orders``, ``parcel_count``, ``tracking_links`` and
``shipments`` — one entry per parcel with ``carrier``, ``tracking_number``,
``orders``, ``items`` (``item_name``, ``qty``), ``address_lines``,
``is_parcel_shop`` and ``tracking_url`` — plus ``has_attachment``. The link goes to our own tracking
page; there is deliberately no carrier link in the mail.
"""

from __future__ import annotations

import frappe

TEMPLATE_DE = "Versandbenachrichtigung"
TEMPLATE_EN = "Shipping Notification (EN)"

_LABEL = 'style="padding: 3px 12px 3px 0; color: #666; vertical-align: top;"'
_VALUE = 'style="padding: 3px 0; vertical-align: top;"'

_BODY = (
    """<div style="font-family: sans-serif; max-width: 600px; margin: auto; color: #333;">
    <h2 style="color: #000; border-bottom: 1px solid #eee; padding-bottom: 10px;">%(heading)s</h2>
    <p>%(greeting)s {{ customer_name | e }},</p>
    <p>{%% if parcel_count > 1 %%}%(intro_many)s{%% else %%}%(intro_one)s{%% endif %%}</p>
    {%% for shipment in shipments %%}
    <div style="border: 1px solid #eee; border-radius: 6px; padding: 14px 16px; margin: 18px 0;">
        {%% if parcel_count > 1 %%}<p style="margin: 0 0 8px 0; font-weight: bold;">%(parcel)s {{ loop.index }}</p>{%% endif %%}
        <table style="border-collapse: collapse;">
            <tr><td __LABEL__>%(label_carrier)s</td><td __VALUE__>{{ shipment.carrier | e }}</td></tr>
            <tr><td __LABEL__>%(label_tracking)s</td><td __VALUE__><b>{{ shipment.tracking_number | e }}</b></td></tr>
            {%% if shipment.orders %%}<tr><td __LABEL__>%(label_orders)s</td><td __VALUE__>{{ shipment.orders | join(", ") | e }}</td></tr>{%% endif %%}
            {%% if shipment["items"] %%}<tr><td __LABEL__>%(label_items)s</td><td __VALUE__>{%% for item in shipment["items"] %%}{{ item.qty }} × {{ item.item_name | e }}{%% if not loop.last %%}<br>{%% endif %%}{%% endfor %%}</td></tr>{%% endif %%}
            {%% if shipment.address_lines %%}<tr><td __LABEL__>{%% if shipment.is_parcel_shop %%}%(label_shop)s{%% else %%}%(label_address)s{%% endif %%}</td><td __VALUE__>{%% for line in shipment.address_lines %%}{{ line | e }}{%% if not loop.last %%}<br>{%% endif %%}{%% endfor %%}</td></tr>{%% endif %%}
        </table>
        {%% if shipment.tracking_url %%}
        <p style="margin: 14px 0 2px 0;">
            <a href="{{ shipment.tracking_url | e }}" style="display: inline-block; background: #333; color: #fff; text-decoration: none; padding: 9px 18px; border-radius: 4px;">%(track)s</a>
        </p>
        {%% endif %%}
    </div>
    {%% endfor %%}
    {%% if has_attachment %%}<p>%(attachment)s</p>{%% endif %%}
    <hr style="margin: 30px 0; border: 0; border-top: 1px solid #eee;">
    <p style="font-size: 12px; color: #999; text-align: center;">
        {{ company | e }}<br>
        %(closing)s
    </p>
</div>""".replace("__LABEL__", _LABEL).replace("__VALUE__", _VALUE)
)


def template_definitions() -> tuple[tuple[str, str, str], ...]:
    """(Name, Betreff, HTML) beider Sprachen."""
    return (
        (
            TEMPLATE_DE,
            "Ihre Bestellung ist unterwegs{% if orders %} – {{ orders | join(', ') }}{% endif %}",
            _BODY
            % {
                "heading": "Ihre Bestellung ist unterwegs",
                "greeting": "Guten Tag",
                "intro_one": "wir haben Ihr Paket an den Zusteller übergeben.",
                "intro_many": "wir haben {{ parcel_count }} Pakete für Sie an den Zusteller übergeben.",
                "parcel": "Paket",
                "label_carrier": "Zusteller",
                "label_tracking": "Sendungsnummer",
                "label_orders": "Bestellung",
                "label_items": "Inhalt",
                "label_address": "Lieferadresse",
                "label_shop": "Paketshop",
                "track": "Sendung verfolgen",
                "attachment": "Den Lieferschein finden Sie im Anhang dieser E-Mail.",
                "closing": "Bei Fragen antworten Sie einfach auf diese E-Mail.",
            },
        ),
        (
            TEMPLATE_EN,
            "Your order is on its way{% if orders %} – {{ orders | join(', ') }}{% endif %}",
            _BODY
            % {
                "heading": "Your order is on its way",
                "greeting": "Hi",
                "intro_one": "we have handed your parcel over to the carrier.",
                "intro_many": "we have handed {{ parcel_count }} parcels for you over to the carrier.",
                "parcel": "Parcel",
                "label_carrier": "Carrier",
                "label_tracking": "Tracking number",
                "label_orders": "Order",
                "label_items": "Contents",
                "label_address": "Delivery address",
                "label_shop": "Parcel shop",
                "track": "Track your parcel",
                "attachment": "You will find the delivery note attached to this email.",
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
    """Fill the empty settings; a deliberate choice in the Desk stays.

    Written field by field rather than through ``settings.save()``: saving
    validates the whole Single, and a site whose settings were never filled in
    (``default_carrier`` is mandatory) would fail the migrate on a field this
    patch has nothing to do with. The switch itself is not touched — the
    notification stays off until someone turns it on.
    """
    current = frappe.db.get_singles_dict("Parcel Station Settings")
    for field, value in (
        ("shipping_notification_email_template", TEMPLATE_DE),
        ("shipping_notification_email_template_en", TEMPLATE_EN),
    ):
        if not current.get(field):
            frappe.db.set_single_value("Parcel Station Settings", field, value)
    # A Check on a Single has no row until it is written once; without one the
    # form shows the default but readers see 0.
    if current.get("shipping_notification_attach_delivery_note") is None:
        frappe.db.set_single_value("Parcel Station Settings", "shipping_notification_attach_delivery_note", 1)
