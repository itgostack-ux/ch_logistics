"""End-to-end lifecycle test for the Operations stage strip.

Walks a small batch of CH Transfer Manifests through every stage of the
shipment journey:

    Draft Manifest  →  Packed  →  Trip Planned  →  In Transit  →  Delivered

After each transition, asserts that ``ops_lifecycle_counts`` reflects the
move correctly. The test compares DELTAs against a baseline snapshot so it
is safe to run on a populated database (it doesn't care what other docs
are in flight, only that *its* manifests show up in the right bucket).

Run with:
    bench --site erpnext.local execute \\
        ch_logistics.tests.test_lifecycle_e2e.run

Self-cleans via _teardown so re-runs are idempotent.
"""
from __future__ import annotations

import frappe
from frappe.utils import nowdate, now_datetime

from ch_logistics.api import logistics_api as api


_TAG = "LIFECYCLE-E2E"


# ---------------------------------------------------------------------------
# Fixtures (idempotent)
# ---------------------------------------------------------------------------

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
    m.status = "Draft"
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


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def _expect(condition, label):
    if condition:
        print(f"  PASS  {label}")
        return
    print(f"  FAIL  {label}")
    raise AssertionError(label)


def _counts(trip_date):
    """Read the lifecycle API and return a dict {key: count}."""
    payload = api.ops_lifecycle_counts(trip_date=trip_date, include_days=1)
    return {s["key"]: s["count"] for s in payload["stages"]}


def _move_manifest(name, status):
    # Direct DB set keeps us out of the heavy validate path (mandatory
    # packages, mandatory route etc.). We are explicitly testing the
    # counts API, not the manifest's own state machine.
    frappe.db.set_value("CH Transfer Manifest", name, "status", status,
                        update_modified=False)


def _move_trip(name, status):
    frappe.db.set_value("CH Logistics Trip", name, "status", status,
                        update_modified=False)


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

def run():
    print("== building fixtures ==")
    frappe.flags.in_test = True
    _teardown()
    try:
        abbr = frappe.get_cached_value("Company", _company(), "abbr")
        source_wh = _ensure_warehouse(f"{_TAG}-Hub", abbr)
        s1_wh = _ensure_warehouse(f"{_TAG}-S1", abbr)
        s2_wh = _ensure_warehouse(f"{_TAG}-S2", abbr)
        store_1 = _ensure_store(f"{_TAG}-Store1", s1_wh)
        store_2 = _ensure_store(f"{_TAG}-Store2", s2_wh)

        td = nowdate()
        baseline = _counts(td)
        print(f"  baseline counts: {baseline}")

        # ── STAGE 1 : Draft Manifests ────────────────────────────────
        print("== STAGE 1 — create Draft manifests ==")
        m_s1_a = _make_manifest(source_wh, s1_wh, store_1, 1)
        m_s1_b = _make_manifest(source_wh, s1_wh, store_1, 2)
        m_s2_a = _make_manifest(source_wh, s2_wh, store_2, 3)
        ours = [m_s1_a, m_s1_b, m_s2_a]

        c1 = _counts(td)
        _expect(c1["draft"] - baseline["draft"] == 3,
                f"draft delta = 3 (was {baseline['draft']}, now {c1['draft']})")
        _expect(c1["packed"] == baseline["packed"],
                "packed unchanged at Stage 1")
        _expect(c1["in_transit"] == baseline["in_transit"],
                "in_transit unchanged at Stage 1")

        # ── STAGE 2 : Packed ─────────────────────────────────────────
        print("== STAGE 2 — promote to Packed ==")
        for m in ours:
            _move_manifest(m, "Packed")
        c2 = _counts(td)
        _expect(c2["draft"] - baseline["draft"] == 0,
                "draft delta back to 0 after packing")
        _expect(c2["packed"] - baseline["packed"] == 3,
                f"packed delta = 3 (was {baseline['packed']}, now {c2['packed']})")
        _expect(c2["planned"] == baseline["planned"],
                "planned unchanged at Stage 2")

        # ── STAGE 3 : Trip Planned (clubbing) ────────────────────────
        print("== STAGE 3 — club into trip ==")
        result = api.club_transfers_into_trip(
            source_warehouse=source_wh,
            manifests=ours,
            trip_date=td,
            company=_company(),
        )
        trip = result["trip"]
        _expect(trip is not None, f"trip created: {trip}")
        _expect(len(result["stops"]) == 2,
                f"two stops created (got {len(result['stops'])})")

        # club_transfers_into_trip leaves the trip in Draft status. The
        # planned bucket = Draft + Assigned, so our trip lands there.
        c3 = _counts(td)
        _expect(c3["packed"] - baseline["packed"] == 0,
                "packed delta back to 0 once attached to a trip")
        _expect(c3["planned"] - baseline["planned"] >= 1,
                f"planned delta >= 1 (was {baseline['planned']}, now {c3['planned']})")

        # ── STAGE 3b : Driver Assigned ───────────────────────────────
        print("== STAGE 3b — mark Assigned (still in 'planned') ==")
        _move_trip(trip, "Assigned")
        c3b = _counts(td)
        _expect(c3b["planned"] - baseline["planned"] >= 1,
                "planned still includes the trip after Assigned status")
        _expect(c3b["in_transit"] == baseline["in_transit"],
                "in_transit unchanged until trip Started")

        # ── STAGE 4 : In Transit ─────────────────────────────────────
        print("== STAGE 4 — trip Started + manifests In Transit ==")
        _move_trip(trip, "Started")
        for m in ours:
            _move_manifest(m, "In Transit")
        c4 = _counts(td)
        _expect(c4["in_transit"] - baseline["in_transit"] >= 1,
                f"trip in in_transit (was {baseline['in_transit']}, now {c4['in_transit']})")
        _expect(c4["planned"] == baseline["planned"],
                "planned back to baseline once trip Started")

        # ── STAGE 5 : Delivered ──────────────────────────────────────
        print("== STAGE 5 — Completed + Delivered ==")
        _move_trip(trip, "Completed")
        for m in ours:
            _move_manifest(m, "Delivered")
        # delivery_datetime drives the "delivered today" manifest count.
        for m in ours:
            frappe.db.set_value("CH Transfer Manifest", m,
                                "delivery_datetime", now_datetime(),
                                update_modified=False)
        c5 = _counts(td)
        _expect(c5["in_transit"] == baseline["in_transit"],
                "in_transit back to baseline once trip Completed")
        _expect(c5["delivered"] - baseline["delivered"] >= 1,
                f"delivered delta >= 1 (was {baseline['delivered']}, now {c5['delivered']})")

        # ── STAGE 6 : Closed (still counts as delivered) ─────────────
        print("== STAGE 6 — Close trip (still under 'delivered') ==")
        _move_trip(trip, "Closed")
        c6 = _counts(td)
        _expect(c6["delivered"] - baseline["delivered"] >= 1,
                "delivered still includes the trip after Closed")

        print("\nALL LIFECYCLE STAGES PASSED")
        return {
            "ok": True,
            "trip": trip,
            "manifests": ours,
            "counts_by_stage": {
                "baseline": baseline,
                "stage1_draft": c1,
                "stage2_packed": c2,
                "stage3_planned": c3,
                "stage3b_assigned": c3b,
                "stage4_in_transit": c4,
                "stage5_delivered": c5,
                "stage6_closed": c6,
            },
        }
    finally:
        _teardown()
