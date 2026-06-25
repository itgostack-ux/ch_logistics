"""Backfill Driver operational fields on benches where v0_0_3 already ran.

The original ``v0_0_3.install_driver_app_fields`` patch *expected*
``availability_status`` and ``current_trip`` to already exist on the
Driver doctype (created manually via Customize Form on early benches)
and only inserted ``last_active``. On fresh DBs those two fields were
never materialised, so any call into ``driver_api`` blew up with
``Unknown column 'availability_status' in SELECT``.

This patch creates the two missing fields idempotently. It is safe to
re-run — ``create_custom_fields`` upserts on (dt, fieldname).
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


NEW_STATUS_OPTIONS = "Offline\nAvailable\nAssigned\nIn Transit\nBreak\nIdle"


CUSTOM_FIELDS = {
    "Driver": [
        {
            "fieldname": "availability_status",
            "fieldtype": "Select",
            "label": "Availability Status",
            "options": NEW_STATUS_OPTIONS,
            "default": "Offline",
            "insert_after": "transporter",
            "in_list_view": 1,
            "in_standard_filter": 1,
            "read_only": 1,
        },
        {
            "fieldname": "current_trip",
            "fieldtype": "Link",
            "label": "Current Trip",
            "options": "CH Logistics Trip",
            "insert_after": "availability_status",
            "read_only": 1,
        },
    ],
}


def execute() -> None:
    if not frappe.db.exists("DocType", "Driver"):
        return

    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)

    # Backfill any pre-existing rows whose new column landed NULL.
    if frappe.db.has_column("Driver", "availability_status"):
        frappe.db.sql(
            "UPDATE `tabDriver` SET availability_status='Offline' "
            "WHERE availability_status IS NULL OR availability_status=''"
        )

    frappe.clear_cache(doctype="Driver")
    frappe.db.commit()
