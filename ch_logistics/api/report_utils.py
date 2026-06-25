"""Shared helpers for CH Logistics Script Reports.

Keeps each report's ``execute`` short: consistent column builders, standard
company/date/store/driver filter conditions, and a current-driver resolver so
delivery-staff reports default to the logged-in driver.
"""
import frappe
from frappe.utils import flt  # noqa: F401  (re-exported for report convenience)


def resolve_company(filters):
    """The single active-company model used across the suite: use the filter if
    set, else fall back to the user's active company (the global switcher /
    company_lock default). Reports are therefore always company-scoped, never
    leaking across companies when the dropdown is left blank."""
    return (filters or {}).get("company") or frappe.defaults.get_user_default("Company")


def col(label, fieldname, fieldtype="Data", width=130, options=None, precision=None):
    c = {"label": label, "fieldname": fieldname, "fieldtype": fieldtype, "width": width}
    if options:
        c["options"] = options
    if precision is not None:
        c["precision"] = precision
    return c


def manifest_conditions(filters, alias="m", date_field="manifest_date"):
    """Standard WHERE fragments for CH Transfer Manifest queries."""
    f = filters or {}
    cond, vals = ["1=1"], {}
    add = lambda c, k, v: (cond.append(c), vals.__setitem__(k, v))
    company = resolve_company(f)
    if company:
        add(f"{alias}.company = %(company)s", "company", company)
    if f.get("from_date"):
        add(f"{alias}.{date_field} >= %(from_date)s", "from_date", f["from_date"])
    if f.get("to_date"):
        add(f"{alias}.{date_field} <= %(to_date)s", "to_date", f["to_date"])
    if f.get("status"):
        add(f"{alias}.status = %(status)s", "status", f["status"])
    if f.get("driver"):
        add(f"{alias}.driver = %(driver)s", "driver", f["driver"])
    if f.get("direction"):
        add(f"{alias}.direction = %(direction)s", "direction", f["direction"])
    if f.get("source_store"):
        add(f"{alias}.source_store = %(source_store)s", "source_store", f["source_store"])
    if f.get("destination_store"):
        add(f"{alias}.destination_store = %(destination_store)s", "destination_store",
            f["destination_store"])
    return " AND ".join(cond), vals


def trip_conditions(filters, alias="t"):
    """Standard WHERE fragments for CH Logistics Trip queries."""
    f = filters or {}
    cond, vals = ["1=1"], {}
    add = lambda c, k, v: (cond.append(c), vals.__setitem__(k, v))
    company = resolve_company(f)
    if company:
        add(f"{alias}.company = %(company)s", "company", company)
    if f.get("from_date"):
        add(f"{alias}.trip_date >= %(from_date)s", "from_date", f["from_date"])
    if f.get("to_date"):
        add(f"{alias}.trip_date <= %(to_date)s", "to_date", f["to_date"])
    if f.get("status"):
        add(f"{alias}.status = %(status)s", "status", f["status"])
    if f.get("driver"):
        add(f"{alias}.driver = %(driver)s", "driver", f["driver"])
    return " AND ".join(cond), vals


def current_driver():
    """Driver linked to the logged-in user, or None for ops/admin.

    Read-only callers (reports) skip the Administrator auto-provision
    side-effect so opening a report never silently mints a Driver record.
    """
    from ch_logistics.api.driver_resolver import resolve_current_driver
    return resolve_current_driver(throw=False, auto_provision_admin=False)


def is_ops_user():
    roles = set(frappe.get_roles(frappe.session.user))
    return bool({"System Manager", "Delivery Manager", "Stock Manager",
                 "Operations Manager"} & roles)
