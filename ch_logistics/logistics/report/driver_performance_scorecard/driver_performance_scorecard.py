"""Driver Performance Scorecard — per-driver delivery KPIs.

For logistics head & managers (all drivers) and delivery staff (own row via the
Driver filter). Merges trip, delivery, rejection, exception and scan-compliance
aggregates into one row per driver.
"""
import frappe
from frappe import _
from frappe.utils import flt

from ch_erp15.ch_erp15.report_scope import scope_where_clause
from ch_logistics.api.report_utils import col, resolve_company


def _trip_scope():
    """Fail-closed scope on `t.hub_warehouse` (CH Logistics Trip alias t)."""
    return scope_where_clause(warehouse_field="t.hub_warehouse")


def _manifest_scope(alias="m"):
    """Fail-closed scope on any endpoint of CH Transfer Manifest."""
    return scope_where_clause(
        warehouse_field=f"{alias}.source_warehouse",
        extra_warehouse_fields=(f"{alias}.destination_warehouse",),
        store_field=f"{alias}.source_store",
        extra_store_fields=(f"{alias}.destination_store",),
    )


def _dt(filters, field, alias="", with_company=True):
    a = (alias + ".") if alias else ""
    cond, vals = [], {}
    company = resolve_company(filters) if with_company else None
    if company:
        cond.append(f"{a}company = %(company)s")
        vals["company"] = company
    if filters.get("from_date"):
        cond.append(f"{a}{field} >= %(from_date)s")
        vals["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        cond.append(f"{a}{field} <= %(to_date)s")
        vals["to_date"] = filters["to_date"]
    if filters.get("driver"):
        cond.append(f"{a}driver = %(driver)s")
        vals["driver"] = filters["driver"]
    return (" AND " + " AND ".join(cond)) if cond else "", vals


def execute(filters=None):
    filters = filters or {}

    tw, tv = _dt(filters, "trip_date", "t")
    trip_scope = _trip_scope()
    trip_scope_sql = f" AND {trip_scope}" if trip_scope else ""
    trips = frappe.db.sql(f"""
        SELECT t.driver, COUNT(*) trips,
               SUM(t.status IN ('Completed','Closed')) trips_done,
               COALESCE(SUM(t.total_distance_actual_km),0) distance_km
        FROM `tabCH Logistics Trip` t
        WHERE t.driver IS NOT NULL {tw}{trip_scope_sql}
        GROUP BY t.driver
    """, tv, as_dict=True)

    mw, mv = _dt(filters, "manifest_date", "m")
    m_scope = _manifest_scope("m")
    m_scope_sql = f" AND {m_scope}" if m_scope else ""
    delivs = frappe.db.sql(f"""
        SELECT m.driver,
               SUM(m.status IN ('Delivered','Partially Received','Received','Closed')) deliveries,
               SUM(m.status IN ('Delivered','Partially Received','Received','Closed')
                   AND m.estimated_delivery_date IS NOT NULL
                   AND m.delivery_datetime IS NOT NULL
                   AND m.delivery_datetime <= m.estimated_delivery_date) on_time,
               SUM(m.damage_reported = 1) damaged
        FROM `tabCH Transfer Manifest` m
        WHERE m.driver IS NOT NULL {mw}{m_scope_sql}
        GROUP BY m.driver
    """, mv, as_dict=True)

    rw, rv = _dt(filters, "rejected_on", "r", with_company=False)  # rejection has no company
    # Tier 4: CH Manifest Rejection has no store/warehouse of its own — reach
    # scope through its linked manifest. LEFT JOIN preserves rejections whose
    # manifest is missing only for bypass callers (where scope is None).
    rej_scope = _manifest_scope("rm")
    rej_scope_sql = f" AND {rej_scope}" if rej_scope else ""
    rej_join = "LEFT JOIN `tabCH Transfer Manifest` rm ON rm.name = r.manifest" if rej_scope else ""
    rejs = frappe.db.sql(f"""
        SELECT r.driver, COUNT(*) rejections
        FROM `tabCH Manifest Rejection` r
        {rej_join}
        WHERE r.driver IS NOT NULL {rw}{rej_scope_sql}
        GROUP BY r.driver
    """, rv, as_dict=True)

    ew, ev = _dt(filters, "trip_date", "t")
    excs = frappe.db.sql(f"""
        SELECT t.driver, COUNT(*) exceptions,
               AVG(NULLIF(s.scan_compliance_pct,0)) avg_scan
        FROM `tabCH Logistics Trip` t
        LEFT JOIN `tabCH Logistics Exception` e ON e.parent = t.name
        LEFT JOIN `tabCH Logistics Trip Stop` s ON s.parent = t.name
        WHERE t.driver IS NOT NULL {ew}{trip_scope_sql}
        GROUP BY t.driver
    """, ev, as_dict=True)

    by = {}
    def row(dr):
        return by.setdefault(dr, {
            "driver": dr, "trips": 0, "trips_done": 0, "deliveries": 0,
            "on_time": 0, "damaged": 0, "rejections": 0, "exceptions": 0,
            "distance_km": 0.0, "avg_scan": None})

    for r in trips:
        x = row(r.driver); x["trips"] = r.trips; x["trips_done"] = r.trips_done
        x["distance_km"] = flt(r.distance_km, 2)
    for r in delivs:
        x = row(r.driver); x["deliveries"] = r.deliveries or 0
        x["on_time"] = r.on_time or 0; x["damaged"] = r.damaged or 0
    for r in rejs:
        row(r.driver)["rejections"] = r.rejections
    for r in excs:
        x = row(r.driver)
        x["exceptions"] = r.exceptions or 0
        x["avg_scan"] = flt(r.avg_scan, 1) if r.avg_scan else None

    data = list(by.values())
    for x in data:
        info = frappe.db.get_value("Driver", x["driver"],
                                   ["full_name", "availability_status"], as_dict=True) or {}
        x["driver_name"] = info.get("full_name")
        x["availability_status"] = info.get("availability_status")
        x["on_time_pct"] = round(x["on_time"] / x["deliveries"] * 100, 1) if x["deliveries"] else None
    data.sort(key=lambda r: (r["deliveries"], r["trips"]), reverse=True)

    columns = [
        col(_("Driver"), "driver", "Link", 120, "Driver"),
        col(_("Name"), "driver_name", "Data", 150),
        col(_("Status"), "availability_status", "Data", 90),
        col(_("Trips"), "trips", "Int", 60),
        col(_("Completed"), "trips_done", "Int", 80),
        col(_("Deliveries"), "deliveries", "Int", 80),
        col(_("On-Time %"), "on_time_pct", "Percent", 90),
        col(_("Damaged"), "damaged", "Int", 70),
        col(_("Rejections"), "rejections", "Int", 80),
        col(_("Exceptions"), "exceptions", "Int", 80),
        col(_("Distance km"), "distance_km", "Float", 90, precision=2),
        col(_("Avg Scan %"), "avg_scan", "Percent", 90),
    ]
    return columns, data
