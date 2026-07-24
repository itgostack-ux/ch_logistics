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
import json
import math

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, add_to_date

from ch_logistics.api.trip_lock import get_locked_trip

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


@frappe.whitelist(methods=["POST"])
def geocode_warehouse(warehouse: str) -> dict:
    """Best-effort: fill a Warehouse's latitude/longitude from its address via
    the Google Geocoding API (key from CH Tracking Settings). Manual entry
    always wins — this only fills blanks unless re-run explicitly."""
    from ch_logistics import roles as role_registry
    from ch_logistics import scope_guard

    role_registry.require("ops_control", _("geocode a warehouse"))
    wh = frappe.get_doc("Warehouse", warehouse)
    if not role_registry.is_privileged():
        wh.check_permission("write")
    scope_guard.assert_scope(
        store=wh.get("ch_store"),
        warehouse=wh.name,
        company=wh.get("company"),
    )
    parts = [wh.get("address_line_1"), wh.get("address_line_2"), wh.get("city"),
             wh.get("state"), wh.get("pin")]
    address = ", ".join([p for p in parts if p])
    if not address:
        frappe.throw(_("Warehouse {0} has no address to geocode.").format(warehouse))

    from ch_logistics.logistics.doctype.ch_tracking_settings.ch_tracking_settings import (
        get_google_maps_api_key,
    )

    key = get_google_maps_api_key()
    if not key:
        frappe.throw(_("Set a Google Maps API key in CH Tracking Settings first."))

    import requests
    timeout = min(role_registry.get_int_setting("geocode_api_timeout_seconds", 10), 15)
    max_bytes = min(
        role_registry.get_int_setting("geocode_api_max_response_bytes", 262144),
        1048576,
    )
    resp = None
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": key},
            timeout=(min(timeout, 5), timeout),
            allow_redirects=False,
            stream=True,
        )
        if 300 <= resp.status_code < 400:
            frappe.throw(_("Google geocoding redirects are not permitted."))
        resp.raise_for_status()
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            frappe.throw(_("Geocoding response exceeds the configured size limit."))
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=min(max_bytes, 65536)):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_bytes:
                frappe.throw(_("Geocoding response exceeds the configured size limit."))
        data = json.loads(bytes(body).decode(resp.encoding or "utf-8", errors="replace"))
    except (requests.RequestException, ValueError) as exc:
        frappe.throw(_("Geocoding provider returned an invalid response: {0}").format(str(exc)[:160]))
    finally:
        if resp is not None:
            resp.close()
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


def _warehouse_coords_map(warehouses) -> dict[str, tuple[float, float]]:
    """Resolve Warehouse coordinates, including CH Store fallback, in two reads."""
    names = list(dict.fromkeys(name for name in warehouses if name))
    if not names:
        return {}
    warehouse_rows = frappe.get_all(
        "Warehouse",
        filters={"name": ["in", names]},
        fields=["name", "custom_latitude", "custom_longitude", "ch_store"],
    )
    coords = {
        row.name: (flt(row.custom_latitude), flt(row.custom_longitude))
        for row in warehouse_rows
        if row.custom_latitude and row.custom_longitude
    }
    unresolved = set(names) - set(coords)
    if not unresolved:
        return coords

    store_names = {row.ch_store for row in warehouse_rows if row.ch_store and row.name in unresolved}
    store_rows = frappe.get_all(
        "CH Store",
        filters=[
            ["CH Store", "disabled", "=", 0],
            ["CH Store", "name", "in", sorted(store_names)],
        ],
        fields=["name", "warehouse", "latitude", "longitude"],
    ) if store_names else []
    linked_warehouses = {row.warehouse for row in store_rows if row.warehouse}
    missing_direct = unresolved - linked_warehouses
    if missing_direct:
        store_rows.extend(frappe.get_all(
            "CH Store",
            filters={"disabled": 0, "warehouse": ["in", sorted(missing_direct)]},
            fields=["name", "warehouse", "latitude", "longitude"],
        ))
    store_by_name = {row.name: row for row in store_rows}
    store_by_warehouse = {row.warehouse: row for row in store_rows if row.warehouse}
    for row in warehouse_rows:
        if row.name not in unresolved:
            continue
        store = store_by_name.get(row.ch_store) or store_by_warehouse.get(row.name)
        if store and store.latitude and store.longitude:
            coords[row.name] = (flt(store.latitude), flt(store.longitude))
    return coords


def _warehouse_coords(warehouse: str):
    return _warehouse_coords_map([warehouse]).get(warehouse) if warehouse else None


