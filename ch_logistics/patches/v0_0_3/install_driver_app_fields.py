"""Logistics Driver App — install custom fields + migrate the driver status.

Brings the schema up to the BRD's driver-app contract:
  * Driver.availability_status → 6-state operational machine (Offline / Available
    / Assigned / In Transit / Break / Idle), with legacy values remapped.
  * Driver.last_active → heartbeat column for the idle sweep.
  * CH Transfer Manifest → manifest-rejection fields (FR-022 → FR-027).
  * CH Logistics Settings → seed sensible defaults.

Idempotent: safe to re-run.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

NEW_STATUS_OPTIONS = "Offline\nAvailable\nAssigned\nIn Transit\nBreak\nIdle"

# Legacy availability_status value → canonical operational state.
STATUS_REMAP = {
    "On Trip": "In Transit",
    "Off Duty": "Offline",
    # "Available" stays "Available"
}

CUSTOM_FIELDS = {
    "Driver": [
        {
            "fieldname": "last_active",
            "fieldtype": "Datetime",
            "label": "Last Active",
            "insert_after": "availability_status",
            "read_only": 1,
            "description": "Heartbeat timestamp driving idle auto-detection.",
        },
    ],
    "CH Transfer Manifest": [
        {
            "fieldname": "sec_rejection",
            "fieldtype": "Section Break",
            "label": "Rejection",
            "insert_after": "damage_photo",
            "collapsible": 1,
        },
        {
            "fieldname": "rejection_reason",
            "fieldtype": "Select",
            "label": "Rejection Reason",
            "options": "\nMaterial Not Ready\nWrong Package\nStore Closed\nDamaged Package\nOther",
            "insert_after": "sec_rejection",
            "in_standard_filter": 1,
            "read_only": 1,
        },
        {
            "fieldname": "rejection_photo",
            "fieldtype": "Attach Image",
            "label": "Rejection Proof Photo",
            "insert_after": "rejection_reason",
            "read_only": 1,
        },
        {
            "fieldname": "rejection_notes",
            "fieldtype": "Small Text",
            "label": "Rejection Notes",
            "insert_after": "rejection_photo",
            "read_only": 1,
        },
        {
            "fieldname": "col_break_rejection",
            "fieldtype": "Column Break",
            "insert_after": "rejection_notes",
        },
        {
            "fieldname": "rejected_by",
            "fieldtype": "Link",
            "label": "Rejected By",
            "options": "User",
            "insert_after": "col_break_rejection",
            "read_only": 1,
        },
        {
            "fieldname": "rejected_at",
            "fieldtype": "Datetime",
            "label": "Rejected At",
            "insert_after": "rejected_by",
            "read_only": 1,
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)

    # 1) Expand the operational status field to the 6-state machine.
    cf = frappe.db.get_value(
        "Custom Field", {"dt": "Driver", "fieldname": "availability_status"}, "name"
    )
    if cf:
        frappe.db.set_value("Custom Field", cf, {
            "options": NEW_STATUS_OPTIONS,
            "default": "Offline",
        })

    # 2) Remap any existing legacy values to the new vocabulary.
    if frappe.db.has_column("Driver", "availability_status"):
        for old, new in STATUS_REMAP.items():
            frappe.db.sql(
                "UPDATE `tabDriver` SET availability_status=%s WHERE availability_status=%s",
                (new, old),
            )
        # Anything null/blank → Offline (not logged in).
        frappe.db.sql(
            "UPDATE `tabDriver` SET availability_status='Offline' "
            "WHERE availability_status IS NULL OR availability_status=''"
        )

    # 3) Seed CH Logistics Settings defaults (single).
    # Force-set the intended defaults: a freshly synced single is not persisted
    # to `tabSingles`, so a conditional seed leaves the operational flags
    # reading as 0/'' (QR, single-device, auto-idle all silently disabled).
    if frappe.db.exists("DocType", "CH Logistics Settings"):
        seed = {
            "idle_timeout_minutes": 15,
            "auto_idle_enabled": 1,
            "enforce_single_device": 1,
            "enforce_pickup_qr": 1,
            "enforce_delivery_otp": 1,
            "push_provider": "Frappe Web Push",
        }
        settings = frappe.get_single("CH Logistics Settings")
        for field, value in seed.items():
            settings.set(field, value)
        settings.flags.ignore_permissions = True
        settings.save()

    frappe.clear_cache(doctype="Driver")
    frappe.db.commit()
