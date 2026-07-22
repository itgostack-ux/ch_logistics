"""Store-scope guard shim for ch_logistics.

Mirrors ``ch_pos.api.scope_guard`` / ``gofix.scope_guard`` — thin wrapper
around the central ``ch_erp15.ch_erp15.scope.assert_user_has_store_scope``
so logistics dispatcher actions respect CH User Scope exactly like POS and
GoFix do.

Enforcement is gated by **CH Logistics Settings → Enforce Store Scope**
(default off) because scope is resolved from CH User Scope rows and most
logistics users have none yet. Roll-out: load CH User Scope for every
dispatcher, then flip the setting — the guard turns fail-closed instantly
(System Manager / Administrator always bypass, drivers are unaffected
because driver endpoints authenticate via their Driver link, not store
scope).
"""

from __future__ import annotations

import frappe


def _enforcement_enabled() -> bool:
    try:
        return bool(
            frappe.db.get_single_value("CH Logistics Settings", "enforce_store_scope")
        )
    except Exception:
        return False


def assert_scope(store: str | None = None, company: str | None = None,
                 warehouse: str | None = None, msg: str | None = None) -> None:
    """Fail-closed store/warehouse/company scope assertion (settings-gated)."""
    if not _enforcement_enabled():
        return
    from ch_erp15.ch_erp15.scope import assert_user_has_store_scope

    assert_user_has_store_scope(
        store=store, company=company, warehouse=warehouse, msg=msg
    )


def assert_manifest_scope(manifest: str | dict, msg: str | None = None) -> None:
    """Assert the session user's scope covers a manifest's source location.

    Accepts a manifest name or a dict already carrying source_store /
    source_warehouse / company.
    """
    if not _enforcement_enabled():
        return
    if isinstance(manifest, str):
        manifest = frappe.db.get_value(
            "CH Transfer Manifest",
            manifest,
            ["source_store", "source_warehouse", "company"],
            as_dict=True,
        ) or {}
    assert_scope(
        store=manifest.get("source_store"),
        warehouse=manifest.get("source_warehouse"),
        company=manifest.get("company"),
        msg=msg,
    )
