"""Digest legacy OTPs and rotate proof bearers that were previously readable."""

import frappe

from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
    _OTP_DIGEST_PREFIX,
    delivery_otp_digest,
)


_ACTIVE_MANIFEST_STATUSES = (
    "Draft",
    "Packed",
    "Assigned",
    "Pickup Started",
    "In Transit",
    "Delivered",
    "Received",
    "Partially Received",
)
_ACTIVE_TRIP_STATUSES = ("Draft", "Planned", "Assigned", "In Transit")


def _protect_tracking_custom_field():
    name = frappe.db.get_value(
        "Custom Field",
        {"dt": "CH Transfer Manifest", "fieldname": "tracking_token"},
        "name",
    )
    if name:
        frappe.db.set_value(
            "Custom Field",
            name,
            {"permlevel": 1, "hidden": 1, "read_only": 1, "no_copy": 1},
            update_modified=False,
        )


def _rotate_manifests():
    cursor = ""
    while True:
        rows = frappe.get_all(
            "CH Transfer Manifest",
            filters={
                "name": (">", cursor),
                "docstatus": ("<", 2),
                "status": ("in", _ACTIVE_MANIFEST_STATUSES),
            },
            fields=["name", "delivery_otp"],
            order_by="name asc",
            limit_page_length=500,
        )
        if not rows:
            break
        updates = {}
        for row in rows:
            values = {
                # Every active QR and public tracking bearer was readable at
                # permlevel 0 before this patch, so rotate it unconditionally.
                "qr_payload": frappe.generate_hash(length=32),
                "tracking_token": frappe.generate_hash(length=32),
            }
            otp = str(row.delivery_otp or "").strip()
            if otp and not otp.startswith(_OTP_DIGEST_PREFIX):
                values["delivery_otp"] = delivery_otp_digest(otp)
            updates[row.name] = values
        frappe.db.bulk_update("CH Transfer Manifest", updates, update_modified=False)
        cursor = rows[-1].name


def _rotate_trip_stops():
    trip_cursor = ""
    while True:
        trips = frappe.get_all(
            "CH Logistics Trip",
            filters={
                "name": (">", trip_cursor),
                "status": ("in", _ACTIVE_TRIP_STATUSES),
            },
            pluck="name",
            order_by="name asc",
            limit_page_length=200,
        )
        if not trips:
            break
        stops = frappe.get_all(
            "CH Logistics Trip Stop",
            filters={"parent": ("in", tuple(trips))},
            pluck="name",
            limit_page_length=5000,
        )
        if stops:
            frappe.db.bulk_update(
                "CH Logistics Trip Stop",
                {
                    name: {
                        "pickup_token": frappe.generate_hash(length=32),
                        "delivery_token": frappe.generate_hash(length=32),
                    }
                    for name in stops
                },
                update_modified=False,
            )
        trip_cursor = trips[-1]


def execute():
    _protect_tracking_custom_field()
    _rotate_manifests()
    _rotate_trip_stops()
    frappe.clear_cache(doctype="CH Transfer Manifest")
    frappe.clear_cache(doctype="CH Logistics Trip")
