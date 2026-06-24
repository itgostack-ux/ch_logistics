"""Extend the manifest rejection contract for the in-transit failure path.

Two changes — both idempotent so the patch can be re-run safely:

  1. ``CH Transfer Manifest.rejection_reason`` Select options gain the
     in-transit failure codes used by carrier-grade logistics ERPs
     (Delhivery, BlueDart, Ekart, FedEx, Oracle TMS, SAP TM):

        Pickup-time:   Material Not Ready / Wrong Package / Store Closed
                       / Damaged Package / Other
        In-transit:    Customer Not Available / Address Not Found
                       / Receiver Refused / Damaged in Transit
                       / Vehicle Breakdown / Other

  2. New ``rejected_during`` Custom Field (Select: Pickup / In Transit)
     records the lifecycle stage at which the rejection happened, so
     reporting can split pickup failure from delivery failure cleanly.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


REJECTION_REASON_OPTIONS = "\n".join([
    "",
    # Pickup-time reasons
    "Material Not Ready",
    "Wrong Package",
    "Store Closed",
    "Damaged Package",
    # In-transit failure reasons
    "Customer Not Available",
    "Address Not Found",
    "Receiver Refused",
    "Damaged in Transit",
    "Vehicle Breakdown",
    # Catch-all
    "Other",
])


CUSTOM_FIELDS = {
    "CH Transfer Manifest": [
        {
            "fieldname": "rejected_during",
            "fieldtype": "Select",
            "label": "Rejected During",
            "options": "\nPickup\nIn Transit",
            "insert_after": "rejected_at",
            "read_only": 1,
            "in_standard_filter": 1,
            "description": "Lifecycle stage at which the driver rejected this manifest.",
        },
    ],
}


def execute():
    # 1) New field — additive, idempotent.
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)

    # 2) Extend rejection_reason options. Only widen; never shrink.
    cf = frappe.db.get_value(
        "Custom Field",
        {"dt": "CH Transfer Manifest", "fieldname": "rejection_reason"},
        "name",
    )
    if cf:
        current = frappe.db.get_value("Custom Field", cf, "options") or ""
        # Merge: keep order from the new canonical list, then append any
        # site-local options the customer may have added that we don't know.
        new_set = {line.strip() for line in REJECTION_REASON_OPTIONS.split("\n")}
        extras = [
            line for line in (l.strip() for l in current.split("\n"))
            if line and line not in new_set
        ]
        merged = REJECTION_REASON_OPTIONS
        if extras:
            merged = REJECTION_REASON_OPTIONS + "\n" + "\n".join(extras)
        if merged != current:
            frappe.db.set_value("Custom Field", cf, "options", merged)

    frappe.clear_cache(doctype="CH Transfer Manifest")
    frappe.db.commit()
