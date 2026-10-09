"""Child doctype for ``Carrier Service.allowed_countries``.

Each row links a Carrier Service to one Country it ships to. The webshop
checkout filters Shipping Rules by the delivery country, only showing rules
whose Carrier Service's ``allowed_countries`` includes that country. An empty
``allowed_countries`` table means the Carrier Service ships everywhere.
"""
from frappe.model.document import Document


class CarrierServiceCountry(Document):
    pass
