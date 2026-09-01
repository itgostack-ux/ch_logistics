# Copyright (c) 2026, Congruence Holdings and contributors
"""C3 go-live blocker proofs — CH Transfer Manifest ↔ Stock Entry integrity.

The Sep-01 go-live audit found 9 Stock Entries left at docstatus=0 with live
Stock Ledger Entries posted against them: `_auto_submit_stock_entries` posted
SLEs seconds into a submit that crashed before the voucher docstatus advanced,
and the surrounding transaction later committed the partial write. It also
found TM-2026-00004 sitting at status "Delivered" with a Draft Stock Entry —
"Delivered" claiming a physical handover the ledger never recorded.

These tests pin the repaired contracts:

* a failed auto-submit strands ZERO ledger rows (per-SE savepoint rollback),
  and the failure lands on the manifest timeline instead of only Error Log;
* the transition INTO "Delivered" is refused while any linked Stock Entry is
  still Draft — through validate() for draft manifests and
  before_update_after_submit() for submitted ones;
* an invalid Stock Entry row no longer aborts _populate_transfer_details
  mid-loop: every bad row is reported in one aggregated refusal.

All fixtures are created in-test and discarded by frappe.db.rollback() in
tearDown — nothing is committed.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt, nowdate


class TestManifestSubmitAtomicity(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")
        # Swallowed frappe.throw/msgprint calls leave message_log residue
        # that would spam popups into whatever request runs next.
        frappe.clear_messages()
        frappe.db.rollback()

    # ── Fixture builders (all rolled back) ─────────────────────────────

    def _company(self):
        company = frappe.db.get_value("Company", {}, "name")
        if not company:
            self.skipTest("No Company is available on this site.")
        return company

    def _warehouse(self, company, label, suffix):
        doc = frappe.new_doc("Warehouse")
        doc.warehouse_name = f"C3-{label}-{suffix}"
        doc.company = company
        doc.is_group = 0
        doc.insert(ignore_permissions=True)
        return doc.name

    def _item(self, suffix):
        item = frappe.new_doc("Item")
        item.item_code = f"C3-ITEM-{suffix}"
        item.item_name = item.item_code
        item.item_group = frappe.db.get_value(
            "Item Group", {"is_group": 0}, "name"
        ) or "Products"
        item.stock_uom = "Nos"
        item.is_stock_item = 1
        if item.meta.has_field("ch_lifecycle_status"):
            item.ch_lifecycle_status = "Active"
        if item.meta.has_field("gst_hsn_code"):
            item.gst_hsn_code = frappe.db.get_value("GST HSN Code", {}, "name")
        if item.meta.has_field("ch_item_mrp"):
            item.ch_item_mrp = 500  # ch_item_master: MRP mandatory on stock items
        if item.meta.has_field("ch_plm_status"):
            # PLM default "NPI" blocks Stock Entries entirely.
            item.ch_plm_status = "Active Production"
        if item.meta.has_field("ch_sub_category"):
            # ch_item_master governance: an Active item needs a sub-category
            # (and its parent category); borrow any goods-nature one already
            # on the site.
            sub_category = frappe.db.get_value(
                "CH Sub Category",
                {
                    "item_nature": ["not in", ["Service", "Subscription", "Asset / Capital"]],
                    "disabled": 0,
                },
                ["name", "category"],
                as_dict=True,
            )
            if sub_category:
                item.ch_sub_category = sub_category.name
                if item.meta.has_field("ch_category"):
                    item.ch_category = sub_category.category
        item.insert(ignore_permissions=True)
        return item.item_code

    def _build_world(self, receipt_qty=5, transfer_qty=2):
        """Item with real stock at a fresh source warehouse, plus a Draft
        Material Transfer SE wrapped in a Draft manifest — the exact state
        accept_delivery's auto-submit operates on."""
        company = self._company()
        suffix = frappe.generate_hash(length=6).upper()
        source = self._warehouse(company, "SRC", suffix)
        destination = self._warehouse(company, "DST", suffix)
        item_code = self._item(suffix)

        receipt = frappe.new_doc("Stock Entry")
        receipt.stock_entry_type = "Material Receipt"
        receipt.company = company
        receipt.to_warehouse = source
        receipt.append("items", {
            "item_code": item_code,
            "qty": receipt_qty,
            "basic_rate": 100,
            "t_warehouse": source,
        })
        receipt.insert(ignore_permissions=True)
        receipt.submit()

        transfer = frappe.new_doc("Stock Entry")
        transfer.stock_entry_type = "Material Transfer"
        transfer.company = company
        transfer.from_warehouse = source
        transfer.to_warehouse = destination
        transfer.append("items", {
            "item_code": item_code,
            "qty": transfer_qty,
            "basic_rate": 100,
            "uom": "Nos",
            "s_warehouse": source,
            "t_warehouse": destination,
        })
        transfer.insert(ignore_permissions=True)

        manifest = frappe.get_doc({
            "doctype": "CH Transfer Manifest",
            "manifest_date": nowdate(),
            "company": company,
            "transfers": [{"stock_entry": transfer.name}],
        })
        manifest.flags.ignore_permissions = True
        manifest.flags.ignore_mandatory = True
        manifest.insert(ignore_permissions=True, ignore_mandatory=True)
        for row in manifest.transfers:
            row.custom_received_qty = flt(row.total_qty)
        return manifest, transfer

    @staticmethod
    def _live_sle_count(voucher_no):
        return frappe.db.count(
            "Stock Ledger Entry",
            {"voucher_no": voucher_no, "is_cancelled": 0},
        )

    # ── (a)+(b): atomic auto-submit that surfaces its failures ─────────

    def test_failed_auto_submit_strands_zero_ledger_rows(self):
        manifest, transfer = self._build_world()
        se_class = type(frappe.get_doc("Stock Entry", transfer.name))
        real_submit = se_class.submit

        def exploding_submit(doc_self, *args, **kwargs):
            # Run the REAL submit so genuine SLE/GL rows exist, then crash —
            # reproducing the estate wreck where the submit chain died after
            # ledger posting but before the request settled.
            real_submit(doc_self, *args, **kwargs)
            raise RuntimeError("simulated crash after ledger posting")

        with patch.object(se_class, "submit", exploding_submit):
            # Must not raise: failures surface on the manifest, they no
            # longer abort (and roll back) the whole acceptance.
            manifest._auto_submit_stock_entries({})

        self.assertEqual(
            self._live_sle_count(transfer.name), 0,
            "a failed submit must leave zero Stock Ledger Entry rows",
        )
        self.assertEqual(
            frappe.db.get_value("Stock Entry", transfer.name, "docstatus"), 0,
            "the Stock Entry must remain Draft after the rollback",
        )
        failure_comments = frappe.get_all(
            "Comment",
            filters={
                "reference_doctype": "CH Transfer Manifest",
                "reference_name": manifest.name,
                "content": ["like", "%Auto-submit FAILED%"],
            },
        )
        self.assertTrue(
            failure_comments,
            "the failure must be recorded on the manifest timeline, "
            "not only in Error Log",
        )

    def test_successful_auto_submit_still_posts_ledger(self):
        # Positive control: the savepoint wrapper must not disturb a clean
        # submit — the SE ends submitted with real ledger rows.
        manifest, transfer = self._build_world()
        manifest._auto_submit_stock_entries({})
        self.assertEqual(
            frappe.db.get_value("Stock Entry", transfer.name, "docstatus"), 1
        )
        self.assertGreater(self._live_sle_count(transfer.name), 0)

    # ── (d): Delivered is unreachable while a linked SE is Draft ───────

    def test_delivered_refused_with_draft_stock_entry_on_draft_manifest(self):
        manifest, transfer = self._build_world()
        manifest.status = "Delivered"
        with self.assertRaises(frappe.ValidationError) as ctx:
            manifest.save()
        self.assertIn(transfer.name, str(ctx.exception))
        self.assertEqual(
            frappe.db.get_value("CH Transfer Manifest", manifest.name, "status"),
            "Draft",
        )

    def test_delivered_refused_with_draft_stock_entry_after_submit(self):
        # Real production shape: manifests are SUBMITTED long before
        # delivery, and a submitted doc's saves bypass validate() entirely
        # (frappe runs before_update_after_submit instead) — so the gate is
        # proven on that hook path, then the same manifest is shown to
        # deliver cleanly once the Stock Entry is genuinely submitted.
        from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
            CHTransferManifest,
        )

        manifest, transfer = self._build_world()
        # Packing-slip/photo site settings are irrelevant to this proof;
        # bypass them so the manifest can reach docstatus=1.
        with (
            patch.object(CHTransferManifest, "_packing_required", return_value=False),
            patch.object(CHTransferManifest, "_packing_photo_required", return_value=False),
        ):
            manifest.submit()  # Draft → Packed
        manifest.reload()

        manifest.status = "Delivered"
        manifest.flags.ignore_validate_update_after_submit = True
        with self.assertRaises(frappe.ValidationError) as ctx:
            manifest.save()
        self.assertIn(transfer.name, str(ctx.exception))
        self.assertEqual(
            frappe.db.get_value("CH Transfer Manifest", manifest.name, "status"),
            "Packed",
        )

        # Submit the Stock Entry for real — the transition must now pass.
        se_doc = frappe.get_doc("Stock Entry", transfer.name)
        se_doc.flags.ignore_permissions = True
        # Bypass the ch_erp15 desk-submit guardrail for this fixture submit,
        # exactly as the controller's own system submits do.
        se_doc.flags.ignore_procurement_guardrails = True
        se_doc.submit()
        manifest.reload()
        manifest.status = "Delivered"
        manifest.flags.ignore_validate_update_after_submit = True
        manifest.save()
        self.assertEqual(
            frappe.db.get_value("CH Transfer Manifest", manifest.name, "status"),
            "Delivered",
        )

    # ── (c): per-row errors are aggregated, not first-row-fatal ────────

    def test_invalid_stock_entry_rows_reported_together(self):
        company = self._company()
        ghost_a = f"C3-GHOST-A-{frappe.generate_hash(length=6)}"
        ghost_b = f"C3-GHOST-B-{frappe.generate_hash(length=6)}"
        manifest = frappe.get_doc({
            "doctype": "CH Transfer Manifest",
            "manifest_date": nowdate(),
            "company": company,
            "transfers": [
                {"stock_entry": ghost_a},
                {"stock_entry": ghost_b},
            ],
        })
        manifest.flags.ignore_permissions = True
        manifest.flags.ignore_mandatory = True
        # Skip frappe's own Link existence check to model rows whose Stock
        # Entries were deleted after linking (a real estate pattern: legacy
        # Received manifests routinely outlive their purged SEs).
        manifest.flags.ignore_links = True
        with self.assertRaises(frappe.ValidationError) as ctx:
            manifest.insert(ignore_permissions=True, ignore_mandatory=True)
        message = str(ctx.exception)
        self.assertIn(ghost_a, message)
        self.assertIn(
            ghost_b, message,
            "validation used to abort at the first bad row; every bad row "
            "must be reported in one pass",
        )


if __name__ == "__main__":
    unittest.main()
