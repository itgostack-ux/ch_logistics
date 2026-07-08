"""End-to-end checks for the driver FCM push transport.

Exercises the shared Firebase transport (``ch_logistics.api.fcm``) and the
driver send path (``ch_logistics.api.driver_push._send_fcm``) without contacting
Firebase, using the ``fcm_dry_run`` flag. Also verifies invalid-token cleanup.

Run with:
    bench --site erpnext.local execute \\
        ch_logistics.tests.test_fcm_push_e2e.run

Self-cleans via _teardown so re-runs are idempotent.
"""
from __future__ import annotations

import frappe

from ch_logistics.api import driver_push, fcm

TEST_DRIVER = "ZZ-FCM-TEST-DRIVER"


def _mk_device(driver, token):
    doc = frappe.get_doc({
        "doctype": "CH Driver Device",
        "driver": driver,
        "device_id": f"TEST-{token}",
        "platform": "Android",
        "fcm_token": token,
        "is_active": 1,
    })
    doc.flags.ignore_links = True  # TEST_DRIVER is a synthetic, non-existent Driver
    doc.insert(ignore_permissions=True)
    return doc.name


def _teardown():
    for name in frappe.get_all("CH Driver Device", filters={"driver": TEST_DRIVER}, pluck="name"):
        frappe.delete_doc("CH Driver Device", name, force=True, ignore_permissions=True)
    frappe.db.commit()


def run():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    _teardown()
    try:
        # 1. Empty token list is a clean no-op.
        r = fcm.send_to_tokens([], "t", "b")
        check("empty-tokens-noop", r["ok"] is False and r["reason"] == "no-tokens")

        # 2. Dry-run send de-duplicates tokens and reports them as delivered.
        frappe.flags.fcm_dry_run = True
        r = fcm.send_to_tokens(["tokA", "tokB", "tokA"], "t", "b", {"k": 1})
        check("dry-run-dedup-sent", r["ok"] and r["sent"] == 2 and r.get("dry_run"))
        frappe.flags.fcm_dry_run = False

        # 3. Without credentials (and no dry-run) sends are a structured no-op,
        #    never an exception. Skipped if the site actually has Firebase creds.
        if not fcm.is_configured():
            r = fcm.send_to_tokens(["x"], "t", "b")
            check("not-configured-noop", r["ok"] is False and r["reason"] == "not-configured")
        else:
            check("not-configured-noop", True)

        # 4. Driver send path resolves the driver's active tokens and delivers.
        _mk_device(TEST_DRIVER, "DTOKEN1")
        frappe.flags.fcm_dry_run = True
        sent = driver_push._send_fcm(TEST_DRIVER, "Trip", "Assigned", {"type": "x"})
        frappe.flags.fcm_dry_run = False
        check("driver-send-dry-run", sent == 1)

        # 5. Invalid tokens reported by FCM are deactivated + cleared.
        fcm._deactivate_invalid("CH Driver Device", ["DTOKEN1"])
        row = frappe.db.get_value(
            "CH Driver Device",
            {"driver": TEST_DRIVER, "device_id": "TEST-DTOKEN1"},
            ["is_active", "fcm_token"], as_dict=True,
        )
        check("invalid-token-deactivated", row and row.is_active == 0 and not row.fcm_token)
    finally:
        frappe.flags.fcm_dry_run = False
        _teardown()

    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"FCM push: {passed}/{len(results)} passed")
    if passed != len(results):
        raise Exception("FCM push test failures")
    return {"passed": passed, "total": len(results)}
