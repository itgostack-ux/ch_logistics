"""Seed the full Gogizmo retail network into CH Store + geocode it.

Source: public store-locator at https://gogizmo.in/pages/store-locator
(25 retail branches) cross-referenced with OSM Nominatim for lat / lng
at neighbourhood + pincode granularity.

For each branch we:
  * Match an existing ``CH Store`` by canonical store_code (``GG-<slug>``)
    or by a curated name-pattern map (covers the 3 legacy stores that
    were created before this catalogue existed: BMPL-STORE-02 Velachery,
    BMPL-STORE-03 Kelleys, BMPL-STORE-04 Doveton).
  * Create the row if no match exists. The ``CH Store`` autoname hook
    composes ``STO-<COMPANY_ABBR>-<CITY>-####`` from ``store_code`` but
    respects an explicit override, so we pin ``store_code = GG-<slug>``
    for deterministic, re-runnable seeding.
  * Backfill the geocode fields (``latitude``, ``longitude``,
    ``google_maps_url``, ``geocoded_at``) added in v0_0_7 — but only
    where the row currently has no lat/lng, so operator-set coordinates
    are never clobbered.

This is the same pattern SAP TM's Location Master and Oracle OTM's
Location Catalogue use: a single, idempotent geocode seed that does not
overwrite curated locations.
"""
from __future__ import annotations

import re

import frappe

# Default operating company. There is exactly one Company in the system
# (BestBuy Mobiles Pvt Ltd); this resolver makes the patch tolerant if
# that ever changes.
_DEFAULT_COMPANY = "BestBuy Mobiles Pvt Ltd"

