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
  assign_driver, start_pickup, mark_reached_destination, reject_manifest,
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

import frappe
from frappe import _

# Roles that bypass every logistics gate (matches ch_erp15.scope._BYPASS_ROLES).
BYPASS_ROLES = {"System Manager", "Administrator"}

# Shipped defaults — exactly the pre-centralisation behaviour of each call
# site. "Logistic Head" (legacy misspelling) is retained as an accepted
# alias so any site that hand-created the misspelt role keeps working.
DEFAULT_ROLE_MATRIX: dict[str, set[str]] = {
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
        if key and role:
            matrix.setdefault(key, set()).add(role)
    return matrix


def get_roles_for(function_key: str) -> set[str]:
    """Allowed role set for a function key (settings override → defaults)."""
    configured = _settings_matrix().get(function_key)
    if configured:
        return configured
    return set(DEFAULT_ROLE_MATRIX.get(function_key, set()))


def user_has(function_key: str, user: str | None = None) -> bool:
    """True when `user` may exercise `function_key` (bypass roles included)."""
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    user_roles = set(frappe.get_roles(user))
    if user_roles & BYPASS_ROLES:
        return True
    needed = get_roles_for(function_key)
    if not needed:
        # Unknown / unconfigured key → no extra roles demanded beyond login.
        return True
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
