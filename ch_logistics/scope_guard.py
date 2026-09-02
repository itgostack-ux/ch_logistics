"""Fail-closed CH User Scope enforcement for Logistics APIs."""

from __future__ import annotations

import frappe
from frappe import _

from ch_logistics import roles as role_registry


def _scope(user: str | None = None) -> dict:
    user = user or frappe.session.user
    if role_registry.is_privileged(user):
        return {"bypass": True, "stores": set(), "warehouses": set(), "companies": set()}
    if not user or user == "Guest":
        return {"bypass": False, "stores": set(), "warehouses": set(), "companies": set()}
    try:
        from ch_erp15.ch_erp15.scope import get_user_scope

        resolved = get_user_scope(user)
    except (ImportError, ModuleNotFoundError):
        resolved = {}
    return {
        "bypass": bool(resolved.get("bypass")),
        "stores": set(resolved.get("stores") or set()),
        "warehouses": set(resolved.get("warehouses") or set()),
        "companies": set(resolved.get("companies") or set()),
        # Explicit CH User Scope company rows only — never the companies
        # back-filled from store grants. An explicit grant covers the whole
        # company, including its non-store warehouses (hubs, transit).
        "direct_companies": set(resolved.get("direct_companies") or set()),
    }


def is_in_scope(
    *,
    store: str | None = None,
    company: str | None = None,
    warehouse: str | None = None,
    user: str | None = None,
) -> bool:
    """Return whether a location anchor is visible to the caller."""
    resolved = _scope(user)
    if resolved["bypass"]:
        return True
    locations = resolved["stores"] | resolved["warehouses"]
    direct_companies = resolved.get("direct_companies") or set()
    if store or warehouse:
        if locations and (
            (store and store in locations) or (warehouse and warehouse in locations)
        ):
            return True
        # Scope warehouses derive from stores, so hub/transit warehouses can
        # never appear in them. An EXPLICIT company grant covers every
        # location of that company, non-store warehouses included.
        if direct_companies:
            anchor_company = None
            if warehouse:
                anchor_company = frappe.get_cached_value("Warehouse", warehouse, "company")
            elif store:
                anchor_company = frappe.db.get_value("CH Store", store, "company")
            return bool(anchor_company) and anchor_company in direct_companies
        return False
    if company:
        if company in direct_companies:
            return True
        if locations:
            return False
        return company in resolved["companies"]
    return False


def assert_scope(store: str | None = None, company: str | None = None,
                 warehouse: str | None = None, msg: str | None = None) -> None:
    """Fail-closed store/warehouse/company scope assertion."""
    if is_in_scope(store=store, company=company, warehouse=warehouse):
        return
    frappe.throw(
        msg or _("This logistics record is outside your assigned location scope."),
        frappe.PermissionError,
    )


def build_scope_sql(
    *,
    location_fields: tuple[str, ...],
    company_field: str | None = None,
    prefix: str = "logistics_scope",
    user: str | None = None,
) -> tuple[str, dict]:
    """Return a parameterized SQL predicate for scoped list/aggregate APIs."""
    resolved = _scope(user)
    if resolved["bypass"]:
        return "1=1", {}

    clauses: list[str] = []
    params: dict = {}

    locations = sorted(resolved["stores"] | resolved["warehouses"])
    if locations and location_fields:
        loc_params = {f"{prefix}_location_{idx}": value for idx, value in enumerate(locations)}
        placeholders = ", ".join(f"%({key})s" for key in loc_params)
        clauses.extend(f"{field} IN ({placeholders})" for field in location_fields)
        params.update(loc_params)

    # An EXPLICIT company grant covers every location of that company —
    # including hub/transit warehouses, which derive from no store and can
    # therefore never appear in the location set.
    direct_companies = sorted(resolved.get("direct_companies") or ())
    if direct_companies and company_field:
        dc_params = {f"{prefix}_direct_co_{idx}": value for idx, value in enumerate(direct_companies)}
        placeholders = ", ".join(f"%({key})s" for key in dc_params)
        clauses.append(f"{company_field} IN ({placeholders})")
        params.update(dc_params)

    if clauses:
        return f"({' OR '.join(clauses)})", params

    companies = sorted(resolved["companies"])
    if companies and company_field:
        params = {f"{prefix}_company_{idx}": value for idx, value in enumerate(companies)}
        placeholders = ", ".join(f"%({key})s" for key in params)
        return f"{company_field} IN ({placeholders})", params

    return "1=0", {}


def assert_manifest_scope(
    manifest: str | dict,
    msg: str | None = None,
    side: str = "source",
) -> None:
    """Assert scope over a manifest's requested location side.

    Accepts a manifest name or a dict already carrying source_store /
    destination_store / warehouses / company.
    """
    if isinstance(manifest, str):
        manifest = frappe.db.get_value(
            "CH Transfer Manifest",
            manifest,
            [
                "source_store",
                "source_warehouse",
                "destination_store",
                "destination_warehouse",
                "company",
            ],
            as_dict=True,
        ) or {}
    if side not in {"source", "destination", "both", "either"}:
        frappe.throw(_("Unknown manifest scope side."), frappe.PermissionError)
    source_ok = is_in_scope(
        store=manifest.get("source_store"),
        warehouse=manifest.get("source_warehouse"),
        company=manifest.get("company"),
    )
    destination_ok = is_in_scope(
        store=manifest.get("destination_store"),
        warehouse=manifest.get("destination_warehouse"),
        company=manifest.get("company"),
    )
    allowed = {
        "source": source_ok,
        "destination": destination_ok,
        "both": source_ok and destination_ok,
        "either": source_ok or destination_ok,
    }[side]
    if not allowed:
        frappe.throw(
            msg or _("This manifest is outside your assigned location scope."),
            frappe.PermissionError,
        )


def assert_trip_scope(trip: str | dict, msg: str | None = None) -> None:
    if isinstance(trip, str):
        trip = frappe.db.get_value(
            "CH Logistics Trip", trip, ["hub_warehouse", "company"], as_dict=True
        ) or {}
    assert_scope(
        warehouse=trip.get("hub_warehouse"),
        company=trip.get("company"),
        msg=msg,
    )
