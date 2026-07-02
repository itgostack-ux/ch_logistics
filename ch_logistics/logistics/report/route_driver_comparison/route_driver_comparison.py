"""Route × Driver Comparison — apples-to-apples per-route driver benchmark.

Purpose
-------

The Driver Performance Scorecard answers "who is my best driver overall?".
That's biased by route mix — a driver who only runs city-loop routes will
always beat one who runs long-haul, even if the long-hauler is objectively
faster per km.  This report rebases the comparison by (route, driver): for
every route that has been run by 2+ drivers over the filter window, we
show one row per driver-on-that-route with:

* trip count on that route
* planned vs actual km
* on-time delivery % (from linked manifests)
* exception count
* average trip duration

Ops managers use this to identify route-specific coaching opportunities —
e.g. "Route CH-South-Loop-3 takes driver A 4h but driver B 6h — pair B
with A for one ride-along".

Design parity: SAP TM Driver-vs-Route benchmark, Oracle OTM Driver
Efficiency by Lane, D365 Route Utilisation by Driver.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ch_erp15.ch_erp15.report_scope import scope_where_clause
from ch_logistics.api.report_utils import col, resolve_company


def _trip_scope(alias: str = "t") -> str:
    """Fail-closed scope on trip.hub_warehouse."""
    return scope_where_clause(warehouse_field=f"{alias}.hub_warehouse")


def _manifest_scope(alias: str = "m") -> str:
    """Fail-closed scope on any endpoint of CH Transfer Manifest."""
    return scope_where_clause(
        warehouse_field=f"{alias}.source_warehouse",
        extra_warehouse_fields=(f"{alias}.destination_warehouse",),
        store_field=f"{alias}.source_store",
        extra_store_fields=(f"{alias}.destination_store",),
    )


def _trip_where(filters: dict) -> tuple[str, dict]:
    """Common WHERE fragment for CH Logistics Trip rows."""
    cond: list[str] = []
    vals: dict[str, object] = {}
    company = resolve_company(filters)
    if company:
        cond.append("t.company = %(company)s")
        vals["company"] = company
    if filters.get("from_date"):
        cond.append("t.trip_date >= %(from_date)s")
        vals["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        cond.append("t.trip_date <= %(to_date)s")
        vals["to_date"] = filters["to_date"]
    if filters.get("driver"):
        cond.append("t.driver = %(driver)s")
        vals["driver"] = filters["driver"]
    if filters.get("route"):
        cond.append("t.route = %(route)s")
        vals["route"] = filters["route"]
    if filters.get("hub_warehouse"):
        cond.append("t.hub_warehouse = %(hub_warehouse)s")
        vals["hub_warehouse"] = filters["hub_warehouse"]
    scope = _trip_scope("t")
    if scope is not None:
        cond.append(scope)
    return (" AND " + " AND ".join(cond)) if cond else "", vals


def _fetch_trip_aggregates(filters: dict) -> list[dict]:
    """One row per (route, driver) — trip counts + distance + duration."""
    where, vals = _trip_where(filters)
    show_unassigned = bool(filters.get("include_unassigned_route"))
    route_filter = "" if show_unassigned else " AND t.route IS NOT NULL AND t.route != ''"
    rows = frappe.db.sql(
        f"""
        SELECT
            COALESCE(NULLIF(t.route, ''), '(Unassigned)') AS route,
            t.driver AS driver,
            COUNT(*) AS trips,
            SUM(t.status IN ('Completed', 'Closed')) AS trips_done,
            COALESCE(SUM(t.total_distance_planned_km), 0) AS planned_km,
            COALESCE(SUM(t.total_distance_actual_km), 0) AS actual_km,
            COALESCE(AVG(NULLIF(t.total_duration_actual_min, 0)), 0) AS avg_duration_min
        FROM `tabCH Logistics Trip` t
        WHERE t.driver IS NOT NULL {route_filter} {where}
        GROUP BY route, t.driver
        """,
        vals,
        as_dict=True,
    )
    return rows


def _fetch_ontime(filters: dict) -> dict[tuple[str, str], dict]:
    """On-time delivery / damaged counts per (trip-route, driver).

    We join manifests → trips to obtain the route (CH Transfer Manifest
    itself has no route field — it's the trip that carries route).
    """
    cond: list[str] = []
    vals: dict[str, object] = {}
    company = resolve_company(filters)
    if company:
        cond.append("m.company = %(company)s")
        vals["company"] = company
    if filters.get("from_date"):
        cond.append("m.manifest_date >= %(from_date)s")
        vals["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        cond.append("m.manifest_date <= %(to_date)s")
        vals["to_date"] = filters["to_date"]
    if filters.get("driver"):
        cond.append("m.driver = %(driver)s")
        vals["driver"] = filters["driver"]
    if filters.get("route"):
        cond.append("t.route = %(route)s")
        vals["route"] = filters["route"]
    m_scope = _manifest_scope("m")
    if m_scope is not None:
        cond.append(m_scope)
    t_scope = _trip_scope("t")
    if t_scope is not None:
        cond.append(t_scope)

    where = (" AND " + " AND ".join(cond)) if cond else ""
    rows = frappe.db.sql(
        f"""
        SELECT
            COALESCE(NULLIF(t.route, ''), '(Unassigned)') AS route,
            m.driver AS driver,
            SUM(m.status IN ('Delivered','Partially Received','Received','Closed')) AS deliveries,
            SUM(m.status IN ('Delivered','Partially Received','Received','Closed')
                AND m.estimated_delivery_date IS NOT NULL
                AND m.delivery_datetime IS NOT NULL
                AND m.delivery_datetime <= m.estimated_delivery_date) AS on_time,
            SUM(m.damage_reported = 1) AS damaged
        FROM `tabCH Transfer Manifest` m
        LEFT JOIN `tabCH Logistics Trip` t ON t.name = m.trip
        WHERE m.driver IS NOT NULL {where}
        GROUP BY route, m.driver
        """,
        vals,
        as_dict=True,
    )
    return {(r.route, r.driver): dict(r) for r in rows}


def _fetch_exceptions(filters: dict) -> dict[tuple[str, str], int]:
    """Exception counts per (route, driver)."""
    where, vals = _trip_where(filters)
    show_unassigned = bool(filters.get("include_unassigned_route"))
    route_filter = "" if show_unassigned else " AND t.route IS NOT NULL AND t.route != ''"
    rows = frappe.db.sql(
        f"""
        SELECT
            COALESCE(NULLIF(t.route, ''), '(Unassigned)') AS route,
            t.driver AS driver,
            COUNT(e.name) AS exceptions
        FROM `tabCH Logistics Trip` t
        LEFT JOIN `tabCH Logistics Exception` e ON e.parent = t.name
        WHERE t.driver IS NOT NULL {route_filter} {where}
        GROUP BY route, t.driver
        """,
        vals,
        as_dict=True,
    )
    return {(r.route, r.driver): r.exceptions or 0 for r in rows}


def execute(filters: dict | None = None):
    filters = dict(filters or {})

    base = _fetch_trip_aggregates(filters)
    ontime = _fetch_ontime(filters)
    exceptions = _fetch_exceptions(filters)

    # Enrich in memory — one pass over base is O(N drivers × routes)
    # which stays tiny (< a few hundred rows) at 500-store scale.
    data: list[dict] = []
    for row in base:
        key = (row["route"], row["driver"])
        onm = ontime.get(key, {})
        deliveries = flt(onm.get("deliveries") or 0)
        on_time = flt(onm.get("on_time") or 0)
        driver_name = (
            frappe.db.get_value("Driver", row["driver"], "full_name")
            or row["driver"]
        )
        route_name = (
            frappe.db.get_value("CH Route", row["route"], "route_name")
            if row["route"] and row["route"] != "(Unassigned)"
            else row["route"]
        )
        planned_km = flt(row["planned_km"] or 0, 1)
        actual_km = flt(row["actual_km"] or 0, 1)
        # Positive delta = actual > planned (bad).  Negative = beat the plan.
        delta_km = round(actual_km - planned_km, 1)
        variance_pct = (
            round((actual_km - planned_km) / planned_km * 100, 1)
            if planned_km
            else None
        )
        data.append(
            {
                "route": row["route"],
                "route_name": route_name,
                "driver": row["driver"],
                "driver_name": driver_name,
                "trips": row["trips"] or 0,
                "trips_done": row["trips_done"] or 0,
                "deliveries": int(deliveries),
                "on_time_pct": (
                    round(on_time / deliveries * 100, 1) if deliveries else None
                ),
                "damaged": int(onm.get("damaged") or 0),
                "exceptions": exceptions.get(key, 0),
                "planned_km": planned_km,
                "actual_km": actual_km,
                "delta_km": delta_km,
                "variance_pct": variance_pct,
                "avg_duration_min": round(flt(row["avg_duration_min"] or 0), 1),
            }
        )

    # Sort routes together, then drivers-in-route by trip count desc so
    # the top performer per route sits at the top of each cluster.
    data.sort(key=lambda r: (r["route"], -r["trips"]))

    columns = [
        col(_("Route"), "route", "Link", 120, "CH Route"),
        col(_("Route Name"), "route_name", "Data", 150),
        col(_("Driver"), "driver", "Link", 120, "Driver"),
        col(_("Driver Name"), "driver_name", "Data", 150),
        col(_("Trips"), "trips", "Int", 70),
        col(_("Completed"), "trips_done", "Int", 80),
        col(_("Deliveries"), "deliveries", "Int", 80),
        col(_("On-Time %"), "on_time_pct", "Percent", 90),
        col(_("Damaged"), "damaged", "Int", 80),
        col(_("Exceptions"), "exceptions", "Int", 90),
        col(_("Planned km"), "planned_km", "Float", 90, precision=1),
        col(_("Actual km"), "actual_km", "Float", 90, precision=1),
        col(_("Δ km"), "delta_km", "Float", 80, precision=1),
        col(_("Variance %"), "variance_pct", "Percent", 90),
        col(_("Avg Duration (min)"), "avg_duration_min", "Float", 110, precision=1),
    ]
    return columns, data
