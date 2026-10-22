"""Store-level geocode fields.

Adds latitude / longitude / Google Maps URL to ``CH Store`` so the
Delivery App trip map can plot stops by store (in addition to the
warehouse geocode added in v0_0_4 and the per-manifest pickup/delivery
geocode captured by the driver app).

Fields only: store coordinates are site data, entered on the CH Store
form or filled by the geocoder, not shipped in code.

Idempotent.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
    "CH Store": [
        {
            "fieldname": "custom_geo_section",
            "fieldtype": "Section Break",
            "label": "Map Location",
            "insert_after": "pincode",
            "collapsible": 1,
        },
        {
            "fieldname": "latitude",
            "fieldtype": "Float",
            "label": "Latitude",
            "precision": "6",
            "insert_after": "custom_geo_section",
            "description": "Used by the Delivery App trip map and route planning.",
        },
        {
            "fieldname": "longitude",
            "fieldtype": "Float",
            "label": "Longitude",
            "precision": "6",
            "insert_after": "latitude",
        },
        {
            "fieldname": "google_maps_url",
            "fieldtype": "Data",
            "label": "Google Maps URL",
            "insert_after": "longitude",
            "description": "Optional sharable Google Maps link to this store.",
        },
        {
            "fieldname": "geocoded_at",
            "fieldtype": "Datetime",
            "label": "Geocoded At",
            "insert_after": "google_maps_url",
            "read_only": 1,
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
    frappe.clear_cache(doctype="CH Store")
