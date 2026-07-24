"""Central role registry for ch_logistics.

Single source of truth for every role-based gate in the logistics app.
Nothing outside this module should hardcode a role name — API modules ask
``user_has("<function_key>")`` / ``require("<function_key>")`` instead.

Resolution order per function key:

1. **CH Logistics Settings → Role Matrix** child rows (``CH Logistics Role
   Rule``): when at least one row exists for a key, those rows ARE the
   allowed role set for that key. Admins can therefore re-map any gate
   without a code change.
2. **DEFAULT_ROLE_MATRIX** below: the shipped defaults, used when the
   settings table has no rows for that key (fresh sites, or keys the admin
   never customised).

``System Manager`` and ``Administrator`` always bypass — same convention as
``ch_erp15.scope`` (the central store-scope guard).

Function keys
─────────────
Stage keys (manifest lifecycle transitions — used by transfer_manifest_api):
  create_manifest, assign_driver, start_pickup, mark_reached_destination, reject_manifest,
  complete_delivery, driver_close_manifest, accept_delivery, close_manifest,
  initiate_recall, confirm_return

Console / override keys:
  ops_control            – Control-Tower mutations (attach to Started trip,
                           dynamic stop insert, recall approval …)
  ops_view               – ops-wide list endpoints (see all manifests/trips,
                           not just own-driver rows)
  head_override          – Logistics-Head-only overrides (close trip with
                           open exceptions, close_as_head)
  resequence_override    – resequence stops on a Started trip
  app_access             – logistics app screen access (permissions.py)
  report_ops             – report default widens from own-driver to all
  digest_recipients      – roles that receive the daily logistics digest
  digest_preview         – may call preview_digest
  rejection_dispatcher_notify – dispatcher realtime alert on rejection
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint

# Roles that bypass every logistics gate (matches ch_erp15.scope._BYPASS_ROLES).
# Shipped defaults — exactly the pre-centralisation behaviour of each call
# site. "Logistic Head" (legacy misspelling) is retained as an accepted
# alias so any site that hand-created the misspelt role keeps working.
DEFAULT_ROLE_MATRIX: dict[str, set[str]] = {
    "create_manifest": {"Delivery Manager", "Stock Manager", "Operations Manager"},
    # Outward stages — source-store dispatch lane
    "assign_driver": {"Delivery Manager", "Stock Manager", "Store Manager"},
    "start_pickup": {"Delivery Manager", "Delivery User", "Stock Manager"},
    "mark_reached_destination": {"Delivery Manager", "Delivery User", "Stock Manager"},
    "reject_manifest": {"Delivery Manager", "Delivery User", "Stock Manager"},
    "complete_delivery": {"Delivery User", "Delivery Manager"},
    "driver_close_manifest": {"Delivery User", "Delivery Manager"},
    # Inward stages — destination-store receipt lane
    "accept_delivery": {"Store Manager", "Stock Manager"},
    "close_manifest": {"Store Manager", "Stock Manager"},
    # Reversal — manager-only
    "initiate_recall": {"Stock Manager", "Delivery Manager", "Store Manager"},
    "confirm_return": {"Stock Manager", "Delivery Manager"},
    # Console / overrides
    "ops_control": {"Operations Manager", "Delivery Manager", "Logistics Head", "Logistic Head"},
    "ops_view": {"Delivery Manager", "Delivery User", "Operations Manager", "Logistics Head"},
    "driver_override": {"Delivery Manager", "Stock Manager", "Operations Manager", "Logistics Head", "Logistic Head"},
    "tracking_view": {"Delivery Manager", "Operations Manager", "Logistics Head", "Logistic Head"},
    "courier_poll": {"Delivery Manager", "Operations Manager", "Logistics Head", "Logistic Head"},
    "ewaybill_sync": {"Delivery Manager", "Stock Manager", "Operations Manager"},
    "head_override": {"Logistics Head", "Logistic Head"},
    "resequence_override": {"Logistics Head", "Logistic Head", "Operations Manager"},
    "app_access": {"Logistics Manager", "Logistics User"},
    "report_ops": {"Delivery Manager", "Stock Manager", "Operations Manager"},
    "digest_recipients": {"Delivery Manager", "Stock Manager"},
    "digest_preview": {"Delivery Manager"},
    "rejection_dispatcher_notify": {"Logistics Manager"},
}

# Roles referenced anywhere in the matrix that are NOT created by any
# doctype permission JSON — ensure_roles() provisions these on migrate.
_PROVISIONED_ROLES = ("Logistics Head", "Logistics Manager", "Logistics User")


def _settings_matrix() -> dict[str, set[str]]:
    """Role matrix from CH Logistics Settings, keyed by function_key.

    Tolerates the table/doctype not existing yet (mid-migrate on old
    sites) by returning an empty mapping.
    """
    try:
        settings = frappe.get_cached_doc("CH Logistics Settings")
    except Exception:
        return {}
    matrix: dict[str, set[str]] = {}
    for row in settings.get("role_matrix") or []:
        key = (row.get("function_key") or "").strip()
        role = (row.get("role") or "").strip()
        if key in DEFAULT_ROLE_MATRIX and role:
            matrix.setdefault(key, set()).add(role)
    return matrix


def get_roles_for(function_key: str) -> set[str]:
    """Allowed role set for a function key (settings override → defaults)."""
    if function_key not in DEFAULT_ROLE_MATRIX:
        return set()
    configured = _settings_matrix().get(function_key)
    if configured:
        return configured
    return set(DEFAULT_ROLE_MATRIX.get(function_key, set()))


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
    """Immutable Administrator/System Manager bypass.

    Administrator is a Frappe principal, not a role.  System Manager cannot
    be removed from this bypass through the editable role matrix.
    """
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    if not user or user == "Guest":
        return False
    return "System Manager" in set(frappe.get_roles(user))


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
    if function_key not in DEFAULT_ROLE_MATRIX or not user or user == "Guest":
        return False
    if is_privileged(user):
        return True
    user_roles = set(frappe.get_roles(user))
    needed = get_roles_for(function_key)
    if not needed:
        return False
    return bool(user_roles & needed)


def require(function_key: str, action_label: str | None = None) -> None:
    """Raise PermissionError unless the session user holds a role for the key."""
    if user_has(function_key):
        return
    needed = get_roles_for(function_key)
    frappe.throw(
        _("You do not have the required role to <b>{0}</b>. Required: {1}").format(
            action_label or function_key.replace("_", " ").title(),
            ", ".join(sorted(needed)) or _("(none configured)"),
        ),
        frappe.PermissionError,
        title=_("Logistics — Role Required"),
    )


@frappe.whitelist()
def get_my_capabilities():
    """Function-key → bool map for the session user (client-side UI gating).

    Purely cosmetic on the client — every server endpoint re-checks via
    require()/user_has().
    """
    return {key: user_has(key) for key in DEFAULT_ROLE_MATRIX}


def ensure_roles():
    """Create Role records referenced by the matrix that no doctype JSON
    provisions (after_migrate hook)."""
    for role in _PROVISIONED_ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
                ignore_permissions=True
            )
