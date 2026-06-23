"""Logistics Trip Performance — trip-level KPIs for managers & logistics head."""
import frappe
from frappe import _

from ch_logistics.api.report_utils import col, trip_conditions


def execute(filters=None):
    filters = filters or {}
    where, vals = trip_conditions(filters)
    rows = frappe.db.sql(f"""
        SELECT t.name, t.trip_date, t.status, t.driver_name, t.vehicle_number,
               t.route, t.hub_warehouse, t.total_shipments,
               t.total_distance_planned_km, t.total_distance_actual_km,
               t.total_duration_actual_min, t.optimized,
               t.planned_end, t.actual_end,
               (SELECT COUNT(*) FROM `tabCH Logistics Trip Stop` s
                  WHERE s.parent = t.name) AS stops
        FROM `tabCH Logistics Trip` t
        WHERE {where}
        ORDER BY t.trip_date DESC, t.name DESC
    """, vals, as_dict=True)

    for r in rows:
        if r.actual_end and r.planned_end:
            r["on_time"] = _("On Time") if str(r.actual_end) <= str(r.planned_end) else _("Late")
        elif r.status in ("Completed", "Closed"):
            r["on_time"] = "—"
        else:
            r["on_time"] = _("In Progress")
        r["optimized"] = _("Yes") if r.optimized else _("No")

    columns = [
        col(_("Trip"), "name", "Link", 130, "CH Logistics Trip"),
        col(_("Date"), "trip_date", "Date", 90),
        col(_("Status"), "status", "Data", 90),
        col(_("Driver"), "driver_name", "Data", 140),
        col(_("Vehicle"), "vehicle_number", "Data", 100),
        col(_("Route"), "route", "Link", 120, "CH Route"),
        col(_("Hub"), "hub_warehouse", "Link", 140, "Warehouse"),
        col(_("Stops"), "stops", "Int", 60),
        col(_("Shipments"), "total_shipments", "Int", 80),
        col(_("Planned km"), "total_distance_planned_km", "Float", 90, precision=2),
        col(_("Actual km"), "total_distance_actual_km", "Float", 90, precision=2),
        col(_("Duration (min)"), "total_duration_actual_min", "Int", 100),
        col(_("On-Time"), "on_time", "Data", 90),
        col(_("Optimized"), "optimized", "Data", 80),
    ]
    return columns, rows
