"""Central permission gate for ch_logistics.

Every logistics action is a capability key (``user_has("assign_driver")`` /
``require("ops_control")``). A key is granted by a DocType permission set in
**Role Permissions Manager**, never by a role name in code:

    CAPABILITIES[key] = (doctype, permission type)

so who may assign drivers, run the control tower or receive a delivery is
changed in Permission Manager alone. The mapping separates drivers from
dispatch by permission type: drivers have Write on manifests, dispatch has
Create on trips, recall needs Cancel, receiving needs Submit on Stock Entry.

Administrator always passes. Beyond that, "privileged" (the server-side
validation bypass) is whoever Permission Manager allows to Delete
CH Logistics Settings.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint

_MANIFEST = "CH Transfer Manifest"
_TRIP = "CH Logistics Trip"

CAPABILITIES: dict[str, tuple[str, str]] = {
    # Driver steps: Write on the manifest (endpoints also require the
    # caller to be the manifest's assigned driver).
    "start_pickup": (_MANIFEST, "write"),
    "mark_reached_destination": (_MANIFEST, "write"),
    "reject_manifest": (_MANIFEST, "write"),
    "complete_delivery": (_MANIFEST, "write"),
    "driver_close_manifest": (_MANIFEST, "write"),
    "create_manifest": (_MANIFEST, "create"),
    # Reversal
    "initiate_recall": (_MANIFEST, "cancel"),
    "confirm_return": (_MANIFEST, "cancel"),
    # Store receipt posts stock
    "accept_delivery": ("Stock Entry", "submit"),
    "close_manifest": ("Stock Entry", "submit"),
    # Dispatch / control tower: Create on trips (drivers only write them)
    "assign_driver": (_TRIP, "create"),
    "ops_control": (_TRIP, "create"),
    "ops_view": (_TRIP, "create"),
    "driver_override": (_TRIP, "create"),
    "tracking_view": (_TRIP, "create"),
    "courier_poll": (_TRIP, "create"),
    "ewaybill_sync": (_TRIP, "create"),
    "resequence_override": (_TRIP, "create"),
    "report_ops": (_TRIP, "create"),
    "digest_recipients": (_TRIP, "create"),
    "digest_preview": (_TRIP, "create"),
    "rejection_dispatcher_notify": (_TRIP, "create"),
    "head_override": (_TRIP, "delete"),
    "app_access": (_TRIP, "read"),
}

_PRIVILEGE = ("CH Logistics Settings", "delete")


def _roles_with(doctype: str, ptype: str) -> set[str]:
    """Roles Permission Manager grants ``ptype`` on ``doctype`` (level 0)."""
    source = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": doctype}) else "DocPerm"
    return set(frappe.get_all(
        source,
        filters={"parent": doctype, "permlevel": 0, ptype: 1},
        pluck="role",
    ))


def get_roles_for(function_key: str) -> set[str]:
    """Roles that hold a capability, read from Permission Manager."""
    if function_key not in CAPABILITIES:
        return set()
    return _roles_with(*CAPABILITIES[function_key])


def get_int_setting(fieldname: str, default: int, minimum: int = 1) -> int:
    """Read a positive CH Logistics Settings value with migrate-safe fallback.

    CH Logistics Settings is a SINGLE doctype — it has no table, so
    frappe.db.has_column() raises TableMissingError for it. The migrate-safe
    existence check for a Single is the meta field lookup.
    """
    try:
        if not frappe.get_meta("CH Logistics Settings").has_field(fieldname):
            return default
    except Exception:
        return default
    value = cint(frappe.db.get_single_value("CH Logistics Settings", fieldname))
    return value if value >= minimum else default


def get_list_setting(fieldname: str) -> set[str]:
    try:
        value = frappe.db.get_single_value("CH Logistics Settings", fieldname) or ""
    except Exception:
        return set()
    return {part.strip() for part in re.split(r"[,\n]", value) if part.strip()}


def is_privileged(user: str | None = None) -> bool:
    """Administrator, or whoever Permission Manager lets delete CH Logistics Settings."""
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    if not user or user == "Guest":
        return False
    return bool(set(frappe.get_roles(user)) & _roles_with(*_PRIVILEGE))


def filter_notification_users(users) -> list[str]:
    limit = get_int_setting("notification_recipient_limit", 200)
    candidates = sorted({user for user in (users or []) if user})[:limit]
    if not candidates:
        return []
    enabled = frappe.get_all(
        "User",
        filters={
            "name": ("in", candidates),
            "enabled": 1,
            "user_type": "System User",
        },
        pluck="name",
        limit=limit,
    )
    include_privileged = bool(
        get_int_setting("business_notifications_include_privileged", 0, minimum=0)
    )
    excluded_roles = get_list_setting("business_notification_excluded_roles")
    return sorted(
        user
        for user in enabled
        if (include_privileged or not is_privileged(user))
        and not set(frappe.get_roles(user)).intersection(excluded_roles)
    )


def get_notification_role_users(function_key: str) -> list[str]:
    limit = get_int_setting("notification_recipient_limit", 200)
    users = frappe.get_all(
        "Has Role",
        filters={
            "role": ("in", sorted(get_roles_for(function_key))),
            "parenttype": "User",
        },
        pluck="parent",
        limit=limit,
    )
    return filter_notification_users(users)


def get_name_batch(
    doctype: str,
    *,
    filters=None,
    fields=None,
    cursor_key: str,
    limit_field: str,
    default_limit: int,
):
    limit = get_int_setting(limit_field, default_limit)
    cache_key = f"ch_logistics::scheduler_cursor::{cursor_key}"
    cursor = frappe.cache().get_value(cache_key) or ""

    def fetch(after):
        query_filters = list(filters or [])
        if after:
            query_filters.append(["name", ">", after])
        return frappe.get_all(
            doctype,
            filters=query_filters,
            fields=fields or ["name"],
            order_by="name asc",
            limit=limit,
        )

    rows = fetch(cursor)
    if not rows and cursor:
        frappe.cache().delete_value(cache_key)
        rows = fetch("")
    if rows:
        frappe.cache().set_value(cache_key, rows[-1].name, expires_in_sec=604800)
    return rows


def user_has(function_key: str, user: str | None = None) -> bool:
    """True when `user` may exercise `function_key` (bypass roles included)."""
    user = user or frappe.session.user
    if function_key not in CAPABILITIES or not user or user == "Guest":
        return False
    if is_privileged(user):
        return True
    user_roles = set(frappe.get_roles(user))
    needed = get_roles_for(function_key)
    if not needed:
        return False
    return bool(user_roles & needed)


def require(function_key: str, action_label: str | None = None) -> None:
    """Raise PermissionError unless Permission Manager grants the capability."""
    if user_has(function_key):
        return
    doctype, ptype = CAPABILITIES.get(function_key, ("", ""))
    frappe.throw(
        _("You are not permitted to <b>{0}</b>. It needs the {1} permission on {2} "
          "(Role Permissions Manager).").format(
            action_label or function_key.replace("_", " ").title(),
            _(ptype.title()), _(doctype),
        ),
        frappe.PermissionError,
        title=_("Logistics — Not Permitted"),
    )


@frappe.whitelist()
def get_my_capabilities():
    """Function-key → bool map for the session user (client-side UI gating).

    Purely cosmetic on the client — every server endpoint re-checks via
    require()/user_has().
    """
    return {key: user_has(key) for key in CAPABILITIES}
