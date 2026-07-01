"""Store Transfer Register — a store's inbound & outbound stock transfers with
receipt status. The store-manager view of "what's coming, what I sent, what's
still to be received"."""
import frappe
from frappe import _

from ch_erp15.ch_erp15.report_scope import scope_where_clause
from ch_logistics.api.report_utils import col, resolve_company


def execute(filters=None):
    filters = filters or {}
    store = filters.get("store")
    lens = filters.get("lens") or "Both"

    cond, vals = ["1=1"], {}
    company = resolve_company(filters)
    if company:
        cond.append("m.company = %(company)s"); vals["company"] = company
    if filters.get("from_date"):
        cond.append("m.manifest_date >= %(from_date)s"); vals["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        cond.append("m.manifest_date <= %(to_date)s"); vals["to_date"] = filters["to_date"]
    if filters.get("status"):
        cond.append("m.status = %(status)s"); vals["status"] = filters["status"]

    if store:
        vals["store"] = store
        if lens == "Inbound":
            cond.append("m.destination_store = %(store)s")
        elif lens == "Outbound":
            cond.append("m.source_store = %(store)s")
        else:
            cond.append("(m.source_store = %(store)s OR m.destination_store = %(store)s)")

    # Tier 4: fail-closed narrow to caller scope. If the user picked a specific
    # store above, the OR chain here is a superset — the user filter still
    # narrows further; scope only ever removes rows they shouldn't see.
    scope = scope_where_clause(
        warehouse_field="m.source_warehouse",
        extra_warehouse_fields=("m.destination_warehouse",),
        store_field="m.source_store",
        extra_store_fields=("m.destination_store",),
    )
    if scope is not None:
        cond.append(scope)

    where = " AND ".join(cond)
    rows = frappe.db.sql(f"""
        SELECT m.name, m.manifest_date, m.status,
               m.source_store, m.source_warehouse,
               m.destination_store, m.destination_warehouse,
               m.driver_name, m.total_items, m.total_qty,
               m.delivery_datetime, m.received_datetime,
               m.damage_reported, m.estimated_delivery_date
        FROM `tabCH Transfer Manifest` m
        WHERE {where}
        ORDER BY m.manifest_date DESC, m.name DESC
    """, vals, as_dict=True)

    for r in rows:
        if store and r.destination_store == store:
            r["flow"] = _("Inbound")
            r["counterparty"] = r.source_store or r.source_warehouse
        elif store and r.source_store == store:
            r["flow"] = _("Outbound")
            r["counterparty"] = r.destination_store or r.destination_warehouse
        else:
            r["flow"] = "—"
            r["counterparty"] = (r.source_store or r.source_warehouse) + " → " + \
                                (r.destination_store or r.destination_warehouse or "")
        r["received"] = _("Yes") if r.received_datetime else _("No")
        r["damage"] = _("Yes") if r.damage_reported else ""

    columns = [
        col(_("Manifest"), "name", "Link", 130, "CH Transfer Manifest"),
        col(_("Date"), "manifest_date", "Date", 90),
        col(_("Flow"), "flow", "Data", 90),
        col(_("Counterparty"), "counterparty", "Data", 200),
        col(_("Status"), "status", "Data", 100),
        col(_("Items"), "total_items", "Int", 60),
        col(_("Qty"), "total_qty", "Float", 70, precision=1),
        col(_("Driver"), "driver_name", "Data", 130),
        col(_("Delivered At"), "delivery_datetime", "Datetime", 150),
        col(_("Received At"), "received_datetime", "Datetime", 150),
        col(_("Received"), "received", "Data", 80),
        col(_("Damage"), "damage", "Data", 70),
    ]
    return columns, rows
