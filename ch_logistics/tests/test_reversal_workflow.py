from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import nowdate

from ch_logistics.api import logistics_api


class TestManifestReversalWorkflow(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        company = frappe.db.get_value("Company", {}, "name")
        if not company:
            self.skipTest("No Company is available on this site.")
        self.manifest = frappe.get_doc({
            "doctype": "CH Transfer Manifest",
            "manifest_date": nowdate(),
            "company": company,
            "status": "Recall Initiated",
            "qr_payload": frappe.generate_hash(length=32),
        })
        self.manifest.flags.ignore_permissions = True
        self.manifest.flags.ignore_mandatory = True
        self.manifest.insert(ignore_permissions=True, ignore_mandatory=True)

    def tearDown(self):
        frappe.set_user("Administrator")
        if getattr(self, "manifest", None) and frappe.db.exists(
            "CH Transfer Manifest", self.manifest.name
        ):
            frappe.delete_doc(
                "CH Transfer Manifest",
                self.manifest.name,
                ignore_permissions=True,
                force=True,
                delete_permanently=True,
            )
        frappe.db.rollback()
        frappe.clear_cache(doctype="Company")

    def _requirements(self):
        return {
            "items": [{
                "stock_entry": "MAT-STE-TEST",
                "item_code": "PHONE",
                "qty": 2,
                "serials": ["IMEI-001", "IMEI-002"],
            }],
            "serials": ["IMEI-001", "IMEI-002"],
            "serial_count": 2,
            "total_qty": 2,
        }

    def test_return_requires_exact_imei_set(self):
        with (
            patch.object(self.manifest, "get_return_requirements", return_value=self._requirements()),
            patch.object(self.manifest, "_reverse_stock_entries") as reverse,
        ):
            with self.assertRaises(frappe.ValidationError):
                self.manifest.confirm_return(
                    return_photo="/files/return.jpg",
                    returned_serials=["IMEI-001", "WRONG-IMEI"],
                )
        reverse.assert_not_called()
        self.assertEqual(
            frappe.db.get_value("CH Transfer Manifest", self.manifest.name, "status"),
            "Recall Initiated",
        )

    def test_duplicate_return_scan_is_rejected(self):
        with patch.object(
            self.manifest, "get_return_requirements", return_value=self._requirements()
        ), self.assertRaises(frappe.ValidationError):
            self.manifest.confirm_return(
                return_photo="/files/return.jpg",
                returned_serials=["IMEI-001", "IMEI-001"],
            )

    def test_nonserialized_return_requires_exact_physical_count(self):
        requirements = {
            "items": [{
                "stock_entry": "MAT-STE-TEST",
                "row_name": "ROW-001",
                "item_code": "ACCESSORY",
                "qty": 2,
                "serials": [],
            }],
            "serials": [],
            "serial_count": 0,
            "total_qty": 2,
        }
        with (
            patch.object(
                self.manifest,
                "get_return_requirements",
                return_value=requirements,
            ),
            patch.object(self.manifest, "_reverse_stock_entries") as reverse,
        ):
            with self.assertRaises(frappe.ValidationError):
                self.manifest.confirm_return(
                    return_photo="/files/return.jpg",
                    returned_serials=[],
                    returned_quantities=[{
                        "stock_entry": "MAT-STE-TEST",
                        "row_name": "ROW-001",
                        "qty": 1,
                    }],
                )
        reverse.assert_not_called()

    def test_stock_failure_does_not_mark_manifest_returned(self):
        with (
            patch.object(self.manifest, "get_return_requirements", return_value=self._requirements()),
            patch.object(
                self.manifest,
                "_reverse_stock_entries",
                side_effect=frappe.ValidationError("ledger reversal failed"),
            ),
        ):
            with self.assertRaises(frappe.ValidationError):
                self.manifest.confirm_return(
                    return_photo="/files/return.jpg",
                    returned_serials=["IMEI-001", "IMEI-002"],
                )
        self.assertEqual(
            frappe.db.get_value("CH Transfer Manifest", self.manifest.name, "status"),
            "Recall Initiated",
        )

    def test_success_marks_returned_only_after_reversal(self):
        with (
            patch.object(self.manifest, "get_return_requirements", return_value=self._requirements()),
            patch.object(
                self.manifest,
                "_reverse_stock_entries",
                return_value=["MAT-STE-TEST (custom transit reversed)"],
            ) as reverse,
            patch.object(self.manifest, "_maybe_finalize_recalled_trip"),
            patch.object(self.manifest, "_maybe_auto_close_parent_trip"),
        ):
            actions = self.manifest.confirm_return(
                return_photo="/files/return.jpg",
                returned_serials=["IMEI-002", "IMEI-001"],
            )
        reverse.assert_called_once()
        self.assertEqual(actions, ["MAT-STE-TEST (custom transit reversed)"])
        self.assertEqual(
            frappe.db.get_value("CH Transfer Manifest", self.manifest.name, "status"),
            "Returned",
        )

    def test_predeparture_cancel_failure_keeps_manifest_active(self):
        self.manifest.db_set("status", "Packed")
        self.manifest.status = "Packed"
        with patch.object(
            self.manifest,
            "_reverse_stock_entries",
            side_effect=frappe.ValidationError("cannot reverse"),
        ), self.assertRaises(frappe.ValidationError):
            self.manifest.cancel_before_departure("Trip cancelled")
        self.assertEqual(
            frappe.db.get_value("CH Transfer Manifest", self.manifest.name, "status"),
            "Packed",
        )

    def test_postdeparture_standard_transfer_uses_compensating_entry(self):
        self.manifest.append("transfers", {"stock_entry": "MAT-STE-POSTED"})
        stock_entry = frappe._dict({
            "name": "MAT-STE-POSTED",
            "docstatus": 1,
            "custom_status": "",
            "ewaybill": "",
        })
        stock_entry.cancel = MagicMock()
        with (
            patch.object(frappe, "get_doc", return_value=stock_entry),
            patch(
                "ch_erp15.ch_erp15.custom.stock_entry.reverse_transfer_to_source",
                return_value=None,
            ),
            patch.object(
                self.manifest,
                "_create_reverse_se",
                return_value="MAT-STE-RETURN",
            ) as create_reverse,
        ):
            result = self.manifest._reverse_stock_entries(
                reason="Vehicle recalled",
                allow_original_cancellation=False,
            )

        stock_entry.cancel.assert_not_called()
        create_reverse.assert_called_once_with(
            "MAT-STE-POSTED",
            reason="Vehicle recalled",
        )
        self.assertEqual(
            result,
            ["MAT-STE-POSTED → reverse MAT-STE-RETURN"],
        )

    def test_predeparture_cancel_blocks_untracked_ewaybill(self):
        stock_entry = frappe._dict({
            "name": "MAT-STE-EWB",
            "ewaybill": "EWB-TEST-001",
        })
        with patch.object(frappe.db, "get_value", return_value=None):
            with self.assertRaises(frappe.ValidationError):
                self.manifest._validate_ewaybill_cancellation(stock_entry)

    def test_predeparture_manifest_cancel_restores_real_stock_to_source(self):
        from ch_erp15.ch_erp15.custom.stock_entry import (
            received_qty,
            set_custom_status,
            set_pending_qty,
        )

        company = self.manifest.company
        abbr = frappe.get_cached_value("Company", company, "abbr")
        suffix = frappe.generate_hash(length=6).upper()

        def warehouse(label):
            doc = frappe.new_doc("Warehouse")
            doc.warehouse_name = f"REV-{label}-{suffix}"
            doc.company = company
            doc.is_group = 0
            doc.insert(ignore_permissions=True)
            return doc.name

        source = warehouse("SRC")
        destination = warehouse("DST")
        transit = frappe.get_cached_value(
            "Company", company, "default_in_transit_warehouse"
        )
        if not transit:
            transit_name = f"Goods In Transit - {abbr}"
            if not frappe.db.exists("Warehouse", transit_name):
                transit = warehouse("TRANSIT")
                frappe.db.set_value(
                    "Company", company, "default_in_transit_warehouse", transit
                )
                frappe.clear_cache(doctype="Company")
            else:
                transit = transit_name

        item_code = f"REV-ITEM-{suffix}"
        item = frappe.new_doc("Item")
        item.item_code = item_code
        item.item_name = item_code
        item.item_group = frappe.db.get_value(
            "Item Group", {"is_group": 0}, "name"
        ) or "Products"
        item.stock_uom = "Nos"
        item.is_stock_item = 1
        if item.meta.has_field("ch_lifecycle_status"):
            item.ch_lifecycle_status = "Active"
        barcode = f"REVBC{suffix}"
        item.append("barcodes", {"barcode": barcode})
        item.insert(ignore_permissions=True)

        receipt = frappe.new_doc("Stock Entry")
        receipt.stock_entry_type = "Material Receipt"
        receipt.company = company
        receipt.to_warehouse = source
        receipt.append("items", {
            "item_code": item_code,
            "qty": 2,
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
        if transfer.meta.has_field("custom_transfer_type"):
            transfer.custom_transfer_type = "Store Transfer"
        transfer.append("items", {
            "item_code": item_code,
            "qty": 2,
            "custom_quantity": 2,
            "uom": "Nos",
            "s_warehouse": source,
            "t_warehouse": destination,
        })
        transfer.insert(ignore_permissions=True)
        set_pending_qty(transfer.name)
        staged = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": source}, "actual_qty"
        ) or 0
        self.assertEqual(float(staged), 0)

        self.assertTrue(received_qty(transfer.name, barcode)["success"])
        self.assertTrue(received_qty(transfer.name, barcode)["success"])
        ready = set_custom_status(transfer.name, "Ready For Pickup")
        manifest = frappe.get_doc(
            "CH Transfer Manifest", ready["transfer_manifest"]
        )
        manifest.cancel_before_departure("E2E pre-departure cancellation")

        source_after = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": source}, "actual_qty"
        ) or 0
        transit_after = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": transit}, "actual_qty"
        ) or 0
        transfer.reload()
        manifest.reload()
        self.assertEqual(float(source_after), 2)
        self.assertEqual(float(transit_after), 0)
        self.assertEqual(transfer.custom_logistics_status, "Reverted")
        self.assertEqual(manifest.status, "Cancelled")


