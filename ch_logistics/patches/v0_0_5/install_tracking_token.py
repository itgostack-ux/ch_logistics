"""Add the opaque public tracking token to CH Transfer Manifest and backfill
existing manifests so their track-and-trace links work immediately."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
    "CH Transfer Manifest": [
        {
            "fieldname": "tracking_token",
            "fieldtype": "Data",
            "label": "Tracking Token",
            "insert_after": "qr_payload",
            "read_only": 1,
            "unique": 1,
            "no_copy": 1,
            "hidden": 1,
            "permlevel": 1,
            "description": "Opaque id for the public /track page.",
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
    # Backfill tokens for manifests that are already on the road.
    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters={"tracking_token": ["in", [None, ""]]},
        pluck="name",
    )
    for name in rows:
        frappe.db.set_value("CH Transfer Manifest", name, "tracking_token",
                            frappe.generate_hash(length=22), update_modified=False)
    frappe.db.commit()