# The Gogizmo retail network. Coordinates are OSM-derived at
# neighbourhood + pincode granularity (typical accuracy: ~100–500 m).
# Operators can refine any row from the Desk form; subsequent re-runs
# of this patch will not overwrite a non-blank coordinate.
GOGIZMO_BRANCHES = [
    {
        "slug": "ALWARTHIRUNAGAR",
        "name": "Gogizmo - Alwarthirunagar",
        "address": "First Floor, K Sundaramoorthy Complex, 1A, Arcot Rd, Alwartirunagar, Valasaravakkam, Chennai",
        "city": "Chennai",
        "pincode": "600087",
        "lat": 13.047486,
        "lng": 80.183669,
    },
    {
        "slug": "AMBATTUR",
        "name": "Gogizmo - Ambattur",
        "address": "No.395, Plot. 42, Madras Thiruvallur High Rd, Secretariat Colony, Vivek Nagar, Ambattur, Chennai",
        "city": "Chennai",
        "pincode": "600053",
        "lat": 13.114894,
        "lng": 80.154630,
    },
    {
        "slug": "ANNANAGAR",
        "name": "Gogizmo - Anna Nagar",
        "address": "A-45 3rd Avenue, next to P ORR & SONS, Anna Nagar, Chennai",
        "city": "Chennai",
        "pincode": "600102",
        "lat": 13.091274,
        "lng": 80.218177,
    },
    {
        "slug": "ASHOKNAGAR",
        "name": "Gogizmo - Ashok Nagar",
        "address": "Door no: 31 & 32, Anjugam nagar 1st Street, Jafferkhanpet, Ashok Nagar, Chennai",
        "city": "Chennai",
        "pincode": "600083",
        "lat": 13.029759,
        "lng": 80.208766,
    },
    {
        "slug": "DOVETON",
        "name": "Gogizmo - Doveton",
        "address": "12, Hunters Rd, Doveton, Choolai, Chennai",
        "city": "Chennai",
        "pincode": "600007",
        "lat": 13.087322,
        "lng": 80.258079,
        # Legacy alias — created as BMPL-STORE-04 long before this seed.
        "match_existing_name_like": ["%Doveton%"],
    },
    {
        "slug": "KELAMBAKKAM",
        "name": "GoGizmo - Kelambakkam",
        "address": "New Survey No. 1530/7, Police Station, Shop No 2, Old survey No. 3, 2A3, Rajiv Gandhi Salai, near Kelambakkam",
        "city": "Chennai",
        "pincode": "603103",
        "lat": 12.786249,
        "lng": 80.221994,
    },
    {
        "slug": "KELLYS",
        "name": "Gogizmo - Kellys",
        "address": "Old No.163/ New, 237, Barakka Rd, Secretariat Colony, Kilpauk, Chennai",
        "city": "Chennai",
        "pincode": "600010",
        "lat": 13.083215,
        "lng": 80.237986,
        "match_existing_name_like": ["%Kell%"],
    },
    {
        "slug": "KODAMBAKKAM",
        "name": "Gogizmo - Kodambakkam",
        "address": "NO 41/71, Arcot Rd, United India Colony, Kodambakkam, Chennai",
        "city": "Chennai",
        "pincode": "600024",
        "lat": 13.049208,
        "lng": 80.224283,
    },
    {
        "slug": "KOLATHUR",
        "name": "Gogizmo - Kolathur",
        "address": "No: 144/1, Paper Mills Road, Anjugam Nagar, Sakthivel Nagar, Peravallur, Perambur, Chennai",
        "city": "Chennai",
        "pincode": "600099",
        "lat": 13.124113,
        "lng": 80.204628,
    },
    {
        "slug": "MADURAI",
        "name": "Gogizmo - Madurai",
        "address": "39, N Veli St, Simmakkal, Madurai Main, Madurai",
        "city": "Madurai",
        "pincode": "625001",
        "lat": 9.924921,
        "lng": 78.121088,
    },
    {
        "slug": "MADURAI-ANNANAGAR",
        "name": "Gogizmo - Madurai Anna Nagar",
        "address": "Mig 337, 80 Feet Rd, Anna Nagar, Madurai",
        "city": "Madurai",
        "pincode": "625020",
        "lat": 9.921675,
        "lng": 78.148137,
    },
    {
        "slug": "MINJUR",
        "name": "Gogizmo - Minjur",
        "address": "No: 404, T.H.Road, Minjur, Chennai",
        "city": "Chennai",
        "pincode": "601203",
        "lat": 13.287820,
        "lng": 80.255861,
    },
    {
        "slug": "MOGAPPAIR",
        "name": "Gogizmo - Mogappair",
        "address": "Shop no. G-2, Ground Floor, Plot No4/PC-2, Bharathi Salai, 4th Block, Mogappair West, Ambattur Industrial Estate, Chennai",
        "city": "Chennai",
        "pincode": "600037",
        "lat": 13.088062,
        "lng": 80.184605,
    },
    {
        "slug": "OLDWASHERMANPET",
        "name": "GoGizmo - Old Washermanpet",
        "address": "564, Thiruvottiyur High Rd, near Vaigai Mahal, Korukkupet, Washermanpet, Chennai",
        "city": "Chennai",
        "pincode": "600021",
        "lat": 13.119425,
        "lng": 80.278456,
    },
    {
        "slug": "PALAVAKKAM",
        "name": "Gogizmo - Palavakkam",
        "address": "D4 156 East Coast Road, Beach Rd, Palavakkam, Chennai",
        "city": "Chennai",
        "pincode": "600041",
        "lat": 12.959990,
        "lng": 80.256027,
    },
    {
        "slug": "PALLAVARAM",
        "name": "Gogizmo - Pallavaram",
        "address": "21, Pillaiyar Koil St, Contonment, Pallavaram, Chennai",
        "city": "Chennai",
        "pincode": "600043",
        "lat": 12.967574,
        "lng": 80.152050,
    },
    {
        "slug": "PAPERMILLS-PERAMBUR",
        "name": "GoGizmo - Paper Mills Road, Perambur",
        "address": "130, Paper Mills Road, Neelam Garden, Gopal Colony, Perambur, Chennai",
        "city": "Chennai",
        "pincode": "600011",
        "lat": 13.112124,
        "lng": 80.245022,
    },
    {
        "slug": "PERAMBUR-HIGHRD",
        "name": "Gogizmo - Perambur High Road",
        "address": "New no 89, KSP Complex, Ground Floor, Old, 69/2, Perambur High Rd, Perambur, Chennai",
        "city": "Chennai",
        "pincode": "600011",
        "lat": 13.115500,
        "lng": 80.249500,
    },
    {
        "slug": "PERUNGUDI",
        "name": "Gogizmo - Perungudi",
        "address": "Burma Colony, Thiruvalluvar Nagar, Perungudi, Chennai",
        "city": "Chennai",
        "pincode": "600096",
        "lat": 12.959282,
        "lng": 80.242603,
    },
    {
        "slug": "TAMBARAM",
        "name": "Gogizmo - Tambaram",
        "address": "Door No.11, B.R.Annexe, 35, Ramakrishna Iyer Street, West Tambaram, Tambaram, Chennai",
        "city": "Chennai",
        "pincode": "600045",
        "lat": 12.925655,
        "lng": 80.104370,
    },
    {
        "slug": "TAMBARAM-SHANMUGAM",
        "name": "Gogizmo - Tambaram Shanmugam Road",
        "address": "Premises Of Tambaram, Municipal Corporation, No. 9 & 10, Shanmugam Rd, West Tambaram, Tambaram, Chennai",
        "city": "Chennai",
        "pincode": "600045",
        "lat": 12.926000,
        "lng": 80.119500,
    },
    {
        "slug": "THIRUVOTTIYUR",
        "name": "Gogizmo - Thiruvottiyur",
        "address": "194, Thiruvottiyur High Rd, Theradi, Rajakadai, Tiruvottiyur, Chennai",
        "city": "Chennai",
        "pincode": "600019",
        "lat": 13.178732,
        "lng": 80.307692,
    },
    {
        "slug": "VELACHERY",
        "name": "Gogizmo - Velachery",
        "address": "Door No 41/348, First Segment, 1, Velachery Main Rd, Vijaya Nagar, Velachery, Chennai",
        "city": "Chennai",
        "pincode": "600042",
        "lat": 12.978038,
        "lng": 80.221529,
        "match_existing_name_like": ["%Velachery%"],
    },
    {
        "slug": "WEST-TAMBARAM",
        "name": "GoGizmo - West Tambaram",
        "address": "Door No.11, B.R.Annexe, 35, Ramakrishna Iyer Street, West Tambaram",
        "city": "Chennai",
        "pincode": "600045",
        "lat": 12.924200,
        "lng": 80.118300,
    },
    {
        "slug": "MADIPAKKAM",
        "name": "GoGizmo Mobiles Shop - Madipakkam",
        "address": "27 Sabari Salai, Medavakkam Main Rd, Madipakkam, Chennai",
        "city": "Chennai",
        "pincode": "600091",
        "lat": 12.961135,
        "lng": 80.200129,
    },
]


