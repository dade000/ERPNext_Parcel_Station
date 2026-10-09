"""Fill Carrier Service.carrier_key on existing records.

The field carries the resolved carrier identity (GLS / AUSTRIAN_POST / FEDEX)
and drives which settings the form shows: packaging type, duties payment and the
import-charge fields only appear for a carrier whose payload actually carries
them. It is written by the Carrier Service validate hook, so a record that is
never opened and saved would keep an empty key — and its carrier-specific
settings would stay hidden even though they apply.

Resolution goes through carrier_routing.carrier_from_name, the same helper label
dispatch uses, so a master named "Österreichische Post" folds to AUSTRIAN_POST
rather than failing on the umlaut.

Written with a direct db update rather than doc.save(): saving would re-run
validation on records that predate the current rules, and a rejected save in a
patch aborts the whole migrate.
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "Carrier Service"):
        return
    if not frappe.get_meta("Carrier Service").has_field("carrier_key"):
        return

    from erpnext_parcel_station.parcel.carrier_routing import carrier_from_name

    filled = 0
    for row in frappe.get_all("Carrier Service", fields=["name", "carrier", "carrier_key"]):
        key = carrier_from_name(row.carrier) or ""
        if key == (row.carrier_key or ""):
            continue
        frappe.db.set_value("Carrier Service", row.name, "carrier_key", key, update_modified=False)
        filled += 1

    if filled:
        frappe.clear_cache(doctype="Carrier Service")
        frappe.logger().info(f"[patch] carrier_key filled on {filled} Carrier Service record(s)")
