"""Store-level geocode fields.

Adds latitude / longitude / Google Maps URL to ``CH Store`` so the
Delivery App trip map can plot stops by store (in addition to the
warehouse geocode added in v0_0_4 and the per-manifest pickup/delivery
geocode captured by the driver app).

Also seeds approximate coordinates for the known Gogizmo stores when
they have no coordinates yet — sourced from the public
https://gogizmo.in/pages/store-locator listing combined with OSM
geocoding for the matching pincode / neighbourhood. Existing
coordinates are never overwritten.

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


# Approximate coordinates sourced from gogizmo.in store locator listings
# (matched by neighbourhood + pincode). Values are intentionally rounded
# to ~6 decimal places. Operators can refine any row via the Desk form;
# this patch only fills BLANK rows, never overwrites.
STORE_SEED = {
    "BMPL-STORE-01": {  # Main Store - Mumbai
        "latitude": 19.054999,
        "longitude": 72.869204,
        "source": "Mumbai city centroid (OSM)",
    },
    "BMPL-STORE-02": {  # Velachery Store -> Gogizmo Velachery
        "latitude": 12.978038,
        "longitude": 80.221529,
        "source": "Velachery Main Road, Vijaya Nagar (OSM, gogizmo.in)",
    },
    "BMPL-STORE-03": {  # Kelleys Store -> Gogizmo Kellys (Barakka Rd, Kilpauk)
        "latitude": 13.083215,
        "longitude": 80.237986,
        "source": "Barakka Rd, Kilpauk 600010 (OSM, gogizmo.in)",
    },
    "BMPL-STORE-04": {  # Doveton Store -> Gogizmo Doveton
        "latitude": 13.087322,
        "longitude": 80.258079,
        "source": "Hunters Rd, Doveton, Choolai 600007 (OSM, gogizmo.in)",
    },
    "STO-BMPL-CHENNA-0001": {  # Marina Beach Store
        "latitude": 13.053397,
        "longitude": 80.283333,
        "source": "Marina Beach, Chennai 600001 (OSM)",
    },
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
    frappe.clear_cache(doctype="CH Store")

    if not frappe.db.has_column("CH Store", "latitude"):
        # Column did not materialise — skip seeding rather than fail hard.
        return

    now = frappe.utils.now_datetime()
    for store_name, payload in STORE_SEED.items():
        if not frappe.db.exists("CH Store", store_name):
            continue
        existing = frappe.db.get_value(
            "CH Store", store_name, ["latitude", "longitude"], as_dict=True
        ) or {}
        if existing.get("latitude") or existing.get("longitude"):
            # Never overwrite a hand-set coordinate.
            continue
        gmap_url = (
            f"https://maps.google.com/?q={payload['latitude']},{payload['longitude']}"
        )
        frappe.db.set_value(
            "CH Store",
            store_name,
            {
                "latitude": payload["latitude"],
                "longitude": payload["longitude"],
                "google_maps_url": gmap_url,
                "geocoded_at": now,
            },
            update_modified=False,
        )

    # Mirror the store geocode onto the store's linked Warehouse so legacy
    # trip stops (which reference Warehouse, not CH Store) can also plot
    # on the Delivery App trip map.
    if frappe.db.has_column("Warehouse", "custom_latitude") and frappe.db.has_column(
        "Warehouse", "custom_longitude"
    ):
        for store_name in STORE_SEED.keys():
            row = frappe.db.get_value(
                "CH Store",
                store_name,
                ["warehouse", "latitude", "longitude"],
                as_dict=True,
            )
            if not row or not row.get("warehouse"):
                continue
            if not (row.get("latitude") and row.get("longitude")):
                continue
            wh_existing = frappe.db.get_value(
                "Warehouse",
                row["warehouse"],
                ["custom_latitude", "custom_longitude"],
                as_dict=True,
            ) or {}
            if wh_existing.get("custom_latitude") or wh_existing.get("custom_longitude"):
                continue
            updates = {
                "custom_latitude": row["latitude"],
                "custom_longitude": row["longitude"],
            }
            if frappe.db.has_column("Warehouse", "custom_geocoded_at"):
                updates["custom_geocoded_at"] = now
            frappe.db.set_value(
                "Warehouse",
                row["warehouse"],
                updates,
                update_modified=False,
            )

    frappe.db.commit()