class TestTripCancellationOrchestration(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    @staticmethod
    def _trip(status="Assigned"):
        trip = frappe._dict({
            "name": "TRIP-TEST",
            "status": status,
            "driver": "DRIVER-TEST",
            "cancellation_reason": None,
        })
        trip.check_permission = MagicMock()
        trip.as_dict = MagicMock(return_value=dict(trip))
        trip.save = MagicMock()
        trip.add_comment = MagicMock()
        return trip

    def test_predeparture_trip_cancel_cascades_to_manifest_and_stock(self):
        trip = self._trip()
        manifest = MagicMock()
        manifest.cancel_before_departure.return_value = [
            "MAT-STE-TEST (custom transit reversed)"
        ]
        rows = [
            frappe._dict(
                name="MANIFEST-TEST",
                status="Assigned",
                pickup_datetime=None,
            )
        ]
        with (
            patch.object(logistics_api, "_require_ops"),
            patch.object(logistics_api, "get_locked_trip", return_value=trip),
            patch.object(logistics_api.scope_guard, "assert_trip_scope"),
            patch.object(logistics_api, "lock_manifests"),
            patch.object(logistics_api.frappe, "get_all", return_value=rows),
            patch.object(logistics_api.frappe, "get_doc", return_value=manifest),
            patch.object(logistics_api, "_trip_audit"),
            patch.object(logistics_api, "_set_driver_availability"),
        ):
            result = logistics_api.trip_cancel("TRIP-TEST", reason="Vehicle unavailable")

        manifest.cancel_before_departure.assert_called_once()
        self.assertEqual(trip.status, "Cancelled")
        self.assertEqual(result["cancelled_manifests"], ["MANIFEST-TEST"])
        self.assertEqual(
            result["stock_reversals"],
            ["MAT-STE-TEST (custom transit reversed)"],
        )

    def test_trip_cancel_blocks_after_physical_pickup(self):
        trip = self._trip()
        rows = [
            frappe._dict(
                name="MANIFEST-TEST",
                status="In Transit",
                pickup_datetime="2026-07-31 10:00:00",
            )
        ]
        with (
            patch.object(logistics_api, "_require_ops"),
            patch.object(logistics_api, "get_locked_trip", return_value=trip),
            patch.object(logistics_api.scope_guard, "assert_trip_scope"),
            patch.object(logistics_api, "lock_manifests"),
            patch.object(logistics_api.frappe, "get_all", return_value=rows),
        ):
            with self.assertRaises(frappe.ValidationError):
                logistics_api.trip_cancel("TRIP-TEST", reason="Driver illness")
        self.assertEqual(trip.status, "Assigned")

    def test_started_trip_recall_cascades_without_premature_cancel(self):
        trip = self._trip(status="Started")
        rows = [
            frappe._dict(name="MANIFEST-A", status="In Transit"),
            frappe._dict(name="MANIFEST-B", status="Delivered"),
        ]
        manifests = {
            row.name: MagicMock(name=row.name)
            for row in rows
        }
        with (
            patch.object(logistics_api, "_require_ops"),
            patch.object(logistics_api, "get_locked_trip", return_value=trip),
            patch.object(logistics_api.scope_guard, "assert_trip_scope"),
            patch.object(logistics_api, "lock_manifests"),
            patch.object(logistics_api.frappe, "get_all", return_value=rows),
            patch.object(
                logistics_api.frappe,
                "get_doc",
                side_effect=lambda doctype, name: manifests[name],
            ),
            patch.object(logistics_api, "_trip_audit"),
        ):
            result = logistics_api.trip_initiate_recall(
                "TRIP-TEST",
                reason="Vehicle breakdown",
            )

        self.assertEqual(trip.status, "Started")
        self.assertEqual(trip.cancellation_reason, "Vehicle breakdown")
        self.assertEqual(
            result["recalled_manifests"],
            ["MANIFEST-A", "MANIFEST-B"],
        )
        for manifest in manifests.values():
            manifest.initiate_recall.assert_called_once()


if __name__ == "__main__":
    unittest.main()
