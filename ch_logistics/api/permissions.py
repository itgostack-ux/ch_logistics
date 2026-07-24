"""Permission helpers exposed via hooks."""
from __future__ import annotations

import frappe


def _driver_for_user(user: str | None) -> str | None:
	if not user or user == "Guest":
		return None
	driver = frappe.db.get_value("Driver", {"user": user}, "name")
	if driver:
		return driver
	employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
	return frappe.db.get_value("Driver", {"employee": employee}, "name") if employee else None


def _scope_clause(table: str, warehouse_field: str, company_field: str, user: str) -> str:
	from ch_logistics import scope_guard

	scope = scope_guard._scope(user)
	if scope.get("bypass"):
		return "1=1"
	locations = sorted(set(scope.get("stores") or ()) | set(scope.get("warehouses") or ()))
	if locations:
		values = ", ".join(frappe.db.escape(value) for value in locations)
		return f"`tab{table}`.`{warehouse_field}` IN ({values})"
	companies = sorted(scope.get("companies") or ())
	if companies:
		values = ", ".join(frappe.db.escape(value) for value in companies)
		return f"`tab{table}`.`{company_field}` IN ({values})"
	return "1=0"


def _trip_visible(doc, user: str, write: bool = False) -> bool:
	from ch_logistics import roles as role_registry, scope_guard

	if role_registry.is_privileged(user):
		return True
	if doc.get("driver") and doc.get("driver") == _driver_for_user(user):
		return True
	capability = "ops_control" if write else "ops_view"
	if not role_registry.user_has(capability, user):
		return False
	return scope_guard.is_in_scope(
		warehouse=doc.get("hub_warehouse"), company=doc.get("company"), user=user
	)


def trip_query_condition(user: str | None = None) -> str:
	from ch_logistics import roles as role_registry

	user = user or frappe.session.user
	if role_registry.is_privileged(user):
		return ""
	clauses = []
	driver = _driver_for_user(user)
	if driver:
		clauses.append(f"`tabCH Logistics Trip`.`driver` = {frappe.db.escape(driver)}")
	if role_registry.user_has("ops_view", user):
		clauses.append(_scope_clause("CH Logistics Trip", "hub_warehouse", "company", user))
	return " OR ".join(f"({clause})" for clause in clauses) or "1=0"


def has_trip_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	user = user or frappe.session.user
	ptype = permission_type or "read"
	return _trip_visible(doc, user, write=ptype in {"write", "create", "delete", "cancel", "submit"})


def _owner_query(doctype: str, user: str) -> str:
	driver = _driver_for_user(user)
	return (
		f"`tab{doctype}`.`driver` = {frappe.db.escape(driver)}"
		if driver else "1=0"
	)


def driver_device_query_condition(user: str | None = None) -> str:
	from ch_logistics import roles as role_registry

	user = user or frappe.session.user
	return "" if role_registry.is_privileged(user) else _owner_query("CH Driver Device", user)


def has_driver_device_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	from ch_logistics import roles as role_registry

	user = user or frappe.session.user
	return role_registry.is_privileged(user) or doc.get("driver") == _driver_for_user(user)


def driver_break_query_condition(user: str | None = None) -> str:
	from ch_logistics import roles as role_registry

	user = user or frappe.session.user
	if role_registry.is_privileged(user):
		return ""
	clauses = [_owner_query("CH Driver Break Log", user)]
	if role_registry.user_has("ops_view", user):
		trip_scope = _scope_clause("CH Logistics Trip", "hub_warehouse", "company", user)
		clauses.append(
			"EXISTS (SELECT 1 FROM `tabCH Logistics Trip` "
			"WHERE `tabCH Logistics Trip`.`name` = `tabCH Driver Break Log`.`trip` "
			f"AND ({trip_scope}))"
		)
	return " OR ".join(f"({clause})" for clause in clauses)


def has_driver_break_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	from ch_logistics import roles as role_registry, scope_guard

	user = user or frappe.session.user
	if role_registry.is_privileged(user) or doc.get("driver") == _driver_for_user(user):
		return True
	if not role_registry.user_has("ops_view", user) or not doc.get("trip"):
		return False
	trip = frappe.db.get_value(
		"CH Logistics Trip", doc.get("trip"), ["hub_warehouse", "company"], as_dict=True
	) or {}
	return scope_guard.is_in_scope(
		warehouse=trip.get("hub_warehouse"), company=trip.get("company"), user=user
	)


def driver_location_query_condition(user: str | None = None) -> str:
	from ch_logistics import roles as role_registry

	user = user or frappe.session.user
	if role_registry.is_privileged(user):
		return ""
	clauses = [_owner_query("CH Driver Location", user)]
	if role_registry.user_has("tracking_view", user):
		trip_scope = _scope_clause("CH Logistics Trip", "hub_warehouse", "company", user)
		clauses.append(
			"EXISTS (SELECT 1 FROM `tabCH Logistics Trip` "
			"WHERE `tabCH Logistics Trip`.`name` = `tabCH Driver Location`.`trip` "
			f"AND ({trip_scope}))"
		)
	return " OR ".join(f"({clause})" for clause in clauses)


def has_driver_location_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	from ch_logistics import roles as role_registry, scope_guard

	user = user or frappe.session.user
	if role_registry.is_privileged(user) or doc.get("driver") == _driver_for_user(user):
		return True
	if not role_registry.user_has("tracking_view", user) or not doc.get("trip"):
		return False
	trip = frappe.db.get_value(
		"CH Logistics Trip", doc.get("trip"), ["hub_warehouse", "company"], as_dict=True
	) or {}
	return scope_guard.is_in_scope(
		warehouse=trip.get("hub_warehouse"), company=trip.get("company"), user=user
	)


def manifest_rejection_query_condition(user: str | None = None) -> str:
	from ch_logistics import roles as role_registry

	user = user or frappe.session.user
	if role_registry.is_privileged(user):
		return ""
	clauses = [_owner_query("CH Manifest Rejection", user)]
	if role_registry.user_has("ops_view", user):
		trip_scope = _scope_clause("CH Logistics Trip", "hub_warehouse", "company", user)
		clauses.append(
			"EXISTS (SELECT 1 FROM `tabCH Logistics Trip` "
			"WHERE `tabCH Logistics Trip`.`name` = `tabCH Manifest Rejection`.`trip` "
			f"AND ({trip_scope}))"
		)
	return " OR ".join(f"({clause})" for clause in clauses)


def has_manifest_rejection_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	from ch_logistics import roles as role_registry, scope_guard

	user = user or frappe.session.user
	if role_registry.is_privileged(user) or doc.get("driver") == _driver_for_user(user):
		return True
	if not role_registry.user_has("ops_view", user) or not doc.get("trip"):
		return False
	trip = frappe.db.get_value(
		"CH Logistics Trip", doc.get("trip"), ["hub_warehouse", "company"], as_dict=True
	) or {}
	return scope_guard.is_in_scope(
		warehouse=trip.get("hub_warehouse"), company=trip.get("company"), user=user
	)


def has_logistics_access() -> bool:
	"""True if the user has any logistics-related role (central registry)."""
	from ch_logistics import roles as role_registry
	return role_registry.user_has("app_access")
