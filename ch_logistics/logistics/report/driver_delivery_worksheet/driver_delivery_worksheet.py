"""Driver Delivery Worksheet — a driver's own manifests for a day with proof
status. Delivery staff see their own row by default; ops can pick any driver."""
import frappe
from frappe import _
from frappe.utils import today

from ch_erp15.ch_erp15.report_scope import scope_where_clause
from ch_logistics.api.report_utils import col, current_driver, is_ops_user, resolve_company


def execute(filters=None):
    filters = filters or {}
    driver = filters.get("driver")
    if not driver and not is_ops_user():
        driver = current_driver()  # delivery staff → own worksheet only
    day = filters.get("date") or today()

    cond = ["m.manifest_date = %(day)s"]
    vals = {"day": day}
    company = resolve_company(filters)
    if company:
        cond.append("m.company = %(company)s"); vals["company"] = company
    if driver:
        cond.append("m.driver = %(driver)s"); vals["driver"] = driver
    elif not is_ops_user():
        return _columns(), []  # no driver record & not ops → nothing
    if filters.get("status"):
        cond.append("m.status = %(status)s"); vals["status"] = filters["status"]

    # Tier 4: fail-closed scope on either manifest endpoint.
    scope = scope_where_clause(
        warehouse_field="m.source_warehouse",
        extra_warehouse_fields=("m.destination_warehouse",),
        store_field="m.source_store",
        extra_store_fields=("m.destination_store",),
    )
    if scope is not None:
        cond.append(scope)

    rows = frappe.db.sql(f"""
        SELECT m.name, m.trip, m.stop_sequence, m.status, m.shipment_priority,
               m.source_store, m.source_warehouse,
               m.destination_store, m.destination_warehouse,
               m.total_items, m.total_qty, m.pickup_datetime, m.delivery_datetime,
               m.receiver_name, m.delivery_otp_verified
        FROM `tabCH Transfer Manifest` m
        WHERE {' AND '.join(cond)}
        ORDER BY m.stop_sequence ASC, m.name ASC
    """, vals, as_dict=True)

    for r in rows:
        r["from_loc"] = r.source_store or r.source_warehouse
        r["to_loc"] = r.destination_store or r.destination_warehouse
        r["otp_ok"] = _("Yes") if r.delivery_otp_verified else ""

    return _columns(), rows


def _columns():
    return [
        col(_("Manifest"), "name", "Link", 130, "CH Transfer Manifest"),
        col(_("Trip"), "trip", "Link", 120, "CH Logistics Trip"),
        col(_("Stop"), "stop_sequence", "Int", 60),
        col(_("Priority"), "shipment_priority", "Data", 80),
        col(_("From"), "from_loc", "Data", 150),
        col(_("To"), "to_loc", "Data", 150),
        col(_("Status"), "status", "Data", 110),
        col(_("Items"), "total_items", "Int", 60),
        col(_("Qty"), "total_qty", "Float", 70, precision=1),
        col(_("Picked At"), "pickup_datetime", "Datetime", 150),
        col(_("Delivered At"), "delivery_datetime", "Datetime", 150),
        col(_("Receiver"), "receiver_name", "Data", 130),
        col(_("OTP Verified"), "otp_ok", "Data", 90),
    ]
