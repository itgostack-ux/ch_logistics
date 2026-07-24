"""Driver-facing 'My Trips' portal page.

Shows the logged-in driver's CH Logistics Trips for today (+/- include_days),
with per-stop checkin/checkout buttons that hit the existing whitelisted
`logistics_api.stop_arrive` / `stop_complete` endpoints.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from ch_logistics.api.driver_resolver import resolve_current_driver
from ch_logistics.roles import user_has

no_cache = 1


def get_context(context):
    user = frappe.session.user
    if user in ("Guest", ""):
        frappe.local.flags.redirect_location = "/login?redirect-to=/my-trips"
        raise frappe.Redirect

    driver_name = resolve_current_driver(throw=False)
    driver = frappe.db.get_value(
        "Driver", driver_name, ["name", "full_name", "cell_number"], as_dict=True
    ) if driver_name else None
    can_preview = user_has("ops_view", user)
    if not driver and not can_preview:
        context.error = _("Your account is not linked to a driver record. "
                          "Please contact dispatch.")
        context.trips = []
        context.title = _("My Trips")
        return context

    today_d = getdate(today())
    yesterday = add_days(today_d, -1)
    tomorrow = add_days(today_d, 1)

    filters = {"trip_date": ["between", [yesterday, tomorrow]]}
    if driver:
        filters["driver"] = driver.name
    else:
        # Admin preview: show any recent trip
        pass

    trips = frappe.get_all(
        "CH Logistics Trip",
        filters=filters,
        fields=["name", "trip_date", "status", "direction", "route",
                "hub_warehouse", "planned_start", "planned_end",
                "vehicle_number", "total_shipments"],
        order_by="trip_date desc, planned_start asc",
        limit=10,
    )

    for t in trips:
        t["stops"] = frappe.get_all(
            "CH Logistics Trip Stop",
            filters={"parent": t.name, "parenttype": "CH Logistics Trip"},
            fields=["name", "sequence", "stop_type", "store", "warehouse",
                    "eta", "ata", "status", "manifest_count", "notes"],
            order_by="sequence asc",
        )

    context.title = _("My Trips")
    context.driver = driver or {"name": "—", "full_name": _("Operations preview")}
    context.trips = trips
    context.today = today_d
    context.no_cache = 1
    return context