@frappe.whitelist(methods=["POST"])
def sync_store_geo_to_warehouses(overwrite: int = 0):
    """Copy each CH Store's lat/lng onto its warehouse(s).

    The map + any layer reading Warehouse.custom_latitude/longitude (e.g. the
    Location Hierarchy map) then has coordinates for every store. Fills blanks
    only unless ``overwrite`` is set. Idempotent.
    """
    from ch_logistics import roles as role_registry
    from ch_logistics import scope_guard

    role_registry.require("ops_control", _("synchronize store coordinates"))
    if not role_registry.is_privileged() and not frappe.has_permission("Warehouse", "write"):
        frappe.throw(_("You do not have permission to update warehouses."), frappe.PermissionError)

    scope = scope_guard._scope()
    batch_limit = role_registry.get_int_setting("geo_sync_store_batch_size", 500)
    warehouse_limit = role_registry.get_int_setting("geo_sync_warehouse_batch_size", 5000)
    store_filters = {"disabled": 0}
    store_or_filters = None
    if not scope.get("bypass"):
        scoped_stores = sorted(scope.get("stores") or ())
        scoped_warehouses = sorted(scope.get("warehouses") or ())
        if not scoped_stores and not scoped_warehouses:
            return {"warehouses_updated": 0}
        store_or_filters = {
            "name": ["in", scoped_stores or ["__none__"]],
            "warehouse": ["in", scoped_warehouses or ["__none__"]],
        }
    stores = [
        row for row in frappe.get_all(
            "CH Store",
            filters=store_filters,
            or_filters=store_or_filters,
            fields=["name", "warehouse", "warehouse_group", "latitude", "longitude", "geocoded_at"],
            limit_page_length=batch_limit,
        )
        if row.latitude and row.longitude
        and scope_guard.is_in_scope(store=row.name, warehouse=row.warehouse)
    ]
    store_by_name = {row.name: row for row in stores}
    group_to_store = {row.warehouse_group: row for row in stores if row.warehouse_group}
    warehouse_to_store = {}
    for store in stores:
        for warehouse in (store.warehouse, store.warehouse_group):
            if warehouse:
                warehouse_to_store[warehouse] = store
    if store_by_name:
        for row in frappe.get_all(
            "Warehouse",
            filters={"ch_store": ["in", sorted(store_by_name)]},
            fields=["name", "ch_store"],
            limit_page_length=warehouse_limit,
        ):
            warehouse_to_store[row.name] = store_by_name[row.ch_store]
    if group_to_store:
        for row in frappe.get_all(
            "Warehouse",
            filters={"parent_warehouse": ["in", sorted(group_to_store)]},
            fields=["name", "parent_warehouse"],
            limit_page_length=warehouse_limit,
        ):
            warehouse_to_store[row.name] = group_to_store[row.parent_warehouse]

    current = {
        row.name: row
        for row in frappe.get_all(
            "Warehouse",
            filters={"name": ["in", sorted(warehouse_to_store)]},
            fields=["name", "custom_latitude", "custom_longitude"],
            limit_page_length=warehouse_limit,
        )
    } if warehouse_to_store else {}
    updates = {}
    for warehouse, store in warehouse_to_store.items():
        row = current.get(warehouse)
        if not int(overwrite) and row and row.custom_latitude and row.custom_longitude:
            continue
        updates[warehouse] = {
            "custom_latitude": store.latitude,
            "custom_longitude": store.longitude,
            "custom_geocoded_at": store.geocoded_at or now_datetime(),
        }
    frappe.db.bulk_update("Warehouse", updates, update_modified=False)
    return {"warehouses_updated": len(updates)}


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
    updates = {
        wh: {
            "custom_latitude": lat,
            "custom_longitude": lng,
            "custom_geocoded_at": doc.get("geocoded_at") or now_datetime(),
        }
        for wh in whs
    }
    frappe.db.bulk_update("Warehouse", updates, update_modified=False)


def _stop_coords(stop, warehouse_coords=None):
    """Planned (lat, lng) for a trip stop, or None if ungeocoded."""
    if flt(stop.get("plan_lat")) and flt(stop.get("plan_lng")):
        return (flt(stop.get("plan_lat")), flt(stop.get("plan_lng")))
    warehouse = stop.get("warehouse")
    if warehouse_coords is not None:
        return warehouse_coords.get(warehouse)
    return _warehouse_coords(warehouse)


def _hub_coords(trip, warehouse_coords=None):
    warehouse = trip.get("hub_warehouse")
    if warehouse_coords is not None:
        return warehouse_coords.get(warehouse)
    return _warehouse_coords(warehouse)


