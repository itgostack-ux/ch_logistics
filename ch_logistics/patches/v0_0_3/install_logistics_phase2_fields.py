"""Logistics Phase 2 — install custom fields on CH Transfer Manifest and Driver,
and backfill direction=Forward on existing manifests.

Idempotent: safe to re-run.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


CUSTOM_FIELDS = {
    "CH Transfer Manifest": [
        {
            "fieldname": "sec_trip_link",
            "fieldtype": "Section Break",
            "label": "Trip Link",
            "insert_after": "amended_from",
            "collapsible": 1,
        },
        {
            "fieldname": "trip",
            "fieldtype": "Link",
            "label": "Logistics Trip",
            "options": "CH Logistics Trip",
            "insert_after": "sec_trip_link",
            "in_standard_filter": 1,
            "search_index": 1,
        },
        {
            "fieldname": "direction",
            "fieldtype": "Select",
            "label": "Direction",
            "options": "Forward\nReverse",
            "default": "Forward",
            "insert_after": "trip",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "col_break_trip",
            "fieldtype": "Column Break",
            "insert_after": "direction",
        },
        {
            "fieldname": "stop_sequence",
            "fieldtype": "Int",
            "label": "Stop Seq",
            "insert_after": "col_break_trip",
        },
        {
            "fieldname": "shipment_priority",
            "fieldtype": "Select",
            "label": "Priority",
            "options": "Normal\nUrgent\nHot",
            "default": "Normal",
            "insert_after": "stop_sequence",
        },
        {
            "fieldname": "box_count",
            "fieldtype": "Int",
            "label": "Box Count",
            "insert_after": "shipment_priority",
        },
        {
            "fieldname": "qr_payload",
            "fieldtype": "Small Text",
            "label": "QR Payload",
            "insert_after": "box_count",
            "read_only": 1,
        },
    ],
    "Driver": [
        {
            "fieldname": "sec_ch_logistics",
            "fieldtype": "Section Break",
            "label": "Logistics",
            "insert_after": "status",
            "collapsible": 1,
        },
        {
            "fieldname": "current_trip",
            "fieldtype": "Link",
            "label": "Current Trip",
            "options": "CH Logistics Trip",
            "insert_after": "sec_ch_logistics",
            "read_only": 1,
        },
        {
            "fieldname": "availability_status",
            "fieldtype": "Select",
            "label": "Availability",
            "options": "Available\nOn Trip\nOff Duty",
            "default": "Available",
            "insert_after": "current_trip",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "col_break_ch_logistics",
            "fieldtype": "Column Break",
            "insert_after": "availability_status",
        },
        {
            "fieldname": "max_stops_per_trip",
            "fieldtype": "Int",
            "label": "Max Stops / Trip",
            "default": "20",
            "insert_after": "col_break_ch_logistics",
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, update=True)
    _backfill_direction()


def _backfill_direction():
    """Set direction='Forward' on existing CH Transfer Manifest rows where NULL/blank."""
    if not frappe.db.has_column("CH Transfer Manifest", "direction"):
        return
    updated = frappe.db.sql(
        """
        UPDATE `tabCH Transfer Manifest`
        SET direction = 'Forward'
        WHERE direction IS NULL OR direction = ''
        """
    )
    frappe.db.commit()
    frappe.logger().info("[Phase 2] Backfilled direction=Forward on existing manifests")
