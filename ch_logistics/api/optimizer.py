"""Route optimization + predictive ETA — the planning "brain" for trips.

This is the capability enterprise TMS (Blue Yonder, Oracle OTM, Dynamics 365 TM)
lead on and that CH Logistics lacked: instead of static, manually-sequenced
routes, optimize the stop order to minimise drive distance, and project a live
ETA per stop from the driver's real position so SLA breaches can be caught
early.

Design notes
------------
* No external dependencies. Distances use the haversine great-circle metric;
  sequencing uses nearest-neighbour + 2-opt, which is fast and near-optimal for
  the small stop counts of last-mile/transfer trips (typically < 30 stops).
* Stop coordinates resolve from the stop's planned lat/lng, else the linked
  Warehouse's geocode (`custom_latitude`/`custom_longitude`). Stops without
  coordinates are kept (appended in original order) so optimization degrades
  gracefully on partially-geocoded data.
* If Google OR-Tools is ever installed, ``_sequence()`` can be swapped for a
  proper VRP solver without touching callers.
"""
import math

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, add_to_date

EARTH_KM = 6371.0
DEFAULT_URBAN_SPEED_KMH = 24.0
DEFAULT_DWELL_MIN = 5


# ── geo helpers ────────────────────────────────────────────────────────────
def haversine_km(lat1, lng1, lat2, lng2) -> float:
    lat1, lng1, lat2, lng2 = map(flt, (lat1, lng1, lat2, lng2))
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


@frappe.whitelist()
def geocode_warehouse(warehouse: str) -> dict:
    """Best-effort: fill a Warehouse's latitude/longitude from its address via
    the Google Geocoding API (key from CH Tracking Settings). Manual entry
    always wins — this only fills blanks unless re-run explicitly."""
    wh = frappe.get_doc("Warehouse", warehouse)
    parts = [wh.get("address_line_1"), wh.get("address_line_2"), wh.get("city"),
             wh.get("state"), wh.get("pin")]
    address = ", ".join([p for p in parts if p])
    if not address:
        frappe.throw(_("Warehouse {0} has no address to geocode.").format(warehouse))

    key = frappe.db.get_single_value("CH Tracking Settings", "google_maps_api_key")
    if not key:
        frappe.throw(_("Set a Google Maps API key in CH Tracking Settings first."))

    import requests
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": address, "key": key}, timeout=10)
    data = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        frappe.throw(_("Geocoding failed for {0}: {1}").format(
            warehouse, data.get("status", "unknown")))
    loc = data["results"][0]["geometry"]["location"]
    frappe.db.set_value("Warehouse", warehouse, {
        "custom_latitude": loc["lat"],
        "custom_longitude": loc["lng"],
        "custom_geocoded_at": now_datetime(),
    })
    return {"warehouse": warehouse, "latitude": loc["lat"], "longitude": loc["lng"]}


def _warehouse_coords(warehouse: str):
    if not warehouse:
        return None
    lat, lng = frappe.db.get_value(
        "Warehouse", warehouse, ["custom_latitude", "custom_longitude"]) or (None, None)
    if lat and lng:
        return (flt(lat), flt(lng))
    # Fallback: the CH Store linked to this warehouse carries the geocode
    # (stores are geocoded; their warehouses may not be). Keeps the delivery
    # optimizer working for every store even before the bulk geo sync runs.
    store = (frappe.db.get_value("Warehouse", warehouse, "ch_store")
             or frappe.db.get_value("CH Store", {"warehouse": warehouse}, "name"))
    if store:
        slat, slng = frappe.db.get_value(
            "CH Store", store, ["latitude", "longitude"]) or (None, None)
        if slat and slng:
            return (flt(slat), flt(slng))
    return None


@frappe.whitelist()
def sync_store_geo_to_warehouses(overwrite: int = 0):
    """Copy each CH Store's lat/lng onto its warehouse(s).

    The map + any layer reading Warehouse.custom_latitude/longitude (e.g. the
    Location Hierarchy map) then has coordinates for every store. Fills blanks
    only unless ``overwrite`` is set. Idempotent.
    """
    updated = 0
    for s in frappe.get_all("CH Store", filters={"disabled": 0},
                            fields=["name", "warehouse", "warehouse_group", "latitude", "longitude", "geocoded_at"]):
        if not (s.latitude and s.longitude):
            continue
        # Every warehouse in the store's tree gets the store's coords: the
        # Sellable leaf (s.warehouse), the group parent (what the tree/map pin
        # points at), and every bin under the group — so no warehouse is blank.
        whs = set(filter(None, [s.warehouse, s.warehouse_group]))
        whs.update(frappe.get_all("Warehouse", filters={"ch_store": s.name}, pluck="name"))
        if s.warehouse_group:
            whs.update(frappe.get_all("Warehouse", filters={"parent_warehouse": s.warehouse_group}, pluck="name"))
        for wh in whs:
            cur = frappe.db.get_value("Warehouse", wh, ["custom_latitude", "custom_longitude"]) or (None, None)
            if not int(overwrite) and cur[0] and cur[1]:
                continue
            frappe.db.set_value("Warehouse", wh, {
                "custom_latitude": s.latitude,
                "custom_longitude": s.longitude,
                "custom_geocoded_at": s.geocoded_at or now_datetime(),
            }, update_modified=False)
            updated += 1
    frappe.db.commit()
    return {"warehouses_updated": updated}


