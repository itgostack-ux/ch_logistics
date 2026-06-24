"""Smoke test: driver visibility of assigned/in-transit manifests."""
import frappe
from collections import Counter


def run_smoke_driver_visibility(driver="HR-DRI-2026-00001"):
    drv_user = frappe.db.get_value("Driver", driver, "user")
    print(f"Driver {driver} user link: {drv_user}")

    if drv_user:
        frappe.set_user(drv_user)

    from ch_logistics.api import transfer_manifest_api as tapi
    rows = tapi.get_driver_assignments()
    print(f"\nDriver app renders {len(rows)} manifest cards:")
    for r in rows:
        print(f"  {r['name']:<20} status={r['status']:<15} "
              f"bucket={r.get('bucket', '-'):<17} trip={r.get('trip', '-')}")

    print("\nBucket counts:", dict(Counter(r['bucket'] for r in rows)))

    siblings = frappe.get_all(
        "CH Transfer Manifest",
        filters={"driver": driver, "status": "Assigned", "docstatus": 1},
        fields=["name", "trip"],
    )
    print(f"\nSibling pool for bulk-reject ({len(siblings)} entries):")
    for s in siblings:
        print(f"  {s['name']:<20} trip={s.get('trip', '-')}")

    return {"count": len(rows), "rows": rows}
