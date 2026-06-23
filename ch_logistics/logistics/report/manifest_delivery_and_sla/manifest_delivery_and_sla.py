"""Manifest Delivery and SLA — per-manifest status, aging and SLA adherence.

The workhorse register for stores ("where is my transfer?"), ops and logistics
head. SLA is derived from estimated_delivery_date vs actual delivery (or now).
"""
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime, time_diff_in_hours, cint

from ch_logistics.api.report_utils import col, manifest_conditions


def execute(filters=None):
    filters = filters or {}
    where, vals = manifest_conditions(filters)
    rows = frappe.db.sql(f"""
        SELECT m.name, m.manifest_date, m.status, m.shipment_priority, m.direction,
               m.source_store, m.source_warehouse,
               m.destination_store, m.destination_warehouse,
               m.driver_name, m.total_items, m.total_qty,
               m.estimated_delivery_date, m.pickup_datetime, m.delivery_datetime,
               m.received_datetime, m.damage_reported, m.creation
        FROM `tabCH Transfer Manifest` m
        WHERE {where}
        ORDER BY m.manifest_date DESC, m.name DESC
    """, vals, as_dict=True)

    now = now_datetime()
    settled = ("Received", "Closed", "Partially Received")
    delivered = settled + ("Delivered",)
    out = []
    for r in rows:
        r["from_loc"] = r.source_store or r.source_warehouse
        r["to_loc"] = r.destination_store or r.destination_warehouse
        r["received"] = _("Yes") if r.received_datetime else _("No")

        end = r.received_datetime or r.delivery_datetime or now
        r["age_hours"] = round(time_diff_in_hours(get_datetime(end), get_datetime(r.creation)), 1)

        if r.status in delivered and r.estimated_delivery_date and r.delivery_datetime:
            r["sla"] = _("On Time") if r.delivery_datetime <= r.estimated_delivery_date else _("Late")
        elif r.status not in delivered and r.status not in ("Cancelled", "Rejected", "Returned") \
                and r.estimated_delivery_date and get_datetime(r.estimated_delivery_date) < now:
            r["sla"] = _("Overdue")
        elif r.status in ("Cancelled", "Rejected", "Returned"):
            r["sla"] = "—"
        else:
            r["sla"] = _("On Track")

        if cint(filters.get("overdue_only")) and r["sla"] != _("Overdue"):
            continue
        out.append(r)

    columns = [
        col(_("Manifest"), "name", "Link", 130, "CH Transfer Manifest"),
        col(_("Date"), "manifest_date", "Date", 90),
        col(_("Status"), "status", "Data", 100),
        col(_("Priority"), "shipment_priority", "Data", 80),
        col(_("From"), "from_loc", "Data", 150),
        col(_("To"), "to_loc", "Data", 150),
        col(_("Driver"), "driver_name", "Data", 130),
        col(_("Items"), "total_items", "Int", 60),
        col(_("Qty"), "total_qty", "Float", 70, precision=1),
        col(_("Est. Delivery"), "estimated_delivery_date", "Datetime", 150),
        col(_("Delivered At"), "delivery_datetime", "Datetime", 150),
        col(_("Age (hrs)"), "age_hours", "Float", 90, precision=1),
        col(_("SLA"), "sla", "Data", 90),
        col(_("Received"), "received", "Data", 80),
    ]
    return columns, out
