# Copyright (c) 2025, Usama and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import now_datetime


class ParcelStationSettings(Document):
	def validate(self):
		self.set_shipping_notification_start()

	def set_shipping_notification_start(self):
		"""Switching the shipping notification on starts the clock: only
		Shipments created from now on are mailed, never the backlog. A date
		entered by hand stays."""
		if self.get("shipping_notification_enabled") and not self.get("shipping_notification_start"):
			self.shipping_notification_start = now_datetime()