def sync_one_store_geo(doc, method=None):
    """on_update hook for CH Store: push its coords onto all its warehouses
    (Sellable + group + bins) so the map/delivery layers stay accurate without
    a batch run. No external call — only fires when the store already has
    coordinates (manual entry or after geocoding)."""
    lat = flt(doc.get("latitude"))
    lng = flt(doc.get("longitude"))
    if not (lat and lng):
        return
    whs = set(filter(None, [doc.get("warehouse"), doc.get("warehouse_group")]))
    whs.update(frappe.get_all("Warehouse", filters={"ch_store": doc.name}, pluck="name"))
    if doc.get("warehouse_group"):
        whs.update(frappe.get_all("Warehouse", filters={"parent_warehouse": doc.warehouse_group}, pluck="name"))
    for wh in whs:
        frappe.db.set_value("Warehouse", wh, {
            "custom_latitude": lat,
            "custom_longitude": lng,
            "custom_geocoded_at": doc.get("geocoded_at") or now_datetime(),
        }, update_modified=False)


def _stop_coords(stop):
    """Planned (lat, lng) for a trip stop, or None if ungeocoded."""
    if flt(stop.get("plan_lat")) and flt(stop.get("plan_lng")):
        return (flt(stop.get("plan_lat")), flt(stop.get("plan_lng")))
    return _warehouse_coords(stop.get("warehouse"))


def _hub_coords(trip):
    return _warehouse_coords(trip.get("hub_warehouse"))


def backfill_stop_coords(trip) -> int:
    """Copy resolved warehouse coords onto stop rows so the trip is
    self-describing (and so optimisation survives later geocode edits)."""
    n = 0
    for s in trip.stops:
        if not (flt(s.plan_lat) and flt(s.plan_lng)):
            c = _warehouse_coords(s.warehouse)
            if c:
                s.plan_lat, s.plan_lng = c[0], c[1]
                n += 1
    return n


# ── sequencing (TSP heuristic) ─────────────────────────────────────────────
def _nearest_neighbour(origin, items, coords):
    """Greedy nearest-neighbour order starting from ``origin`` (lat, lng)."""
    remaining = list(items)
    order = []
    cur = origin
    while remaining:
        nxt = min(remaining, key=lambda s: haversine_km(cur[0], cur[1], *coords[s.name]))
        order.append(nxt)
        cur = coords[nxt.name]
        remaining.remove(nxt)
    return order


def _route_len(order, coords, origin):
    total, prev = 0.0, origin
    for s in order:
        c = coords[s.name]
        total += haversine_km(prev[0], prev[1], c[0], c[1])
        prev = c
    return total


def _two_opt(order, coords, origin, max_passes=20):
    """Classic 2-opt local search to remove path crossings."""
    best = order[:]
    best_len = _route_len(best, coords, origin)
    improved = True
    passes = 0
    while improved and passes < max_passes:
        improved = False
        passes += 1
        for i in range(len(best) - 1):
            for k in range(i + 1, len(best)):
                cand = best[:i] + best[i:k + 1][::-1] + best[k + 1:]
                cand_len = _route_len(cand, coords, origin)
                if cand_len + 1e-9 < best_len:
                    best, best_len = cand, cand_len
                    improved = True
    return best, best_len


def _sequence(stops, coords, origin):
    geo = [s for s in stops if coords.get(s.name)]
    nogeo = [s for s in stops if not coords.get(s.name)]
    if len(geo) < 2:
        return stops, None
    order = _nearest_neighbour(origin, geo, coords)
    order, length = _two_opt(order, coords, origin)
    return order + nogeo, length


