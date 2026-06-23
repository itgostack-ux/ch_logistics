"""Freight and Delivery Cost — freight spend per manifest with headline totals,
for logistics head & finance."""
import frappe
from frappe import _
from frappe.utils import flt

from ch_logistics.api.report_utils import col, manifest_conditions


def execute(filters=None):
    filters = filters or {}
    where, vals = manifest_conditions(filters)
    rows = frappe.db.sql(f"""
        SELECT m.name, m.manifest_date, m.status, m.direction,
               m.source_store, m.source_warehouse,
               m.destination_store, m.destination_warehouse,
               m.driver_name, m.total_weight_kg, m.freight_amount,
               m.freight_journal_entry, m.total_items, m.total_qty
        FROM `tabCH Transfer Manifest` m
        WHERE {where} AND IFNULL(m.freight_amount,0) > 0
        ORDER BY m.manifest_date DESC, m.name DESC
    """, vals, as_dict=True)

    total_freight = 0.0
    total_weight = 0.0
    for r in rows:
        r["from_loc"] = r.source_store or r.source_warehouse
        r["to_loc"] = r.destination_store or r.destination_warehouse
        r["cost_per_kg"] = round(flt(r.freight_amount) / flt(r.total_weight_kg), 2) \
            if flt(r.total_weight_kg) else None
        r["posted"] = _("Yes") if r.freight_journal_entry else _("No")
        total_freight += flt(r.freight_amount)
        total_weight += flt(r.total_weight_kg)

    n = len(rows)
    report_summary = [
        {"label": _("Total Freight"), "value": round(total_freight, 2),
         "datatype": "Currency", "indicator": "Blue"},
        {"label": _("Freighted Manifests"), "value": n, "datatype": "Int"},
        {"label": _("Avg Freight / Manifest"),
         "value": round(total_freight / n, 2) if n else 0, "datatype": "Currency"},
        {"label": _("Total Weight (kg)"), "value": round(total_weight, 1), "datatype": "Float"},
        {"label": _("Avg Cost / kg"),
         "value": round(total_freight / total_weight, 2) if total_weight else 0,
         "datatype": "Currency"},
    ]

    columns = [
        col(_("Manifest"), "name", "Link", 130, "CH Transfer Manifest"),
        col(_("Date"), "manifest_date", "Date", 90),
        col(_("From"), "from_loc", "Data", 150),
        col(_("To"), "to_loc", "Data", 150),
        col(_("Driver"), "driver_name", "Data", 130),
        col(_("Weight (kg)"), "total_weight_kg", "Float", 90, precision=2),
        col(_("Freight"), "freight_amount", "Currency", 110),
        col(_("Cost / kg"), "cost_per_kg", "Currency", 90),
        col(_("Items"), "total_items", "Int", 60),
        col(_("Posted to GL"), "posted", "Data", 90),
        col(_("Status"), "status", "Data", 100),
    ]
    return columns, rows, None, None, report_summary
