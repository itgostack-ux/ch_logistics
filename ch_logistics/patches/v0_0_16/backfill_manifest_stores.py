"""Name the store at each end of manifests that only knew their warehouses.

A manifest takes its warehouses from the Stock Entries it carries, and a
Stock Entry has no store — so source_store / destination_store were never
filled in. Everyone the delivery OTP has to reach hangs off the store: its
manager, its POS profile's executives, its own phone number. Without the
link, the only contact left was the warehouse's own email, which is usually
blank, and the code reached nobody.

Manifests now fill this in on save (_fill_stores_from_warehouses); this is
the same resolution applied to the ones written before that. Warehouses with
no store, or with more than one, are left alone. Safe to re-run.
"""

from __future__ import annotations

import frappe


def execute():
    if not frappe.db.exists("DocType", "CH Store"):
        return
    from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
        store_for_warehouse,
    )

    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters={"docstatus": ("<", 2)},
        fields=["name", "source_warehouse", "source_store",
                "destination_warehouse", "destination_store"],
        limit_page_length=0,
    )
    cache: dict[str, str | None] = {}
    filled = 0
    for row in rows:
        update = {}
        for side in ("source", "destination"):
            if row.get(f"{side}_store"):
                continue
            warehouse = row.get(f"{side}_warehouse")
            if not warehouse:
                continue
            if warehouse not in cache:
                cache[warehouse] = store_for_warehouse(warehouse)
            if cache[warehouse]:
                update[f"{side}_store"] = cache[warehouse]
        if update:
            # Submitted manifests are the norm here, and this only names what
            # the warehouse already implied — no timestamps are disturbed.
            frappe.db.set_value("CH Transfer Manifest", row.name, update,
                                update_modified=False)
            filled += 1
    frappe.db.commit()
    print(f"CH Transfer Manifest: store links filled on {filled} of {len(rows)}")
