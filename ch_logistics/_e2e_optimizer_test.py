"""E2E smoke test for route optimization + predictive ETA.

Run: bench --site erpnext.local execute ch_logistics._e2e_optimizer_test.run

Seeds warehouses with known coordinates, builds a deliberately bad stop order,
then asserts optimize_trip shortens the route and compute_trip_eta projects
monotonically-increasing arrival times. Cleans up after itself.
"""
import frappe

from ch_logistics.api import optimizer as opt

PASS, FAIL = [], []


def _ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ✓ " if cond else "  ✗ ") + label)


def _wh(name, company, lat, lng):
    full = f"{name} - E2EOPT"
    if not frappe.db.exists("Warehouse", {"warehouse_name": name, "company": company}):
        doc = frappe.get_doc({
            "doctype": "Warehouse", "warehouse_name": name, "company": company,
        }).insert(ignore_permissions=True)
    else:
        doc = frappe.get_doc("Warehouse", {"warehouse_name": name, "company": company})
    frappe.db.set_value("Warehouse", doc.name,
                        {"custom_latitude": lat, "custom_longitude": lng})
    return doc.name


def run():
    frappe.set_user("Administrator")
    company = frappe.db.get_single_value("Global Defaults", "default_company") \
        or frappe.db.get_value("Company", {}, "name")
    created_whs, trip_name = [], None
    try:
        # ── 0. Pure-algorithm sanity (no DB) ──────────────────────────
        print("\n[1] Algorithm")
        d = opt.haversine_km(13.0, 80.0, 13.0, 80.0)
        _ok(abs(d) < 1e-6, "haversine zero distance")
        d2 = opt.haversine_km(13.0, 80.0, 13.09, 80.0)  # ~10 km north
        _ok(9 < d2 < 11, f"haversine ~10km ({d2:.2f})")

        # ── 1. Seed warehouses: hub + two clusters (east, north) ──────
        print("\n[2] Seed geocoded warehouses")
        hub = _wh("E2EOPT Hub", company, 13.000, 80.000)
        a = _wh("E2EOPT A east-near", company, 13.000, 80.020)
        b = _wh("E2EOPT B east-far", company, 13.000, 80.090)
        c = _wh("E2EOPT C north-near", company, 13.020, 80.000)
        dd = _wh("E2EOPT D north-far", company, 13.090, 80.000)
        created_whs = [hub, a, b, c, dd]
        _ok(all(created_whs), "5 warehouses seeded with coords")

        # ── 2. Trip with a deliberately bad zig-zag stop order ────────
        print("\n[3] Trip + bad stop order")
        trip = frappe.get_doc({
            "doctype": "CH Logistics Trip",
            "trip_date": frappe.utils.nowdate(),
            "company": company,
            "hub_warehouse": hub,
            "status": "Draft",
            "direction": "Forward",
        })
        bad_order = [b, c, a, dd]  # far, near, near, far → back-and-forth
        for i, wh in enumerate(bad_order, start=1):
            trip.append("stops", {"sequence": i, "warehouse": wh,
                                   "stop_type": "Drop", "status": "Pending"})
        trip.insert(ignore_permissions=True)
        trip_name = trip.name
        _ok(len(trip.stops) == 4, "trip created with 4 stops")

        # ── 3. Optimize ──────────────────────────────────────────────
        print("\n[4] Optimize")
        res = opt.optimize_trip(trip_name)
        print("    ", res)
        _ok(res["distance_after_km"] <= res["distance_before_km"],
            f"optimized route not longer ({res['distance_before_km']}→{res['distance_after_km']} km)")
        _ok(res["distance_saved_km"] > 0, "optimization saved distance")

        reloaded = frappe.get_doc("CH Logistics Trip", trip_name)
        seqs = sorted(s.sequence for s in reloaded.stops)
        _ok(seqs == [1, 2, 3, 4], "stop sequence is contiguous 1..4")
        first_stop = min(reloaded.stops, key=lambda s: s.sequence)
        _ok(first_stop.warehouse == a, "nearest stop to hub (A) sequenced first")
        _ok(reloaded.optimized == 1 and reloaded.total_distance_planned_km > 0,
            "trip flagged optimized + planned distance set")

        # ── 4. Predictive ETA (hub-origin fallback) ──────────────────
        print("\n[5] Predictive ETA")
        eta_res = opt.compute_trip_eta(trip_name)
        print("    ", eta_res)
        _ok(eta_res["updated"] == 4, "ETA computed for all 4 stops")
        reloaded = frappe.get_doc("CH Logistics Trip", trip_name)
        etas = [s.eta for s in sorted(reloaded.stops, key=lambda s: s.sequence)]
        monotonic = all(etas[i] and etas[i + 1] and str(etas[i]) <= str(etas[i + 1])
                        for i in range(len(etas) - 1))
        _ok(monotonic, "stop ETAs are monotonically increasing")

    finally:
        frappe.set_user("Administrator")
        if trip_name and frappe.db.exists("CH Logistics Trip", trip_name):
            frappe.delete_doc("CH Logistics Trip", trip_name, force=True, ignore_permissions=True)
        for wh in created_whs:
            if wh and frappe.db.exists("Warehouse", wh):
                try:
                    frappe.delete_doc("Warehouse", wh, force=True, ignore_permissions=True)
                except Exception:
                    pass
        frappe.db.commit()

    print(f"\n==== RESULT: {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        for f in FAIL:
            print("   FAILED:", f)
        raise Exception(f"{len(FAIL)} optimizer e2e checks failed")
    return {"passed": len(PASS), "failed": len(FAIL)}
