"""Delivery Exceptions and Rejections — one inbox of every pickup rejection and
in-trip exception, for logistics head / ops follow-up."""
import frappe
from frappe import _

from ch_logistics.api.report_utils import col


def _between(field, vals):
    c = ""
    if vals.get("from_date"):
        c += f" AND {field} >= %(from_date)s"
    if vals.get("to_date"):
        c += f" AND {field} <= %(to_date)s"
    return c


def execute(filters=None):
    filters = filters or {}
    want = filters.get("record_type")  # "", Rejection, Exception
    drv = filters.get("driver")
    p = {k: filters.get(k) for k in ("from_date", "to_date")}
    if drv:
        p["driver"] = drv

    data = []

    if want in (None, "", "Rejection"):
        rej = frappe.db.sql(f"""
            SELECT 'Rejection' AS record_type, r.rejected_on AS event_time,
                   r.driver, NULL AS severity, r.rejection_reason AS category,
                   r.manifest AS reference, r.status, r.remarks
            FROM `tabCH Manifest Rejection` r
            WHERE 1=1 {_between('DATE(r.rejected_on)', p)}
                  {' AND r.driver = %(driver)s' if drv else ''}
            ORDER BY r.rejected_on DESC
        """, p, as_dict=True)
        data += rej

    if want in (None, "", "Exception"):
        exc = frappe.db.sql(f"""
            SELECT 'Exception' AS record_type, e.occurred_at AS event_time,
                   t.driver, e.severity, e.exception_type AS category,
                   t.name AS reference,
                   IFNULL(e.resolution_status,'Open') AS status, e.remarks
            FROM `tabCH Logistics Exception` e
            JOIN `tabCH Logistics Trip` t ON t.name = e.parent
            WHERE 1=1 {_between('DATE(e.occurred_at)', p)}
                  {' AND t.driver = %(driver)s' if drv else ''}
            ORDER BY e.occurred_at DESC
        """, p, as_dict=True)
        data += exc

    for r in data:
        r["driver_name"] = frappe.db.get_value("Driver", r["driver"], "full_name") if r.get("driver") else None
    data.sort(key=lambda r: str(r.get("event_time") or ""), reverse=True)

    columns = [
        col(_("Type"), "record_type", "Data", 90),
        col(_("When"), "event_time", "Datetime", 150),
        col(_("Driver"), "driver_name", "Data", 140),
        col(_("Category / Reason"), "category", "Data", 160),
        col(_("Severity"), "severity", "Data", 90),
        col(_("Reference"), "reference", "Dynamic Link", 140),
        col(_("Status"), "status", "Data", 100),
        col(_("Remarks"), "remarks", "Small Text", 220),
    ]
    return columns, data
