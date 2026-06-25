"""One-shot script: create a desk-login test Driver account.

Run with:
    bench --site erpnext.local execute ch_logistics._create_test_driver.run

Idempotent: re-running just resets the password and re-prints the credentials.
"""
from __future__ import annotations

import frappe

EMAIL = "driver1@bestbuy.local"
FIRST_NAME = "Test"
LAST_NAME = "Driver"
PASSWORD = "Driver@123"   # change after first login
ROLES = [
    "Delivery User",
    "Delivery Manager",   # so the driver can see/manage trips end-to-end
    "Stock User",         # needed for warehouse-side reads on the desk
    "Employee",           # desk login baseline
    "Driver",
]


def run():
    user = _ensure_user()
    driver = _ensure_driver(user)
    _set_password(user)
    _print_summary(user, driver)


# ---------------------------------------------------------------------------
def _ensure_user() -> str:
    if frappe.db.exists("User", EMAIL):
        u = frappe.get_doc("User", EMAIL)
        existing = {r.role for r in u.roles}
        added = [r for r in ROLES if r not in existing]
        for r in added:
            u.append("roles", {"role": r})
        if added:
            u.flags.ignore_permissions = True
            u.save(ignore_permissions=True)
        return u.name

    u = frappe.new_doc("User")
    u.email = EMAIL
    u.first_name = FIRST_NAME
    u.last_name = LAST_NAME
    u.send_welcome_email = 0
    u.enabled = 1
    u.user_type = "System User"   # required for desk login
    u.language = "en"
    u.time_zone = "Asia/Kolkata"
    for r in ROLES:
        u.append("roles", {"role": r})
    u.flags.ignore_permissions = True
    u.insert(ignore_permissions=True)
    return u.name


def _ensure_driver(user: str) -> str:
    # Reuse via the same resolution chain the app uses.
    existing = frappe.db.get_value("Driver", {"user": user}, "name")
    if existing:
        return existing

    d = frappe.new_doc("Driver")
    d.full_name = f"{FIRST_NAME} {LAST_NAME}"
    d.user = user
    d.status = "Active"
    d.cell_number = "+91-9000000001"
    d.flags.ignore_permissions = True
    d.insert(ignore_permissions=True)
    return d.name


def _set_password(user: str) -> None:
    from frappe.utils.password import update_password
    update_password(user, PASSWORD)


def _print_summary(user: str, driver: str) -> None:
    frappe.db.commit()
    site = frappe.local.site or "<site>"
    print("\n" + "=" * 70)
    print("  Test driver account ready")
    print("=" * 70)
    print(f"  Site         : {site}")
    print(f"  Login URL    : http://localhost:8000/login")
    print(f"  Username     : {user}")
    print(f"  Password     : {PASSWORD}")
    print(f"  Driver record: {driver}")
    print(f"  Roles        : {', '.join(ROLES)}")
    print("-" * 70)
    print("  After login:")
    print("    1. Open  /app/delivery-app   (the screen you screenshotted)")
    print("    2. Or    /my-trips           (mobile portal page)")
    print("    3. Assign a trip to this driver from")
    print(f"         /app/ch-logistics-trip → set Driver = {driver}")
    print("=" * 70 + "\n")
