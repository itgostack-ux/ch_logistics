"""Single source of truth for "which Driver record does the current user own?"

Historically every API module (`driver_api`, `tracking_api`, `logistics_api`,
`transfer_manifest_api`, `rejection_api`, `report_utils`, …) had its own copy
of this lookup, and all of them shared the same two defects:

* the fallback ``frappe.db.get_value("Driver", {"employee": user}, "name")``
  was comparing a User name against a Link-to-Employee column, so it never
  matched anything in practice;
* there was no story for ``Administrator`` / sysadmins opening the driver
  app for testing — they always hit "No Driver record is linked to your user
  account.".

This module fixes both. The resolution chain is:

1. ``Driver.user == frappe.session.user`` — the documented Frappe pattern.
2. Employee whose ``user_id == frappe.session.user`` → Driver whose
   ``employee == <that Employee>``. This is how ERPNext canonically links a
   User to a Driver through HR (`Driver.validate` even auto-copies the
   Employee's ``user_id`` into ``Driver.user``).
3. For Administrator only, lazily provision a singleton bench-admin Driver
   so the desk delivery app is usable out of the box for testing and
   demos. Real drivers must still be onboarded normally — we deliberately
   do **not** auto-create drivers for arbitrary system users.

Every API surface should call :func:`resolve_current_driver` instead of
re-implementing the lookup.
"""
from __future__ import annotations

import frappe
from frappe import _


_ADMIN_DRIVER_FULL_NAME = "Bench Admin (Auto)"


def _lookup_by_user(user: str) -> str | None:
    """Direct ``Driver.user`` match — the Frappe-native binding."""
    return frappe.db.get_value("Driver", {"user": user}, "name")


def _lookup_by_employee(user: str) -> str | None:
    """User → Employee.user_id → Driver.employee.

    Replaces the legacy (and broken) ``{"employee": user}`` filter that was
    comparing a User name against an Employee link column.
    """
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    if not employee:
        return None
    return frappe.db.get_value("Driver", {"employee": employee}, "name")


def _provision_admin_driver() -> str:
    """Create (or re-use) the singleton Driver bound to Administrator.

    Idempotent: if Administrator already has a Driver via either lookup
    path, we just return it. Otherwise we mint a fresh ``HR-DRI-...``
    record, bind ``user = "Administrator"``, and return its name. The
    record is created as Administrator (the only user that can reach this
    branch), so permission checks are not an issue.
    """
    existing = _lookup_by_user("Administrator") or _lookup_by_employee("Administrator")
    if existing:
        return existing

    doc = frappe.new_doc("Driver")
    doc.full_name = _ADMIN_DRIVER_FULL_NAME
    doc.user = "Administrator"
    doc.status = "Active"
    # Driver.validate clobbers ``self.user`` from Employee.user_id when an
    # Employee link is set — leaving employee empty is intentional so the
    # admin link survives validation.
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def resolve_current_driver(
    throw: bool = False,
    auto_provision_admin: bool = True,
) -> str | None:
    """Return the Driver name for ``frappe.session.user`` or ``None``.

    Parameters
    ----------
    throw:
        If True and no Driver can be resolved, raise the standard
        "Not a Driver" message so the desk shows a clean dialog.
    auto_provision_admin:
        If True (default) and the caller is ``Administrator``, lazily
        create a singleton bench-admin Driver so testing/demoing the
        driver app does not require an HR onboarding step. Disable from
        callers that must NOT cause side-effects (e.g. read-only report
        helpers).
    """
    user = frappe.session.user
    if not user or user in ("Guest", ""):
        if throw:
            frappe.throw(
                _("Please sign in to use the driver app."),
                title=_("Not Signed In"),
            )
        return None

    driver = _lookup_by_user(user) or _lookup_by_employee(user)
    if driver:
        return driver

    if user == "Administrator" and auto_provision_admin:
        return _provision_admin_driver()

    if throw:
        frappe.throw(
            _("No Driver record is linked to your user account. "
              "Please ask an administrator to create a Driver for you "
              "(HR → Driver) and link it to your user."),
            title=_("Not a Driver"),
        )
    return None
