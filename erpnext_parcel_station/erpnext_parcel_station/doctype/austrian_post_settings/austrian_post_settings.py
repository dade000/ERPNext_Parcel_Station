# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
from frappe.model.document import Document


class AustrianPostSettings(Document):
    """All Austrian Post configuration, mirroring the GLS/FedEx Settings
    pattern: SOAP label API (endpoint, SOAPActions, ClientID/Org Unit) and the
    tracking-file SFTP access. Split out of Parcel Station Settings by
    ``patches/v15/split_austrian_post_settings.py`` — Parcel Station Settings
    keeps only the carrier-independent config (general, sender, tracking
    monitoring).
    """
