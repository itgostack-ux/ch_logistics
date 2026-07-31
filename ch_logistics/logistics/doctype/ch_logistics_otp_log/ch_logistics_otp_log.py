from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, now_datetime


class DeliveryOTPError(frappe.ValidationError):
    """A recorded OTP refusal that API callers may return without rollback."""


class CHLogisticsOTPLog(Document):
    def before_insert(self):
        self.generated_at = self.generated_at or now_datetime()
        self.generated_by = self.generated_by or frappe.session.user
        self.status = self.status or "Pending"
        self.dispatch_status = self.dispatch_status or "Pending"
        self.attempts = cint(self.attempts)
        self.max_attempts = max(cint(self.max_attempts or _otp_setting("delivery_otp_max_attempts", 5)), 1)

    def validate(self):
        if not self.is_new() and not getattr(self.flags, "allow_audit_update", False):
            frappe.throw(_("Logistics OTP audit records are immutable."), frappe.PermissionError)


def _otp_setting(fieldname: str, default: int) -> int:
    try:
        return max(cint(frappe.db.get_single_value("CH Logistics Settings", fieldname) or default), 1)
    except Exception:
        return default


def _request_ip() -> str | None:
    return getattr(frappe.local, "request_ip", None)


def _mask_email(value: str) -> str:
    local, separator, domain = str(value or "").partition("@")
    if not separator:
        return ""
    if len(local) <= 1:
        return f"{local[:1]}***@{domain}"
    return f"{local[:1]}***{local[-1:]}@{domain}"


def _mask_mobile(value: str) -> str:
    value = str(value or "")
    if len(value) <= 4:
        return value
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def issue_manifest_otp(manifest, plaintext_otp: str, request_source: str,
                       stop_sequence: int | None = None) -> str:
    """Persist a hashed OTP audit row and supersede prior active codes."""
    from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
        delivery_otp_digest,
    )

    generated_at = now_datetime()
    expires_at = add_to_date(
        generated_at,
        minutes=_otp_setting("delivery_otp_expiry_minutes", 10),
    )
    digest = delivery_otp_digest(plaintext_otp)

    active_logs = frappe.get_all(
        "CH Logistics OTP Log",
        filters={"manifest": manifest.name, "status": "Pending"},
        pluck="name",
        limit_page_length=20,
    )
    for log_name in active_logs:
        frappe.db.set_value(
            "CH Logistics OTP Log",
            log_name,
            {
                "status": "Superseded",
                "failure_reason": _("A newer OTP was issued for this manifest."),
            },
            update_modified=False,
        )

    log = frappe.get_doc({
        "doctype": "CH Logistics OTP Log",
        "manifest": manifest.name,
        "trip": manifest.get("trip"),
        "stop_sequence": stop_sequence or manifest.get("stop_sequence"),
        "request_source": request_source,
        "status": "Pending",
        "otp_digest": digest,
        "generated_at": generated_at,
        "expires_at": expires_at,
        "generated_by": frappe.session.user,
        "attempts": 0,
        "max_attempts": _otp_setting("delivery_otp_max_attempts", 5),
        "dispatch_status": "Pending",
        "request_ip": _request_ip(),
    })
    log.insert(ignore_permissions=True)

    manifest.delivery_otp = digest
    manifest.delivery_otp_log = log.name
    manifest.delivery_otp_generated_at = generated_at
    manifest.delivery_otp_expires_at = expires_at
    manifest.delivery_otp_sent_at = None
    manifest.delivery_otp_attempts = 0
    manifest.delivery_otp_verified = 0
    manifest.delivery_otp_verified_at = None
    manifest.delivery_otp_verified_by = None
    manifest.flags.delivery_otp_plaintext = plaintext_otp
    return log.name


def record_otp_dispatch(manifest, recipients: dict, dispatch_status: str,
                        sent_at=None) -> None:
    """Stamp recipient counts and masked destinations without persisting PII."""
    log_name = manifest.get("delivery_otp_log")
    if not log_name or not frappe.db.exists("CH Logistics OTP Log", log_name):
        return

    emails = list(recipients.get("emails") or [])
    mobiles = list(recipients.get("mobiles") or [])
    users = list(recipients.get("manager_users") or [])
    sent_at = sent_at or (now_datetime() if dispatch_status == "Sent" else None)
    frappe.db.set_value(
        "CH Logistics OTP Log",
        log_name,
        {
            "dispatch_status": dispatch_status,
            "sent_at": sent_at,
            "email_count": len(emails),
            "sms_count": len(mobiles),
            "in_app_count": len(users),
            "masked_emails": ", ".join(filter(None, (_mask_email(v) for v in emails))),
            "masked_mobiles": ", ".join(filter(None, (_mask_mobile(v) for v in mobiles))),
            "failure_reason": (
                _("No destination contact was configured for OTP delivery.")
                if dispatch_status == "No Recipient" else None
            ),
        },
        update_modified=False,
    )
    manifest.delivery_otp_sent_at = sent_at
    if manifest.name and frappe.db.exists("CH Transfer Manifest", manifest.name):
        frappe.db.set_value(
            "CH Transfer Manifest",
            manifest.name,
            "delivery_otp_sent_at",
            sent_at,
            update_modified=False,
        )


