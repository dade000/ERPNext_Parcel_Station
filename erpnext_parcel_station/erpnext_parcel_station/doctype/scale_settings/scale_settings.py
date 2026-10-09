from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_url


class ScaleSettings(Document):
    def validate(self):
        url = (self.scale_url or "").strip()
        if url and not validate_url(url, valid_schemes=("http", "https")):
            frappe.throw(_("Scale URL must be a valid http(s) URL."))
        self.scale_url = url
