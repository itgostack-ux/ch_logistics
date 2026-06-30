"""Backfill stop-level QR tokens for every existing CH Logistics Trip Stop.

Stop-level clubbing (one consolidated QR per drop) was introduced in v0.0.11.
Pre-existing trips have no pickup_token / delivery_token, so the new scan
APIs would refuse to start pickup or complete delivery for them. This patch
mints a random token for any stop that doesn't already have one. It is
idempotent: stops that already carry a token (e.g. minted by the trip
controller after this release lands) are left alone.
"""
from __future__ import annotations

import frappe


def execute():
    if not frappe.db.has_column("CH Logistics Trip Stop", "pickup_token"):
        # Schema not migrated yet — bench migrate will retry once the doctype
        # JSON is loaded.
        return

    rows = frappe.get_all(
        "CH Logistics Trip Stop",
        fields=["name", "pickup_token", "delivery_token"],
    )
    updated = 0
    for r in rows:
        update = {}
        if not r.pickup_token:
            update["pickup_token"] = frappe.generate_hash(length=22)
        if not r.delivery_token:
            update["delivery_token"] = frappe.generate_hash(length=22)
        if update:
            frappe.db.set_value("CH Logistics Trip Stop", r.name, update,
                                update_modified=False)
            updated += 1

    if updated:
        frappe.db.commit()
        print(f"[ch_logistics] backfilled QR tokens on {updated} trip stops")