def _resolve_company():
    if frappe.db.exists("Company", _DEFAULT_COMPANY):
        return _DEFAULT_COMPANY
    # Single-company tenancies often have exactly one row; pick it.
    rows = frappe.get_all("Company", pluck="name", limit=2)
    return rows[0] if len(rows) == 1 else None


def _ensure_city(city_name: str) -> str | None:
    if not city_name:
        return None
    if frappe.db.exists("CH City", city_name):
        return city_name
    # Don't auto-create cities here — that crosses an app boundary into
    # ch_item_master master data. Just skip the link, the row is still
    # valid without city.
    return None


def _find_existing(slug: str, branch: dict) -> str | None:
    canonical_code = f"GG-{slug}"
    existing = frappe.db.exists("CH Store", {"store_code": canonical_code})
    if existing:
        return existing
    for pattern in branch.get("match_existing_name_like") or []:
        row = frappe.db.get_value(
            "CH Store",
            {"store_name": ("like", pattern)},
            "name",
        )
        if row:
            return row
    return None


def execute():
    # Defensive: only run if v0_0_7 already added the geocode columns.
    if not frappe.db.has_column("CH Store", "latitude"):
        frappe.logger().warning(
            "ch_logistics v0_0_8: latitude column missing on CH Store; "
            "ensure ch_logistics.patches.v0_0_7.install_store_geo_fields "
            "ran first. Skipping Gogizmo seed."
        )
        return

    company = _resolve_company()
    if not company:
        frappe.logger().warning(
            "ch_logistics v0_0_8: could not determine default Company; "
            "skipping Gogizmo seed."
        )
        return

    now = frappe.utils.now_datetime()
    created = 0
    updated_geo = 0
    skipped = 0
    has_geocoded_at = frappe.db.has_column("CH Store", "geocoded_at")

    for branch in GOGIZMO_BRANCHES:
        slug = branch["slug"]
        canonical_code = f"GG-{slug}"
        existing_name = _find_existing(slug, branch)

        if existing_name:
            store_name = existing_name
            current = frappe.db.get_value(
                "CH Store",
                store_name,
                ["latitude", "longitude"],
                as_dict=True,
            ) or {}
            if current.get("latitude") or current.get("longitude"):
                skipped += 1
                continue
            updates = {
                "latitude": branch["lat"],
                "longitude": branch["lng"],
                "google_maps_url": f"https://maps.google.com/?q={branch['lat']},{branch['lng']}",
            }
            if has_geocoded_at:
                updates["geocoded_at"] = now
            frappe.db.set_value("CH Store", store_name, updates, update_modified=False)
            updated_geo += 1
            continue

        # Need to create a fresh CH Store.
        doc = frappe.new_doc("CH Store")
        doc.store_code = canonical_code
        doc.store_name = branch["name"]
        doc.company = company
        city = _ensure_city(branch.get("city"))
        if city:
            doc.city = city
        if branch.get("address"):
            doc.address = branch["address"]
        doc.state = "Tamil Nadu"
        if re.fullmatch(r"\d{6}", branch.get("pincode", "")):
            doc.pincode = branch["pincode"]
        doc.latitude = branch["lat"]
        doc.longitude = branch["lng"]
        doc.google_maps_url = f"https://maps.google.com/?q={branch['lat']},{branch['lng']}"
        if has_geocoded_at:
            doc.geocoded_at = now
        try:
            doc.insert(ignore_permissions=True)
            created += 1
        except Exception as exc:  # pragma: no cover — diagnostics only
            frappe.logger().warning(
                f"ch_logistics v0_0_8: could not create CH Store {canonical_code}: {exc}"
            )
            skipped += 1

    frappe.db.commit()
    frappe.logger().info(
        f"ch_logistics v0_0_8: Gogizmo seed complete — created={created} "
        f"geo_updated={updated_geo} skipped={skipped}"
    )
