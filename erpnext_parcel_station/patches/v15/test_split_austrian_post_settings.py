# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Guards the Austrian Post settings split — the patch that moved the carrier's
config out of Parcel Station Settings.

Why this exists: the first version of this patch reached production and
crashed there while passing everywhere else, because both of its bugs are
invisible unless a field actually holds a value:

* ``frappe.db.get_value("Singles", ...)`` appends a default ``ORDER BY
  modified``, and ``tabSingles`` has no ``modified`` column — the query dies
  with ``Unknown column 'modified' in 'ORDER BY'``. The call sat behind an
  ``if value in (None, ""): continue`` guard, so an environment with no
  Austrian Post configuration never reached it and reported success. Only the
  live site had values to copy, and its migration aborted mid-run.
* ``frappe.db.get_single_value`` casts through the fieldtype, so a Check/Int
  field with no stored row returns 0 rather than None. Used as an "already
  configured?" guard it silently skips migrating an enabled flag.

Both are therefore exercised here against seeded rows, with a Check and an Int
field among them. FrappeTestCase rolls the transaction back per class, and the
patch itself never commits, so the site's own settings are untouched.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_parcel_station.patches.v15 import split_austrian_post_settings as patch


class TestSplitAustrianPostSettings(FrappeTestCase):
    def setUp(self):
        super().setUp()
        for doctype in (patch.OLD, patch.NEW):
            frappe.db.delete("Singles", {"doctype": doctype, "field": ("in", list(patch.FIELDS))})

    def _seed_old(self, **values):
        rows = [(patch.OLD, field, value) for field, value in values.items()]
        frappe.qb.into("Singles").columns("doctype", "field", "value").insert(*rows).run()

    def _new_values(self):
        return frappe.db.get_singles_dict(patch.NEW)

    def test_configured_values_are_carried_over(self):
        # The production case: real values present, target empty. The first
        # version of the patch raised a SQL error on the very first field.
        self._seed_old(
            client_id="21181807",
            org_unit_id="1400024",
            endpoint_url="https://abn-plc.post.at/DataService/x.svc/secure",
            enable_austrian_post_integration="1",
            customs_option_id="0",
        )
        patch.execute()

        migrated = self._new_values()
        self.assertEqual(migrated.get("client_id"), "21181807")
        self.assertEqual(migrated.get("org_unit_id"), "1400024")
        self.assertEqual(migrated.get("endpoint_url"), "https://abn-plc.post.at/DataService/x.svc/secure")
        # Check/Int fields: absent from the target means "not configured", not 0.
        self.assertEqual(migrated.get("enable_austrian_post_integration"), "1")
        self.assertEqual(migrated.get("customs_option_id"), "0")

    def test_old_rows_are_removed(self):
        self._seed_old(client_id="21181807", enable_austrian_post_integration="1")
        patch.execute()

        left_behind = frappe.db.get_singles_dict(patch.OLD)
        self.assertNotIn("client_id", left_behind)
        self.assertNotIn("enable_austrian_post_integration", left_behind)

    def test_existing_target_values_are_not_overwritten(self):
        self._seed_old(client_id="OLD-ID")
        frappe.db.set_value(patch.NEW, patch.NEW, "client_id", "ALREADY-SET", update_modified=False)

        patch.execute()

        self.assertEqual(self._new_values().get("client_id"), "ALREADY-SET")

    def test_rerun_is_idempotent(self):
        # The failed production run left the patch out of the Patch Log, so it
        # re-runs on the next migrate — twice must be as good as once.
        self._seed_old(client_id="21181807")
        patch.execute()
        patch.execute()

        self.assertEqual(self._new_values().get("client_id"), "21181807")

    def test_nothing_configured_is_a_no_op(self):
        # The staging case, which is exactly why the bug slipped through.
        patch.execute()

        self.assertEqual(self._new_values(), {})