def verify_manifest_otp(manifest, candidate: str | None) -> dict:
    """Verify and persist one attempt; plaintext is never stored in the log."""
    from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
        verify_delivery_otp,
    )

    candidate = str(candidate or "").strip()
    if not candidate:
        return {"valid": False, "message": _("Delivery OTP is required."), "attempts": 0}

    log_name = manifest.get("delivery_otp_log")
    if not log_name or not frappe.db.exists("CH Logistics OTP Log", log_name):
        log_name = _adopt_legacy_manifest_otp(manifest)
    if not log_name:
        return {
            "valid": False,
            "message": _("No active delivery OTP exists. Request a new OTP."),
            "attempts": 0,
        }

    row = frappe.db.get_value(
        "CH Logistics OTP Log",
        log_name,
        ["status", "otp_digest", "attempts", "max_attempts", "expires_at"],
        as_dict=True,
        for_update=True,
    )
    if not row or row.status != "Pending":
        return {
            "valid": False,
            "message": _("This delivery OTP is no longer active. Request a new OTP."),
            "attempts": cint(row.attempts if row else 0),
        }

    now = now_datetime()
    if row.expires_at and now > row.expires_at:
        frappe.db.set_value(
            "CH Logistics OTP Log",
            log_name,
            {"status": "Expired", "failure_reason": _("OTP validity period elapsed.")},
            update_modified=False,
        )
        return {
            "valid": False,
            "message": _("Delivery OTP has expired. Request a new OTP."),
            "attempts": cint(row.attempts),
        }

    attempts = cint(row.attempts) + 1
    max_attempts = max(cint(row.max_attempts), 1)
    valid = attempts <= max_attempts and verify_delivery_otp(row.otp_digest, candidate)
    values = {"attempts": attempts, "last_attempt_at": now}
    if valid:
        values.update({
            "status": "Verified",
            "verified_at": now,
            "verified_by": frappe.session.user,
            "failure_reason": None,
        })
    elif attempts >= max_attempts:
        values.update({
            "status": "Failed",
            "failure_reason": _("Maximum OTP attempts exceeded."),
        })
    frappe.db.set_value(
        "CH Logistics OTP Log", log_name, values, update_modified=False
    )

    manifest.delivery_otp_attempts = attempts
    manifest.delivery_otp_verified_at = now if valid else None
    manifest.delivery_otp_verified_by = frappe.session.user if valid else None
    if manifest.name and frappe.db.exists("CH Transfer Manifest", manifest.name):
        manifest_updates = {"delivery_otp_attempts": attempts}
        if valid:
            manifest_updates.update({
                "delivery_otp_verified_at": now,
                "delivery_otp_verified_by": frappe.session.user,
            })
        frappe.db.set_value(
            "CH Transfer Manifest",
            manifest.name,
            manifest_updates,
            update_modified=False,
        )

    remaining = max(max_attempts - attempts, 0)
    return {
        "valid": valid,
        "message": (
            _("Delivery OTP verified.")
            if valid
            else _("Invalid OTP. {0} attempt(s) remaining.").format(remaining)
        ),
        "attempts": attempts,
        "remaining_attempts": remaining,
        "otp_log": log_name,
    }


def _adopt_legacy_manifest_otp(manifest) -> str | None:
    digest = str(manifest.get("delivery_otp") or "").strip()
    if not digest:
        return None
    generated_at = manifest.get("modified") or now_datetime()
    log = frappe.get_doc({
        "doctype": "CH Logistics OTP Log",
        "manifest": manifest.name,
        "trip": manifest.get("trip"),
        "stop_sequence": manifest.get("stop_sequence"),
        "request_source": "Legacy Backfill",
        "status": "Pending",
        "otp_digest": digest,
        "generated_at": generated_at,
        "expires_at": add_to_date(
            now_datetime(), minutes=_otp_setting("delivery_otp_expiry_minutes", 10)
        ),
        "generated_by": manifest.get("modified_by") or "Administrator",
        "max_attempts": _otp_setting("delivery_otp_max_attempts", 5),
        "dispatch_status": "Legacy Unknown",
    })
    log.insert(ignore_permissions=True)
    manifest.delivery_otp_log = log.name
    frappe.db.set_value(
        "CH Transfer Manifest",
        manifest.name,
        "delivery_otp_log",
        log.name,
        update_modified=False,
    )
    return log.name
