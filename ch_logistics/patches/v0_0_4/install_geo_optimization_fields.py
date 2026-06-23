"""Geo + optimization fields.

Adds the coordinate foundation that route optimization and predictive ETA need:
  * Warehouse → latitude / longitude (the canonical stop location geocode).
  * CH Logistics Trip Stop → planned coords + per-leg distance.
  * CH Logistics Trip → optimized flag + planned total distance.

Idempotent.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
    "Warehouse": [
        {
            "fieldname": "custom_geo_section",
            "fieldtype": "Section Break",
            "label": "Location (Geocode)",
            "insert_after": "pin",
            "collapsible": 1,
        },
        {
            "fieldname": "custom_latitude",
            "fieldtype": "Float",
            "label": "Latitude",
            "precision": "6",
            "insert_after": "custom_geo_section",
            "description": "Used by route optimization and live ETA.",
        },
        {
            "fieldname": "custom_longitude",
            "fieldtype": "Float",
            "label": "Longitude",
            "precision": "6",
            "insert_after": "custom_latitude",
        },
        {
            "fieldname": "custom_geocoded_at",
            "fieldtype": "Datetime",
            "label": "Geocoded At",
            "insert_after": "custom_longitude",
            "read_only": 1,
        },
    ],
    "CH Logistics Trip Stop": [
        {
            "fieldname": "plan_lat",
            "fieldtype": "Float",
            "label": "Planned Lat",
            "precision": "6",
            "insert_after": "gps_lng",
            "read_only": 1,
        },
        {
            "fieldname": "plan_lng",
            "fieldtype": "Float",
            "label": "Planned Lng",
            "precision": "6",
            "insert_after": "plan_lat",
            "read_only": 1,
        },
        {
            "fieldname": "leg_distance_km",
            "fieldtype": "Float",
            "label": "Leg Distance (km)",
            "precision": "2",
            "insert_after": "plan_lng",
            "read_only": 1,
        },
    ],
    "CH Logistics Trip": [
        {
            "fieldname": "optimized",
            "fieldtype": "Check",
            "label": "Route Optimized",
            "insert_after": "total_distance_actual_km",
            "read_only": 1,
            "in_list_view": 0,
        },
        {
            "fieldname": "total_distance_planned_km",
            "fieldtype": "Float",
            "label": "Planned Distance (km)",
            "precision": "2",
            "insert_after": "optimized",
            "read_only": 1,
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
    frappe.clear_cache(doctype="Warehouse")
    frappe.clear_cache(doctype="CH Logistics Trip")
    frappe.clear_cache(doctype="CH Logistics Trip Stop")
