"""Backfill CH Transfer Manifest.route for manifests created before this
tag existed. Best-effort, informational — silently leaves route blank for
any destination not covered by a CH Route Stop. Idempotent: only touches
rows where route is currently blank, and uses frappe.db.set_value rather
than doc.save() since most pre-existing manifests are already submitted.
"""
from __future__ import annotations

import frappe

from ch_erp15.ch_erp15.doctype.ch_route.ch_route import get_route_for_destination


def execute():
    if not frappe.db.has_column("CH Transfer Manifest", "route"):
        return

    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters={"route": ("in", ("", None))},
        fields=["name", "company", "destination_warehouse", "destination_store"],
    )
    updated = 0
    for r in rows:
        route = get_route_for_destination(
            r.company, r.destination_warehouse, r.destination_store
        )
        if route:
            frappe.db.set_value(
                "CH Transfer Manifest", r.name, "route", route,
                update_modified=False,
            )
            updated += 1

    if updated:
        frappe.db.commit()
        print(f"[ch_logistics] backfilled route on {updated} transfer manifests")
