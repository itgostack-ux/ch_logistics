"""End-to-end test for the "Bundle & Print Pickup QR" action.

Mirrors exactly what the front-end button does when the user ticks
multiple Packed manifests in Operations and clicks "Bundle & Print
Pickup QR":

    1. club_transfers_into_trip(source_warehouse, manifests=[...]).
    2. For each stop returned, call get_stop_label(trip, sequence,
       kind="pickup").
    3. Assert every label embeds the right pickup_token and has the
       expected printable HTML structure.

Run with:
    bench --site erpnext.local execute \\
        ch_logistics.tests.test_bundle_pickup_qr_ui_flow.run

Self-cleans via _teardown so re-runs are idempotent.
"""
from __future__ import annotations

import frappe
from frappe.utils import nowdate

from ch_logistics.api import logistics_api as api


_TAG = "BUNDLE-UI-E2E"


def _company():
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name")
    if not company:
        raise RuntimeError("No Company exists; create one before running this test.")
    return company


def _ensure_warehouse(name, abbr):
    full = f"{name} - {abbr}"
    if frappe.db.exists("Warehouse", full):
        return full
    wh = frappe.new_doc("Warehouse")
    wh.warehouse_name = name
    wh.company = _company()
    wh.is_group = 0
    wh.insert(ignore_permissions=True)
    return wh.name


def _ensure_store(name, warehouse):
    # CH Store autonames by store code (e.g. GG-BUNDLEUIE2ESTORE1) — look
    # up by store_name, not by document name.
    existing = frappe.db.get_value("CH Store", {"store_name": name, "company": _company()})
    if existing:
        return existing
    s = frappe.new_doc("CH Store")
    s.store_name = name
    s.company = _company()
    s.warehouse = warehouse
    s.insert(ignore_permissions=True)
    return s.name


def _make_manifest(source_wh, dest_wh, dest_store, idx):
    m = frappe.new_doc("CH Transfer Manifest")
    m.company = _company()
    m.posting_date = nowdate()
    m.source_warehouse = source_wh
    m.destination_warehouse = dest_wh
    m.destination_store = dest_store
    m.status = "Packed"
    m.qr_payload = f"{_TAG}-M{idx}"
    m.flags.ignore_validate = True
    m.flags.ignore_mandatory = True
    m.insert(ignore_permissions=True)
    return m.name