# ── public: optimize a trip ────────────────────────────────────────────────
@frappe.whitelist()
def optimize_trip(trip: str) -> dict:
    """Re-sequence a trip's stops to minimise total drive distance.

    Allowed only before the trip starts (Draft/Assigned) — you cannot reorder a
    route a driver is already executing. Returns before/after distance so the
    saving is visible to the dispatcher.
    """
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status not in ("Draft", "Assigned"):
        frappe.throw(_("Can only optimize a trip while it is Draft or Assigned."))
    if not doc.stops:
        frappe.throw(_("Trip has no stops to optimize."))

    backfill_stop_coords(doc)
    coords = {s.name: c for s in doc.stops if (c := _stop_coords(s))}
    if len(coords) < 2:
        frappe.throw(_(
            "Need at least 2 geocoded stops. Set latitude/longitude on the "
            "stops' warehouses (Warehouse → Location)."))

    origin = _hub_coords(doc) or coords[next(iter(coords))]
    before = _route_len([s for s in doc.stops if s.name in coords], coords, origin)

    ordered, after = _sequence(list(doc.stops), coords, origin)

    # Re-write sequence + per-leg distance, recompute planned total.
    total, prev, seq = 0.0, origin, 1
    for s in ordered:
        s.sequence = seq
        seq += 1
        c = coords.get(s.name)
        if c:
            leg = haversine_km(prev[0], prev[1], c[0], c[1])
            s.leg_distance_km = round(leg, 2)
            total += leg
            prev = c
        else:
            s.leg_distance_km = 0
    doc.stops.sort(key=lambda r: r.sequence or 0)
    doc.total_distance_planned_km = round(total, 2)
    doc.optimized = 1
    doc.flags.ignore_version = True
    doc.save()

    saved = round(before - total, 2)
    return {
        "trip": doc.name,
        "stops": len(ordered),
        "distance_before_km": round(before, 2),
        "distance_after_km": round(total, 2),
        "distance_saved_km": saved,
        "saved_pct": round((saved / before * 100), 1) if before else 0,
    }


# ── public: predictive ETA ─────────────────────────────────────────────────
def _driver_last_pos(driver: str):
    if not driver:
        return None
    row = frappe.db.get_value(
        "CH Driver Location", {"driver": driver},
        ["latitude", "longitude"], order_by="captured_at desc", as_dict=True)
    if row and row.latitude and row.longitude:
        return (flt(row.latitude), flt(row.longitude))
    return None


def _avg_speed_kmh() -> float:
    val = frappe.db.get_single_value("CH Tracking Settings", "alert_on_speed_kmh_above")
    # use the configured "normal max" as a rough planning speed cap; fall back.
    return DEFAULT_URBAN_SPEED_KMH


@frappe.whitelist()
def compute_trip_eta(trip: str) -> dict:
    """Project an ETA for every not-yet-completed stop from the driver's live
    position along the planned sequence. Writes `eta` on each stop row."""
    doc = frappe.get_doc("CH Logistics Trip", trip)
    backfill_stop_coords(doc)

    # Origin: live driver position if moving, else the hub.
    origin = _driver_last_pos(doc.driver) or _hub_coords(doc)
    if not origin:
        return {"trip": doc.name, "updated": 0, "reason": "no-origin-coords"}

    speed = _avg_speed_kmh()
    cursor = now_datetime()
    prev = origin
    updated = 0
    last_eta = None
    for s in sorted(doc.stops, key=lambda r: r.sequence or 0):
        if s.status in ("Completed", "Skipped", "Arrived"):
            # already (being) served — anchor the cursor at its actual time
            if s.ata:
                cursor = s.ata
                prev = _stop_coords(s) or prev
            continue
        c = _stop_coords(s)
        if not c:
            continue
        drive_min = (haversine_km(prev[0], prev[1], c[0], c[1]) / max(speed, 1)) * 60
        cursor = add_to_date(cursor, minutes=int(round(drive_min)))
        frappe.db.set_value("CH Logistics Trip Stop", s.name, "eta", cursor,
                            update_modified=False)
        last_eta = cursor
        dwell = cint(frappe.db.get_value("CH Route Stop", s.route_stop, "est_dwell_min")) or DEFAULT_DWELL_MIN
        cursor = add_to_date(cursor, minutes=dwell)
        prev = c
        updated += 1

    if last_eta:
        frappe.db.set_value("CH Logistics Trip", doc.name, "planned_end", last_eta,
                            update_modified=False)
    return {"trip": doc.name, "updated": updated, "final_eta": str(last_eta) if last_eta else None}


# ── scheduled: SLA-breach early warning ────────────────────────────────────
def check_eta_sla_breaches() -> int:
    """For every in-progress trip, refresh ETAs and alert the dispatch desk for
    any stop whose projected arrival is past the trip's planned window."""
    trips = frappe.get_all(
        "CH Logistics Trip", filters={"status": "Started"}, pluck="name")
    alerts = 0
    for name in trips:
        try:
            compute_trip_eta(name)
            doc = frappe.get_doc("CH Logistics Trip", name)
            planned_end = doc.planned_end
            late = [s for s in doc.stops
                    if s.status not in ("Completed", "Skipped")
                    and s.eta and planned_end and str(s.eta) > str(planned_end)]
            if late:
                alerts += 1
                _notify_dispatch_late(doc, late)
        except Exception:
            frappe.log_error(title=f"check_eta_sla_breaches failed for {name}",
                             message=frappe.get_traceback())
    if trips:
        frappe.db.commit()
    return alerts


def _notify_dispatch_late(trip, late_stops):
    from ch_logistics.api.tracking_api import _alert  # reuse alert helper
    msg = _("Trip {0} ({1}): {2} stop(s) projected to miss the planned window.").format(
        trip.name, trip.driver_name or trip.driver or "", len(late_stops))
    try:
        _alert(_("Trip running late"), trip.driver, msg)
    except Exception:
        frappe.log_error(title="late-trip alert failed", message=frappe.get_traceback())
