"""Monthly Driver KPI — executive scorecard, one row per (driver, month).

Purpose
-------

The Driver Performance Scorecard produces a single flat row per driver
over the whole filter window — useful for "who is the top performer
right now?" but useless for spotting month-over-month drift.  This
report re-shapes the same data as a (driver × month) grid so managers
can eyeball trends without pivoting in Excel:

* trips run
* total & average km per trip
* on-time delivery %
* damage / exception counts
* break minutes  (populated once CH Driver Break Log lands in Phase B —
  today the column shows 0 for every driver, which is the honest
  answer given no break log exists yet)

Sort order is stable (driver ASC, then month DESC) so the newest month
sits at the top of each driver's cluster.

Design parity: SAP TM Driver Monthly Scorecard, Oracle OTM Fleet
Manager Monthly KPI, D365 Transportation Monthly Performance.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ch_erp15.ch_erp15.report_scope import scope_where_clause
from ch_logistics.api.report_utils import col, resolve_company


def _trip_scope(alias: str = "t") -> str:
    return scope_where_clause(warehouse_field=f"{alias}.hub_warehouse")


def _manifest_scope(alias: str = "m") -> str:
    return scope_where_clause(
        warehouse_field=f"{alias}.source_warehouse",
        extra_warehouse_fields=(f"{alias}.destination_warehouse",),
        store_field=f"{alias}.source_store",
        extra_store_fields=(f"{alias}.destination_store",),
    )


def _trip_where(filters: dict) -> tuple[str, dict]:
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
    scope = _trip_scope("t")
    if scope is not None:
        cond.append(scope)
    return (" AND " + " AND ".join(cond)) if cond else "", vals


def _fetch_trips(filters: dict) -> list[dict]:
    where, vals = _trip_where(filters)
    rows = frappe.db.sql(
        f"""
        SELECT
            t.driver AS driver,
            DATE_FORMAT(t.trip_date, '%%Y-%%m') AS month,
            COUNT(*) AS trips,
            SUM(t.status IN ('Completed','Closed')) AS trips_done,
            COALESCE(SUM(t.total_distance_actual_km), 0) AS actual_km,
            COALESCE(AVG(NULLIF(t.total_distance_actual_km, 0)), 0) AS avg_km_per_trip,
            COALESCE(AVG(NULLIF(t.total_duration_actual_min, 0)), 0) AS avg_duration_min
        FROM `tabCH Logistics Trip` t
        WHERE t.driver IS NOT NULL {where}
        GROUP BY driver, month
        """,
        vals,
        as_dict=True,
    )
    return rows


def _fetch_manifests(filters: dict) -> dict[tuple[str, str], dict]:
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
    m_scope = _manifest_scope("m")
    if m_scope is not None:
        cond.append(m_scope)
    where = (" AND " + " AND ".join(cond)) if cond else ""
    rows = frappe.db.sql(
        f"""
        SELECT
            m.driver AS driver,
            DATE_FORMAT(m.manifest_date, '%%Y-%%m') AS month,
            SUM(m.status IN ('Delivered','Partially Received','Received','Closed')) AS deliveries,
            SUM(m.status IN ('Delivered','Partially Received','Received','Closed')
                AND m.estimated_delivery_date IS NOT NULL
                AND m.delivery_datetime IS NOT NULL
                AND m.delivery_datetime <= m.estimated_delivery_date) AS on_time,
            SUM(m.damage_reported = 1) AS damaged
        FROM `tabCH Transfer Manifest` m
        WHERE m.driver IS NOT NULL {where}
        GROUP BY driver, month
        """,
        vals,
        as_dict=True,
    )
    return {(r.driver, r.month): dict(r) for r in rows}


def _fetch_exceptions(filters: dict) -> dict[tuple[str, str], int]:
    where, vals = _trip_where(filters)
    rows = frappe.db.sql(
        f"""
        SELECT
            t.driver AS driver,
            DATE_FORMAT(t.trip_date, '%%Y-%%m') AS month,
            COUNT(e.name) AS exceptions
        FROM `tabCH Logistics Trip` t
        LEFT JOIN `tabCH Logistics Exception` e ON e.parent = t.name
        WHERE t.driver IS NOT NULL {where}
        GROUP BY driver, month
        """,
        vals,
        as_dict=True,
    )
    return {(r.driver, r.month): r.exceptions or 0 for r in rows}


def _fetch_break_minutes(filters: dict) -> dict[tuple[str, str], float]:
    """Break-minute aggregation — Phase B (CH Driver Break Log).

    Doctype does not exist yet; return an empty map so the column
    renders 0 for every driver.  When Phase B ships, populate this
    with a SUM(duration_min) grouped by (driver, month).
    """
    if not frappe.db.table_exists("CH Driver Break Log"):
        return {}
    where: list[str] = []
    vals: dict[str, object] = {}
    if filters.get("from_date"):
        where.append("b.start_ts >= %(from_date)s")
        vals["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        where.append("b.start_ts <= %(to_date)s")
        vals["to_date"] = filters["to_date"]
    if filters.get("driver"):
        where.append("b.driver = %(driver)s")
        vals["driver"] = filters["driver"]
    where_sql = (" AND " + " AND ".join(where)) if where else ""
    rows = frappe.db.sql(
        f"""
        SELECT
            b.driver AS driver,
            DATE_FORMAT(b.start_ts, '%%Y-%%m') AS month,
            COALESCE(SUM(b.duration_min), 0) AS break_minutes
        FROM `tabCH Driver Break Log` b
        WHERE b.driver IS NOT NULL {where_sql}
        GROUP BY driver, month
        """,
        vals,
        as_dict=True,
    )
    return {(r.driver, r.month): flt(r.break_minutes) for r in rows}


def execute(filters: dict | None = None):
    filters = dict(filters or {})

    trips = _fetch_trips(filters)
    manifests = _fetch_manifests(filters)
    exceptions = _fetch_exceptions(filters)
    breaks = _fetch_break_minutes(filters)

    data: list[dict] = []
    for row in trips:
        key = (row["driver"], row["month"])
        mn = manifests.get(key, {})
        deliveries = flt(mn.get("deliveries") or 0)
        on_time = flt(mn.get("on_time") or 0)
        driver_name = (
            frappe.db.get_value("Driver", row["driver"], "full_name")
            or row["driver"]
        )
        actual_km = flt(row["actual_km"] or 0, 1)
        avg_km = flt(row["avg_km_per_trip"] or 0, 1)
        avg_duration = flt(row["avg_duration_min"] or 0, 1)
        break_minutes = breaks.get(key, 0.0)
        # Load factor = deliveries / trips.  Above 1.0 means multiple
        # manifests per trip (good — batch density); below 1.0 usually
        # signals empty legs and warrants investigation.
        load_factor = (
            round(deliveries / row["trips"], 2) if row["trips"] else None
        )
        data.append(
            {
                "driver": row["driver"],
                "driver_name": driver_name,
                "month": row["month"],
                "trips": row["trips"] or 0,
                "trips_done": row["trips_done"] or 0,
                "deliveries": int(deliveries),
                "load_factor": load_factor,
                "actual_km": actual_km,
                "avg_km_per_trip": avg_km,
                "avg_duration_min": avg_duration,
                "on_time_pct": (
                    round(on_time / deliveries * 100, 1) if deliveries else None
                ),
                "damaged": int(mn.get("damaged") or 0),
                "exceptions": exceptions.get(key, 0),
                "break_minutes": round(break_minutes, 1),
            }
        )

    # Driver ASC, month DESC — puts newest month at the top of each cluster.
    data.sort(key=lambda r: (r["driver"] or "", r["month"] or ""), reverse=False)
    data.sort(key=lambda r: r["month"] or "", reverse=True)
    data.sort(key=lambda r: r["driver"] or "")

    columns = [
        col(_("Driver"), "driver", "Link", 120, "Driver"),
        col(_("Driver Name"), "driver_name", "Data", 150),
        col(_("Month"), "month", "Data", 80),
        col(_("Trips"), "trips", "Int", 70),
        col(_("Completed"), "trips_done", "Int", 80),
        col(_("Deliveries"), "deliveries", "Int", 80),
        col(_("Load Factor"), "load_factor", "Float", 90, precision=2),
        col(_("Total km"), "actual_km", "Float", 90, precision=1),
        col(_("Avg km / Trip"), "avg_km_per_trip", "Float", 100, precision=1),
        col(_("Avg Duration (min)"), "avg_duration_min", "Float", 110, precision=1),
        col(_("On-Time %"), "on_time_pct", "Percent", 90),
        col(_("Damaged"), "damaged", "Int", 80),
        col(_("Exceptions"), "exceptions", "Int", 90),
        col(_("Break Minutes"), "break_minutes", "Float", 100, precision=1),
    ]
    return columns, data
