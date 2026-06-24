"""Add arrival location capture fields to CH Transfer Manifest.

Two-stage POD: pickup proves the driver was at the source, arrival proves
they reached the destination *before* the receiver hands them the OTP /
signs the manifest. Carrier industry equivalent of the \"Arrived at
Destination\" geofence ping (Delhivery, BlueDart, Ekart, FedEx).

Custom fields (idempotent — patch is safe to re-run):

    arrival_datetime  Datetime   when the driver tapped \"Reached Location\"
    arrival_lat       Float      GPS latitude at that moment
    arrival_lng       Float      GPS longitude at that moment

All three are read-only because they're set server-side by
``mark_reached_destination``. They're inserted right after the delivery
location block so the form Proof tab reads top-to-bottom in chronological
order: pickup → arrival → delivery.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


CUSTOM_FIELDS = {
    "CH Transfer Manifest": [
        {
            "fieldname": "arrival_datetime",
            "fieldtype": "Datetime",
            "label": "Arrived At Destination",
            "insert_after": "delivery_lng",
            "read_only": 1,
            "description": "Timestamp when the driver tapped 'Reached Location'.",
        },
        {
            "fieldname": "arrival_lat",
            "fieldtype": "Float",
            "label": "Arrival Latitude",
            "insert_after": "arrival_datetime",
            "read_only": 1,
            "hidden": 1,
        },
        {
            "fieldname": "arrival_lng",
            "fieldtype": "Float",
            "label": "Arrival Longitude",
            "insert_after": "arrival_lat",
            "read_only": 1,
            "hidden": 1,
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
    frappe.clear_cache(doctype="CH Transfer Manifest")
    frappe.db.commit()
