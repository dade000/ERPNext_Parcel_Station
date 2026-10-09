# Copyright (c) 2026, Devich Daniel and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class FedExSettings(Document):
    def validate(self):
        # Terms of sale lands on the commercial invoice and FedEx caps it at 3
        # characters; a longer value is rejected at shipment time, i.e. far away
        # from where it was typed.
        terms = (self.terms_of_sale or "").strip()
        if len(terms) > 3:
            frappe.throw(_("Terms of Sale must be at most 3 characters (e.g. DAP, DDP, FCA)."))
        self.terms_of_sale = terms.upper()

        for fieldname in ("sandbox_account_number", "production_account_number"):
            account = (self.get(fieldname) or "").strip()
            if account and not account.isdigit():
                frappe.throw(_("{0} must contain digits only.").format(_(fieldname)))
            self.set(fieldname, account)

        if self.enable_fedex_integration:
            # Only the ACTIVE environment has to be complete; the other set may
            # legitimately be empty (e.g. production credentials not issued yet).
            mode = (self.api_mode or "Sandbox").strip()
            prefix = "production" if mode == "Production" else "sandbox"
            missing = [
                label
                for label, value in (
                    (_("API Key"), self.get(f"{prefix}_client_key")),
                    (
                        _("Secret Key"),
                        self.get_password(f"{prefix}_client_secret", raise_exception=False),
                    ),
                    (_("Account Number"), self.get(f"{prefix}_account_number")),
                )
                if not (value or "").strip()
            ]
            if missing:
                frappe.throw(
                    _("Cannot enable the FedEx integration in {0} mode without: {1}").format(
                        mode, ", ".join(missing)
                    )
                )
