from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime, nowdate

from ch_logistics.logistics.doctype.ch_logistics_otp_log.ch_logistics_otp_log import (
    record_otp_dispatch,
    verify_manifest_otp,
)


class TestLogisticsOTPAuditFlow(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        company = frappe.db.get_value("Company", {}, "name")
        if not company:
            self.skipTest("No Company is available on this site.")
        self.manifest = frappe.get_doc({
            "doctype": "CH Transfer Manifest",
            "manifest_date": nowdate(),
            "company": company,
            "status": "In Transit",
            "arrival_datetime": now_datetime(),
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

    def _issue(self, plaintext="123456", source="Driver App"):
        returned = self.manifest._generate_delivery_otp(
            request_source=source,
            plaintext_otp=plaintext,
        )
        self.manifest.flags.ignore_validate_update_after_submit = True
        self.manifest.save(ignore_permissions=True)
        self.assertEqual(returned, plaintext)
        return self.manifest.delivery_otp_log

    def test_issue_persists_digest_timestamps_and_manifest_link(self):
        log_name = self._issue()
        self.assertTrue(log_name)
        log = frappe.db.get_value(
            "CH Logistics OTP Log",
            log_name,
            [
                "manifest", "status", "request_source", "otp_digest",
                "generated_at", "expires_at", "attempts",
            ],
            as_dict=True,
        )
        self.assertEqual(log.manifest, self.manifest.name)
        self.assertEqual(log.status, "Pending")
        self.assertEqual(log.request_source, "Driver App")
        self.assertTrue(log.otp_digest.startswith("hmac-sha256$"))
        self.assertNotIn("123456", log.otp_digest)
        self.assertTrue(log.generated_at and log.expires_at)
        self.assertEqual(log.attempts, 0)
        stored = frappe.db.get_value(
            "CH Transfer Manifest",
            self.manifest.name,
            ["delivery_otp_log", "delivery_otp_generated_at", "delivery_otp_expires_at"],
            as_dict=True,
        )
        self.assertEqual(stored.delivery_otp_log, log_name)
        self.assertTrue(stored.delivery_otp_generated_at and stored.delivery_otp_expires_at)

    def test_resend_supersedes_prior_log(self):
        first = self._issue("123456")
        second = self._issue("654321", source="Manual Resend")
        self.assertNotEqual(first, second)
        self.assertEqual(
            frappe.db.get_value("CH Logistics OTP Log", first, "status"),
            "Superseded",
        )
        self.assertEqual(
            frappe.db.get_value("CH Logistics OTP Log", second, "status"),
            "Pending",
        )

    def test_dispatch_audit_masks_recipients(self):
        log_name = self._issue()
        record_otp_dispatch(
            self.manifest,
            {
                "emails": ["operations@example.com"],
                "mobiles": ["9876543210"],
                "manager_users": ["manager@example.com"],
            },
            "Sent",
        )
        log = frappe.db.get_value(
            "CH Logistics OTP Log",
            log_name,
            [
                "dispatch_status", "sent_at", "email_count", "sms_count",
                "in_app_count", "masked_emails", "masked_mobiles",
            ],
            as_dict=True,
        )
        self.assertEqual(log.dispatch_status, "Sent")
        self.assertTrue(log.sent_at)
        self.assertEqual((log.email_count, log.sms_count, log.in_app_count), (1, 1, 1))
        self.assertNotIn("operations@example.com", log.masked_emails)
        self.assertNotIn("9876543210", log.masked_mobiles)

    def test_wrong_attempt_and_expiry_are_persisted(self):
        log_name = self._issue()
        wrong = verify_manifest_otp(self.manifest, "000000")
        self.assertFalse(wrong["valid"])
        self.assertEqual(
            frappe.db.get_value("CH Logistics OTP Log", log_name, "attempts"),
            1,
        )

        frappe.db.set_value(
            "CH Logistics OTP Log",
            log_name,
            "expires_at",
            add_to_date(now_datetime(), minutes=-1),
            update_modified=False,
        )
        expired = verify_manifest_otp(self.manifest, "123456")
        self.assertFalse(expired["valid"])
        self.assertIn("expired", expired["message"].lower())
        self.assertEqual(
            frappe.db.get_value("CH Logistics OTP Log", log_name, "status"),
            "Expired",
        )

    def test_complete_delivery_retains_permanent_confirmation_audit(self):
        log_name = self._issue()
        with (
            patch.object(self.manifest, "_validate_delivery_qr"),
            patch.object(self.manifest, "_validate_geo", return_value=(12.97, 77.59)),
            patch.object(self.manifest, "_sync_logistics_status_to_entries"),
            patch.object(self.manifest, "_sync_driver_state_after_action"),
            patch.object(self.manifest, "_cascade_stop_status_to_trip"),
            patch.object(self.manifest, "_maybe_auto_close_parent_trip"),
            patch("ch_logistics.api.customer_tracking.notify_destination"),
        ):
            self.manifest.complete_delivery(
                delivery_photo="/files/delivery.jpg",
                receiver_name="Customer Receiver",
                otp="123456",
                lat=12.97,
                lng=77.59,
                scanned_qr=self.manifest.qr_payload,
            )

        stored = frappe.db.get_value(
            "CH Transfer Manifest",
            self.manifest.name,
            [
                "status", "delivery_otp", "delivery_otp_verified",
                "delivery_otp_verified_at", "delivery_otp_verified_by",
                "delivery_datetime", "delivery_photo", "receiver_name",
                "delivery_lat", "delivery_lng", "delivery_otp_log",
            ],
            as_dict=True,
        )
        self.assertEqual(stored.status, "Delivered")
        self.assertFalse(stored.delivery_otp)
        self.assertEqual(stored.delivery_otp_verified, 1)
        self.assertTrue(stored.delivery_otp_verified_at)
        self.assertEqual(stored.delivery_otp_verified_by, "Administrator")
        self.assertEqual(stored.delivery_otp_log, log_name)
        self.assertTrue(stored.delivery_datetime)
        self.assertEqual(stored.delivery_photo, "/files/delivery.jpg")
        self.assertEqual(stored.receiver_name, "Customer Receiver")
        self.assertEqual((stored.delivery_lat, stored.delivery_lng), (12.97, 77.59))

        log = frappe.db.get_value(
            "CH Logistics OTP Log",
            log_name,
            ["status", "attempts", "verified_at", "verified_by", "otp_digest"],
            as_dict=True,
        )
        self.assertEqual(log.status, "Verified")
        self.assertEqual(log.attempts, 1)
        self.assertTrue(log.verified_at)
        self.assertEqual(log.verified_by, "Administrator")
        self.assertTrue(log.otp_digest.startswith("hmac-sha256$"))


if __name__ == "__main__":
    unittest.main()