def backfill_stop_coords(trip) -> int:
    """Copy resolved warehouse coords onto stop rows so the trip is
    self-describing (and so optimisation survives later geocode edits)."""
    n = 0
    warehouse_coords = _warehouse_coords_map(
        s.warehouse
        for s in trip.stops
        if s.warehouse and not (flt(s.plan_lat) and flt(s.plan_lng))
    )
    for s in trip.stops:
        if not (flt(s.plan_lat) and flt(s.plan_lng)):
            c = warehouse_coords.get(s.warehouse)
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
    """Open-route 2-opt using edge deltas instead of remeasuring each route."""
    best = order[:]
    for _ in range(max_passes):
        improved = False
        for i in range(len(best) - 1):
            previous = origin if i == 0 else coords[best[i - 1].name]
            for k in range(i + 1, len(best)):
                first = coords[best[i].name]
                last = coords[best[k].name]
                old_edges = haversine_km(previous[0], previous[1], first[0], first[1])
                new_edges = haversine_km(previous[0], previous[1], last[0], last[1])
                if k + 1 < len(best):
                    following = coords[best[k + 1].name]
                    old_edges += haversine_km(last[0], last[1], following[0], following[1])
                    new_edges += haversine_km(first[0], first[1], following[0], following[1])
                delta = new_edges - old_edges
                if delta < -1e-9:
                    best[i:k + 1] = reversed(best[i:k + 1])
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break
    return best, _route_len(best, coords, origin)


def _sequence(stops, coords, origin, max_passes=20):
    geo = [s for s in stops if coords.get(s.name)]
    nogeo = [s for s in stops if not coords.get(s.name)]
    if len(geo) < 2:
        return stops, None
    order = _nearest_neighbour(origin, geo, coords)
    order, length = _two_opt(order, coords, origin, max_passes=max_passes)
    return order + nogeo, length


def _optimizer_limits(stop_count: int) -> int:
    from ch_logistics.roles import get_int_setting

    max_stops = get_int_setting("optimizer_max_stops", 250)
    if stop_count > max_stops:
        frappe.throw(
            _("A maximum of {0} stops can be optimized at once.").format(max_stops)
        )
    return get_int_setting("optimizer_max_passes", 10)


# ── public: optimize a trip ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
def optimize_trip(trip: str) -> dict:
    """Re-sequence a trip's stops to minimise total drive distance.

    Allowed only before the trip starts (Draft/Assigned) — you cannot reorder a
    route a driver is already executing. Returns before/after distance so the
    saving is visible to the dispatcher.
    """
    from ch_logistics import roles as role_registry
    from ch_logistics import scope_guard

    role_registry.require("ops_control", _("optimize a trip"))
    doc = get_locked_trip(trip)
    doc.check_permission("write")
    scope_guard.assert_trip_scope(doc.as_dict())
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

    ordered, after = _sequence(
        list(doc.stops), coords, origin, max_passes=_optimizer_limits(len(doc.stops))
    )

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


