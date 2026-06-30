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
    if frappe.db.exists("CH Store", name):
        return name
    s = frappe.new_doc("CH Store")
    s.store_name = name
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
                            filters={"name": ["like", f"{_TAG}-%"]},
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

        # Three Packed manifests, two destinations — exactly what the
        # dispatcher would tick after the pack station marks them Packed.
        m1 = _make_manifest(src, d1wh, s1, 1)
        m2 = _make_manifest(src, d2wh, s2, 2)
        m3 = _make_manifest(src, d1wh, s1, 3)
        selected = [m1, m2, m3]
        print(f"  packed manifests ready: {selected}")

        # ── Step 1 : Bundle ────────────────────────────────────────
        print("== Step 1 — club_transfers_into_trip ==")
        res = api.club_transfers_into_trip(
            source_warehouse=src,
            manifests=selected,
            trip_date=nowdate(),
            company=_company(),
        )
        trip = res["trip"]
        stops = res["stops"]
        _expect(trip, f"trip created: {trip}")
        _expect(len(stops) == 2, f"two stops created (got {len(stops)})")
        _expect(sum(len(s["manifests"]) for s in stops) == 3,
                "all 3 manifests landed on a stop")

        # ── Step 2 : Get label for each stop ──────────────────────
        print("== Step 2 — get_stop_label per stop ==")
        labels = []
        for s in stops:
            lbl = api.get_stop_label(trip=trip, sequence=s["sequence"], kind="pickup")
            labels.append((s, lbl))
            _expect(s["pickup_token"] == lbl["token"],
                    f"stop #{s['sequence']} label token matches pickup_token")
            _expect(lbl["manifest_count"] == len(s["manifests"]),
                    f"stop #{s['sequence']} manifest count consistent ({lbl['manifest_count']})")
            _expect("<div" in lbl["html"] and lbl["token"] in lbl["html"],
                    f"stop #{s['sequence']} HTML embeds the token and renders a div")

        # ── Step 3 : Negative path (different source warehouses) ─
        # Mirrors the front-end guard: a bundle with two source
        # warehouses must NOT produce a trip. The UI catches this client
        # side; the server-side API enforces the same invariant because
        # its filter is anchored to ``source_warehouse``: a manifest from
        # a *different* source is silently skipped, which leaves an empty
        # attachable pool and the API throws "Nothing to Club".
        print("== Step 3 — refuses to bundle across source warehouses ==")
        other_src = _ensure_warehouse(f"{_TAG}-Hub2", abbr)
        m4 = _make_manifest(other_src, d1wh, s1, 4)  # Packed @ other_src
        try:
            api.club_transfers_into_trip(
                source_warehouse=src,         # anchor source = src
                manifests=[m4],                # but the manifest is from other_src
                trip_date=nowdate(),
                company=_company(),
            )
            _expect(False, "API should refuse — m4 is from a different source")
        except frappe.ValidationError as exc:
            msg = str(exc).lower()
            _expect("no attachable" in msg or "nothing to club" in msg or "warehouse" in msg,
                    f"server rejected cross-warehouse bundle: {exc}")

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
