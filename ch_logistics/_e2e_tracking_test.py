"""E2E smoke test for customer track-and-trace + notifications.
Run: bench --site erpnext.local execute ch_logistics._e2e_tracking_test.run
"""
import frappe

from ch_logistics.api import customer_tracking as ct

PASS, FAIL = [], []


def _ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ✓ " if cond else "  ✗ ") + label)


def _throws(fn, label):
    try:
        fn()
        _ok(False, label + " (expected throw)")
    except Exception:
        _ok(True, label)


def run():
    frappe.set_user("Administrator")
    m = frappe.db.get_value("CH Transfer Manifest", {"tracking_token": ["is", "set"]},
                            ["name", "tracking_token", "status"], as_dict=True)
    if not m:
        print("  (no tokenized manifest on site — skipping)")
        return {"passed": 0, "failed": 0}

    print(f"\n[1] Public tracking payload ({m.name})")
    data = ct.get_public_tracking(m.tracking_token)
    _ok(data.get("order_id") == m.name, "payload returns correct order id")
    _ok("status" in data and "timeline" in data and len(data["timeline"]) == 4,
        "payload has public status + 4-step timeline")
    _ok("grand_total" not in data and "freight_amount" not in data,
        "payload leaks no internal financial fields")
    print("    status:", data["status"], "| eta:", data.get("eta"))

    print("\n[2] Bad token rejected")
    _throws(lambda: ct.get_public_tracking("not-a-real-token"), "invalid token throws")

    print("\n[3] Destination notification (best-effort)")
    res = ct.notify_destination(m.name, "out_for_delivery")
    _ok(isinstance(res, dict) and "ok" in res, "notify_destination returns a result dict")
    print("    notify result:", res)

    print("\n[4] Public page context")
    frappe.form_dict = frappe._dict(id=m.tracking_token)
    ctx = frappe._dict()
    from ch_logistics.www.track.index import get_context
    get_context(ctx)
    _ok(ctx.get("found") is True and ctx.get("t"), "www/track get_context resolves manifest")

    print(f"\n==== RESULT: {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        for f in FAIL:
            print("   FAILED:", f)
        raise Exception(f"{len(FAIL)} tracking e2e checks failed")
    return {"passed": len(PASS), "failed": len(FAIL)}