@frappe.whitelist(methods=["POST"])
def resequence_trip(trip: str) -> dict:
    """Controlled resequencing with audit trail.

    - Draft / Assigned: delegates to ``optimize_trip``.
    - Started: resequences only remaining stops (Pending / Exception), keeps
      reached stops (Arrived / Completed / Skipped) immutable.
    """
    from ch_logistics import scope_guard

    doc = get_locked_trip(trip)
    doc.check_permission("write")
    scope_guard.assert_trip_scope(doc.as_dict())
    if doc.status in ("Draft", "Assigned"):
        out = optimize_trip(trip)
        doc = frappe.get_doc("CH Logistics Trip", trip)
        try:
            doc.add_comment(
                "Comment",
                _("Trip resequenced by {0} using optimizer (status: {1}).").format(
                    frappe.session.user,
                    doc.status,
                ),
            )
        except Exception:
            frappe.log_error(title=f"resequence comment failed for {trip}", message=frappe.get_traceback())
        return out

    if doc.status != "Started":
        frappe.throw(_("Can resequence only Draft/Assigned/Started trips."))

    from ch_logistics import roles as role_registry
    role_registry.require("resequence_override", frappe._("resequence a Started trip"))

    reached = [
        s for s in (doc.stops or [])
        if (s.get("status") or "") in ("Arrived", "Completed", "Skipped")
    ]
    movable = [
        s for s in (doc.stops or [])
        if (s.get("status") or "") not in ("Arrived", "Completed", "Skipped")
    ]
    if len(movable) < 2:
        frappe.throw(_("Need at least two remaining stops to resequence."))

    backfill_stop_coords(doc)
    coords = {s.name: c for s in movable if (c := _stop_coords(s))}
    if len(coords) < 2:
        frappe.throw(_("Need at least two geocoded remaining stops to resequence."))

    origin = _driver_last_pos(doc.driver)
    if not origin and reached:
        reached_sorted = sorted(reached, key=lambda r: r.sequence or 0)
        origin = _stop_coords(reached_sorted[-1])
    origin = origin or _hub_coords(doc) or coords[next(iter(coords))]

    before = _route_len([s for s in movable if s.name in coords], coords, origin)
    ordered, _ = _sequence(
        list(movable), coords, origin, max_passes=_optimizer_limits(len(movable))
    )

    seq = (max([cint(s.sequence) for s in reached], default=0) + 1)
    total, prev = 0.0, origin
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
    doc.flags.ignore_version = False
    doc.save()

    after = _route_len([s for s in ordered if s.name in coords], coords, origin)
    saved = round(before - after, 2)
    try:
        doc.add_comment(
            "Comment",
            _(
                "Started-trip resequence by {0}: remaining stops {1}, distance {2}km -> {3}km (saved {4}km)."
            ).format(
                frappe.session.user,
                len(ordered),
                round(before, 2),
                round(after, 2),
                saved,
            ),
        )
    except Exception:
        frappe.log_error(title=f"resequence comment failed for {trip}", message=frappe.get_traceback())

    return {
        "trip": doc.name,
        "status": doc.status,
        "mode": "started_remaining_only",
        "stops_resequenced": len(ordered),
        "distance_before_km": round(before, 2),
        "distance_after_km": round(after, 2),
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
    from ch_logistics.roles import get_int_setting

    return flt(
        get_int_setting("eta_average_speed_kmh", int(DEFAULT_URBAN_SPEED_KMH))
    )


@frappe.whitelist(methods=["POST"])
def compute_trip_eta(trip: str) -> dict:
    """Project an ETA for every not-yet-completed stop from the driver's live
    position along the planned sequence. Writes `eta` on each stop row."""
    from ch_logistics.api.driver_resolver import assert_trip_driver_access

    doc = get_locked_trip(trip)
    doc.check_permission("write")
    assert_trip_driver_access(doc, override_key="tracking_view")
    from ch_logistics.roles import get_int_setting

    stop_limit = min(get_int_setting("optimizer_max_stops", 50), 500)
    if len(doc.stops or []) > stop_limit:
        frappe.throw(
            _("Trip {0} exceeds the configured {1}-stop optimizer limit.").format(
                doc.name, stop_limit
            ),
            frappe.ValidationError,
        )
    backfill_stop_coords(doc)

    # Origin: live driver position if moving, else the hub.
    origin = _driver_last_pos(doc.driver) or _hub_coords(doc)
    if not origin:
        return {"trip": doc.name, "updated": 0, "reason": "no-origin-coords"}

    speed = _avg_speed_kmh()
    route_stop_names = {
        s.route_stop for s in doc.stops if s.route_stop
    }
    dwell_by_stop = {
        row.name: cint(row.est_dwell_min) or _default_dwell_minutes()
        for row in frappe.get_all(
            "CH Route Stop",
            filters={"name": ["in", sorted(route_stop_names)]},
            fields=["name", "est_dwell_min"],
            limit_page_length=stop_limit,
        )
    } if route_stop_names else {}
    cursor = now_datetime()
    prev = origin
    updated = 0
    last_eta = None
    eta_updates = {}
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
        eta_updates[s.name] = {"eta": cursor}
        last_eta = cursor
        dwell = dwell_by_stop.get(s.route_stop, _default_dwell_minutes())
        cursor = add_to_date(cursor, minutes=dwell)
        prev = c
        updated += 1

    frappe.db.bulk_update(
        "CH Logistics Trip Stop", eta_updates, update_modified=False
    )
    if last_eta:
        frappe.db.set_value("CH Logistics Trip", doc.name, "planned_end", last_eta,
                            update_modified=False)
    return {"trip": doc.name, "updated": updated, "final_eta": str(last_eta) if last_eta else None}


# ── scheduled: SLA-breach early warning ────────────────────────────────────
def check_eta_sla_breaches() -> int:
    """For every in-progress trip, refresh ETAs and alert the dispatch desk for
    any stop whose projected arrival is past the trip's planned window."""
    from ch_logistics.roles import get_name_batch

    trip_rows = get_name_batch(
        "CH Logistics Trip",
        filters=[["status", "=", "Started"]],
        fields=["name"],
        cursor_key="eta_sla_trips",
        limit_field="eta_sla_trip_batch_size",
        default_limit=500,
    )
    alerts = 0
    for row in trip_rows:
        name = row.name
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
    return alerts


def _default_dwell_minutes() -> int:
    from ch_logistics.roles import get_int_setting

    return get_int_setting("eta_default_dwell_minutes", DEFAULT_DWELL_MIN)


def _notify_dispatch_late(trip, late_stops):
    from ch_logistics.api.tracking_api import _alert  # reuse alert helper
    msg = _("Trip {0} ({1}): {2} stop(s) projected to miss the planned window.").format(
        trip.name, trip.driver_name or trip.driver or "", len(late_stops))
    try:
        _alert(_("Trip running late"), trip.driver, msg)
    except Exception:
        frappe.log_error(title="late-trip alert failed", message=frappe.get_traceback())