def _teardown():
    for m in frappe.get_all("CH Transfer Manifest",
                            filters={"qr_payload": ["like", f"{_TAG}-%"]},
                            pluck="name"):
        try:
            frappe.delete_doc("CH Transfer Manifest", m, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    for t in frappe.get_all("CH Logistics Trip",
                            filters={"hub_warehouse": ["like", f"%{_TAG}-Hub%"]},
                            pluck="name"):
        try:
            frappe.delete_doc("CH Logistics Trip", t, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    for s in frappe.get_all("CH Store",
                            filters={"store_name": ["like", f"{_TAG}-%"]},
                            pluck="name"):
        try:
            frappe.delete_doc("CH Store", s, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    for w in frappe.get_all("Warehouse",
                            filters={"warehouse_name": ["like", f"{_TAG}-%"]},
                            pluck="name"):
        try:
            frappe.delete_doc("Warehouse", w, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    frappe.db.commit()


def _expect(condition, label):
    if condition:
        print(f"  PASS  {label}")
        return
    print(f"  FAIL  {label}")
    raise AssertionError(label)


def run():
    print("== building fixtures ==")
    frappe.flags.in_test = True
    _teardown()
    try:
        abbr = frappe.get_cached_value("Company", _company(), "abbr")
        src  = _ensure_warehouse(f"{_TAG}-Hub", abbr)
        d1wh = _ensure_warehouse(f"{_TAG}-S1", abbr)
        d2wh = _ensure_warehouse(f"{_TAG}-S2", abbr)
        s1   = _ensure_store(f"{_TAG}-Store1", d1wh)
        s2   = _ensure_store(f"{_TAG}-Store2", d2wh)

        # Three Packed manifests, ALL going to the SAME destination —
        # exactly what the bundle button now requires (same source +
        # same destination = one pickup QR + one delivery QR).
        m1 = _make_manifest(src, d1wh, s1, 1)
        m2 = _make_manifest(src, d1wh, s1, 2)
        m3 = _make_manifest(src, d1wh, s1, 3)
        selected = [m1, m2, m3]
        print(f"  packed manifests ready (same dest): {selected}")

        # ── Step 1 : Bundle ────────────────────────────────────────
        print("== Step 1 — club_transfers_into_trip (enforce_single_destination=1) ==")
        res = api.club_transfers_into_trip(
            source_warehouse=src,
            manifests=selected,
            trip_date=nowdate(),
            company=_company(),
            enforce_single_destination=1,
        )
        trip = res["trip"]
        stops = res["stops"]
        _expect(trip, f"trip created: {trip}")
        _expect(len(stops) == 1, f"exactly ONE stop created (got {len(stops)})")
        _expect(len(stops[0]["manifests"]) == 3,
                "all 3 manifests landed on the single stop")
        _expect(stops[0]["store"] == s1,
                f"stop is the shared destination store ({s1})")

        # ── Step 2 : Get label for the stop ───────────────────────
        print("== Step 2 — get_stop_label for the consolidated stop ==")
        s = stops[0]
        lbl = api.get_stop_label(trip=trip, sequence=s["sequence"], kind="pickup")
        _expect(s["pickup_token"] == lbl["token"],
                "label token matches stop.pickup_token")
        _expect(lbl["manifest_count"] == 3,
                f"label manifest_count == 3 (got {lbl['manifest_count']})")
        _expect("<div" in lbl["html"] and lbl["token"] in lbl["html"],
                "label HTML embeds the token and renders a div")

        # ── Step 3 : Negative path — cross-source bundle is refused ─
        # Mirrors the UI guard for source warehouse: a manifest from a
        # different source warehouse is silently skipped by the filter,
        # which leaves an empty attachable pool and the API throws
        # "Nothing to Club".
        print("== Step 3 — refuses to bundle across source warehouses ==")
        other_src = _ensure_warehouse(f"{_TAG}-Hub2", abbr)
        m4 = _make_manifest(other_src, d1wh, s1, 4)
        try:
            api.club_transfers_into_trip(
                source_warehouse=src,
                manifests=[m4],
                trip_date=nowdate(),
                company=_company(),
                enforce_single_destination=1,
            )
            _expect(False, "API should refuse — m4 is from a different source")
        except frappe.ValidationError as exc:
            msg = str(exc).lower()
            _expect("no attachable" in msg or "nothing to club" in msg or "warehouse" in msg,
                    f"server rejected cross-warehouse bundle: {exc}")

        # ── Step 4 : Negative path — mixed destinations is refused ─
        # This is the server-side mirror of the new client-side guard:
        # when enforce_single_destination=1, the API must throw before
        # any trip/stop is created.
        print("== Step 4 — refuses to bundle across destination stores ==")
        m5 = _make_manifest(src, d1wh, s1, 5)
        m6 = _make_manifest(src, d2wh, s2, 6)
        try:
            api.club_transfers_into_trip(
                source_warehouse=src,
                manifests=[m5, m6],
                trip_date=nowdate(),
                company=_company(),
                enforce_single_destination=1,
            )
            _expect(False, "API should refuse — m5 and m6 have different destinations")
        except frappe.ValidationError as exc:
            msg = str(exc).lower()
            _expect("cannot bundle" in msg or "mixed destinations" in msg
                    or "different destinations" in msg or "destination" in msg,
                    f"server rejected mixed-destination bundle: {exc}")
        # And ensure neither m5 nor m6 was attached to any trip.
        residue = frappe.get_all("CH Transfer Manifest",
                                 filters={"name": ["in", [m5, m6]]},
                                 fields=["name", "trip"])
        _expect(all((r.trip in (None, "")) for r in residue),
                "no trip was created for the refused bundle")

        print("\nALL BUNDLE-FLOW ASSERTIONS PASSED")
        return {
            "ok": True,
            "trip": trip,
            "stops": [{"sequence": s["sequence"],
                        "store": s["store"],
                        "manifests": s["manifests"],
                        "pickup_token": s["pickup_token"]} for s in stops],
        }
    finally:
        _teardown()
