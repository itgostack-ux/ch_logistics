"""Fleet and Vehicle Utilization — per-vehicle trip/distance/load rollup for the
logistics head."""
import frappe
from frappe import _
from frappe.utils import flt

from ch_logistics.api.report_utils import col, trip_conditions


def execute(filters=None):
    filters = filters or {}
    where, vals = trip_conditions(filters)
    rows = frappe.db.sql(f"""
        SELECT COALESCE(t.vehicle, t.vehicle_number, '— Unassigned —') AS vehicle_key,
               t.vehicle, MAX(t.vehicle_number) AS vehicle_number,
               COUNT(*) AS trips,
               SUM(t.status IN ('Completed','Closed')) AS trips_done,
               COALESCE(SUM(t.total_shipments),0) AS shipments,
               COALESCE(SUM(t.total_distance_actual_km),0) AS distance_km,
               COUNT(DISTINCT t.trip_date) AS active_days
        FROM `tabCH Logistics Trip` t
        WHERE {where}
        GROUP BY vehicle_key
        ORDER BY trips DESC
    """, vals, as_dict=True)

    for r in rows:
        r["distance_km"] = flt(r.distance_km, 2)
        r["avg_shipments"] = round(r.shipments / r.trips, 1) if r.trips else 0
        r["capacity_kg"] = flt(frappe.db.get_value("Vehicle", r.vehicle, "custom_capacity_kg")) \
            if r.vehicle else None
        r["trips_per_active_day"] = round(r.trips / r.active_days, 1) if r.active_days else 0

    columns = [
        col(_("Vehicle"), "vehicle", "Link", 120, "Vehicle"),
        col(_("Vehicle No"), "vehicle_number", "Data", 110),
        col(_("Capacity (kg)"), "capacity_kg", "Float", 100, precision=1),
        col(_("Trips"), "trips", "Int", 70),
        col(_("Completed"), "trips_done", "Int", 90),
        col(_("Shipments"), "shipments", "Int", 90),
        col(_("Distance km"), "distance_km", "Float", 100, precision=2),
        col(_("Active Days"), "active_days", "Int", 90),
        col(_("Trips / Active Day"), "trips_per_active_day", "Float", 120, precision=1),
        col(_("Avg Shipments / Trip"), "avg_shipments", "Float", 130, precision=1),
    ]
    return columns, rows
