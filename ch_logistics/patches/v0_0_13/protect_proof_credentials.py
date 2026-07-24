"""Digest legacy OTPs and rotate proof bearers that were previously readable.

Runs as post_model_sync so the tracking_token / pickup_token / delivery_token
columns already exist in the database when we try to bulk-update them.
"""

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


def _column_exists(doctype, column):
    """Authoritative check for a physical column via information_schema — NOT
    ``frappe.db.has_column``, which reads a cached column list that can report a
    column present after it was dropped (or absent right after it is added). We
    act on the real schema so cache staleness can't defeat the guards.
    """
    return bool(
        frappe.db.sql(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s AND column_name = %s
            """,
            (frappe.conf.db_name, f"tab{doctype}", column),
        )
    )


def _ensure_tracking_token_column():
    """Self-heal the ``tracking_token`` custom field before we rotate it.

    ``tracking_token`` is a Custom Field created by the earlier pre_model_sync
    patch ``v0_0_5.install_tracking_token``. Some databases have that patch
    marked as run (Patch Log) while the column is missing — e.g. a DB
    branched/restored from a state where the custom field was never persisted,
    or the field was later dropped. Because patches run only once, it is never
    recreated, so this post_model_sync patch would die with
    ``Unknown column 'tracking_token' in 'SET'`` on ``bulk_update``. Recreate it
    here so the migrate always succeeds. No-op when the column already exists.
    """
    if _column_exists("CH Transfer Manifest", "tracking_token"):
        return
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    from ch_logistics.patches.v0_0_5.install_tracking_token import CUSTOM_FIELDS

    # create_custom_fields() is a no-op when the Custom Field *doc* already
    # exists — even if its column was dropped (schema drift). Remove any such
    # orphan first so the recreate re-adds the column cleanly.
    orphan = frappe.db.get_value(
        "Custom Field",
        {"dt": "CH Transfer Manifest", "fieldname": "tracking_token"},
        "name",
    )
    if orphan:
        frappe.delete_doc("Custom Field", orphan, force=True, ignore_permissions=True)
        frappe.db.commit()

    # Add the physical column FIRST via raw DDL — a stale table-columns cache
    # could otherwise fool create_custom_fields()/add_column() into skipping the
    # ALTER. The bulk_update writes to the real column, so this is what matters.
    if not _column_exists("CH Transfer Manifest", "tracking_token"):
        frappe.db.sql(
            "ALTER TABLE `tabCH Transfer Manifest` "
            "ADD COLUMN `tracking_token` varchar(140)"
        )
        frappe.db.commit()

    # Now (re)create the Custom Field metadata over the existing column and drop
    # frappe's cached column list so later reads reflect reality.
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
    frappe.db.commit()
    frappe.clear_cache(doctype="CH Transfer Manifest")


def _rotate_manifests():
    # Rotate only the bearer columns that physically exist. A schema-drifted DB
    # may be missing one or more of them, and migrate must never crash on that
    # (any missing one is simply skipped — there is nothing to rotate).
    has_qr = _column_exists("CH Transfer Manifest", "qr_payload")
    has_tracking = _column_exists("CH Transfer Manifest", "tracking_token")
    has_otp = _column_exists("CH Transfer Manifest", "delivery_otp")
    if not (has_qr or has_tracking or has_otp):
        return
    fields = ["name"] + (["delivery_otp"] if has_otp else [])
    cursor = ""
    while True:
        rows = frappe.get_all(
            "CH Transfer Manifest",
            filters={
                "name": (">", cursor),
                "docstatus": ("<", 2),
                "status": ("in", _ACTIVE_MANIFEST_STATUSES),
            },
            fields=fields,
            order_by="name asc",
            limit_page_length=500,
        )
        if not rows:
            break
        updates = {}
        for row in rows:
            # Every active QR and public tracking bearer was readable at
            # permlevel 0 before this patch, so rotate it unconditionally.
            values = {}
            if has_qr:
                values["qr_payload"] = frappe.generate_hash(length=32)
            if has_tracking:
                values["tracking_token"] = frappe.generate_hash(length=32)
            if has_otp:
                otp = str(row.get("delivery_otp") or "").strip()
                if otp and not otp.startswith(_OTP_DIGEST_PREFIX):
                    values["delivery_otp"] = delivery_otp_digest(otp)
            if values:
                updates[row.name] = values
        if updates:
            frappe.db.bulk_update("CH Transfer Manifest", updates, update_modified=False)
        cursor = rows[-1].name


def _rotate_trip_stops():
    has_pickup = _column_exists("CH Logistics Trip Stop", "pickup_token")
    has_delivery = _column_exists("CH Logistics Trip Stop", "delivery_token")
    if not (has_pickup or has_delivery):
        return

    def _stop_values():
        v = {}
        if has_pickup:
            v["pickup_token"] = frappe.generate_hash(length=32)
        if has_delivery:
            v["delivery_token"] = frappe.generate_hash(length=32)
        return v

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
                {name: _stop_values() for name in stops},
                update_modified=False,
            )
        trip_cursor = trips[-1]


def execute():
    # Best-effort self-heal. Even if it fails, the rotate helpers below skip any
    # column that is still missing, so the migrate can never crash on drift.
    try:
        _ensure_tracking_token_column()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "protect_proof_credentials: tracking_token self-heal failed (continuing)",
        )
    _protect_tracking_custom_field()
    _rotate_manifests()
    _rotate_trip_stops()
    frappe.clear_cache(doctype="CH Transfer Manifest")
    frappe.clear_cache(doctype="CH Logistics Trip")
