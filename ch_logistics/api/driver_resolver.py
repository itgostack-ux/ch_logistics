"""Single source of truth for "which Driver record does the current user own?"

Historically every API module (`driver_api`, `tracking_api`, `logistics_api`,
`transfer_manifest_api`, `rejection_api`, `report_utils`, …) had its own copy
of this lookup, which produced inconsistent ownership checks:

* the fallback ``frappe.db.get_value("Driver", {"employee": user}, "name")``
  was comparing a User name against a Link-to-Employee column, so it never
  matched anything in practice;
Privileged operations use the explicit role/scope override paths; they are not
silently assigned a fabricated Driver identity. The ownership chain is:

1. ``Driver.user == frappe.session.user`` — the documented Frappe pattern.
2. Employee whose ``user_id == frappe.session.user`` → Driver whose
   ``employee == <that Employee>``. This is how ERPNext canonically links a
   User to a Driver through HR (`Driver.validate` even auto-copies the
   Employee's ``user_id`` into ``Driver.user``).
3. If neither link exists, fail closed. Driver records are provisioned only
   through the normal onboarding workflow; read APIs never create identities.

Every API surface should call :func:`resolve_current_driver` instead of
re-implementing the lookup.
"""
from __future__ import annotations

import frappe
from frappe import _


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


def resolve_current_driver(throw: bool = False) -> str | None:
    """Return the Driver name for ``frappe.session.user`` or ``None``.

    Parameters
    ----------
    throw:
        If True and no Driver can be resolved, raise the standard
        "Not a Driver" message so the desk shows a clean dialog.
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

    if throw:
        frappe.throw(
            _("No Driver record is linked to your user account. "
              "Please ask an administrator to create a Driver for you "
              "(HR → Driver) and link it to your user."),
            title=_("Not a Driver"),
        )
    return None


def assert_manifest_driver_access(manifest, *, scope_side: str = "source") -> None:
    """Allow the assigned driver or a scoped, configured override role."""
    from ch_logistics import roles as role_registry
    from ch_logistics import scope_guard

    doc = (
        frappe.get_doc("CH Transfer Manifest", manifest)
        if isinstance(manifest, str)
        else manifest
    )
    if role_registry.is_privileged():
        return

    current_driver = resolve_current_driver(throw=False)
    trip_driver = None
    if doc.get("trip"):
        trip_driver = frappe.db.get_value("CH Logistics Trip", doc.trip, "driver")
    if (
        current_driver
        and doc.get("driver") == current_driver
        and (not trip_driver or trip_driver == current_driver)
    ):
        return

    if role_registry.user_has("driver_override"):
        scope_guard.assert_manifest_scope(doc.as_dict(), side=scope_side)
        return

    frappe.throw(
        _("You can only act on manifests assigned to your Driver profile."),
        frappe.PermissionError,
    )


def assert_trip_driver_access(trip, *, override_key: str = "driver_override") -> None:
    """Allow the assigned driver or a scoped role configured for override."""
    from ch_logistics import roles as role_registry
    from ch_logistics import scope_guard

    doc = frappe.get_doc("CH Logistics Trip", trip) if isinstance(trip, str) else trip
    if role_registry.is_privileged():
        return
    current_driver = resolve_current_driver(throw=False)
    if current_driver and doc.get("driver") == current_driver:
        return
    if role_registry.user_has(override_key):
        scope_guard.assert_trip_scope(doc.as_dict())
        return
    frappe.throw(
        _("You can only access trips assigned to your Driver profile."),
        frappe.PermissionError,
    )
