"""Who signed for the delivery has to be a person.

The driver app's Receiver Name was free text, so whatever was typed became the
proof of delivery — in practice the store or location name, which says nothing
about who took custody.

The people who can receive are already known: they are the ones the delivery
OTP is sent to. request_delivery_otp now returns them so the field can offer
them. It stays an Autocomplete rather than a Select because someone else
genuinely can be at the counter, and refusing that would strand the driver —
the same shape carrier POD apps use.
"""

import inspect
import pathlib
import unittest

import frappe

from ch_logistics.api import transfer_manifest_api as api


class TestReceiverCandidates(unittest.TestCase):
    def tearDown(self):
        frappe.db.rollback()

    def test_the_otp_response_carries_the_candidate_list(self):
        src = inspect.getsource(api.request_delivery_otp)
        self.assertIn('"receiver_options"', src)
        self.assertIn("_receiver_candidates(doc)", src)

    def test_candidates_are_the_same_people_the_code_goes_to(self):
        src = inspect.getsource(api._receiver_candidates)
        self.assertIn("_collect_store_manager_contacts", src)

    def test_a_missing_roster_never_blocks_a_delivery(self):
        """A destination with no mapped contacts must still be deliverable."""
        src = inspect.getsource(api._receiver_candidates)
        self.assertIn("except Exception", src)
        doc = frappe._dict({"destination_store": None})
        self.assertEqual(api._receiver_candidates(doc), [])

    def test_it_returns_real_names_for_a_staffed_store(self):
        store = frappe.db.get_value(
            "POS Executive", {"is_active": 1}, "store")
        if not store:
            self.skipTest("no active POS Executive on this site")
        names = api._receiver_candidates(frappe._dict({"destination_store": store}))
        self.assertTrue(names, f"{store} has active executives but offered no receiver")
        for name in names:
            self.assertIsInstance(name, str)
            self.assertTrue(name.strip())

    def test_the_counter_roster_is_used_when_the_manager_gate_is_unset(self):
        """material_request_approval_roles is empty on this estate, so the
        manager lookup resolves nobody. The POS Executive roster is what keeps
        the picker useful — without it the field is blank everywhere."""
        src = inspect.getsource(api._receiver_candidates)
        self.assertIn('"POS Executive"', src)
        self.assertIn('"is_active": 1', src)

    def test_administrator_is_not_offered_as_a_receiver(self):
        src = inspect.getsource(api._receiver_candidates)
        self.assertIn('"Administrator"', src)
        for store in frappe.get_all("CH Store", filters={"disabled": 0}, pluck="name", limit=25):
            self.assertNotIn(
                "Administrator",
                api._receiver_candidates(frappe._dict({"destination_store": store})))

    def test_the_field_is_an_autocomplete_not_free_text(self):
        js = pathlib.Path(frappe.get_app_path(
            "ch_logistics", "logistics", "page", "delivery_app", "delivery_app.js")).read_text()
        block = js[js.index('fieldname: "receiver_name"'):]
        block = block[:block.index("},")]
        self.assertIn('fieldtype: "Autocomplete"', block,
                      "a Select would refuse a receiver who is not on the roster")
        self.assertIn("receiver_options", block)
