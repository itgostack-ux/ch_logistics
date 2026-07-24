"""Setup hooks — after_install / after_migrate."""

import frappe


def after_install():
	"""Run patches that install custom fields on upstream Driver."""
	from ch_logistics.patches.v0_0_1 import install_driver_geo_fields
	from ch_logistics.patches.v0_0_6 import add_arrival_location_fields
	install_driver_geo_fields.execute()
	add_arrival_location_fields.execute()


def after_migrate():
	"""Idempotent — re-run field installer to recover from manual drift."""
	# Re-run every custom-field installer (all idempotent) — recovers from
	# manual drift AND from prod-dump restores that lost Custom Fields while
	# the patch log still says "executed".
	from ch_logistics.patches.v0_0_1 import install_driver_geo_fields
	from ch_logistics.patches.v0_0_3 import (
		install_driver_app_fields,
		install_logistics_phase2_fields,
	)
	from ch_logistics.patches.v0_0_4 import install_geo_optimization_fields
	from ch_logistics.patches.v0_0_5 import extend_rejection_reasons, install_tracking_token
	from ch_logistics.patches.v0_0_6 import add_arrival_location_fields
	from ch_logistics.patches.v0_0_7 import install_store_geo_fields
	from ch_logistics.patches.v0_0_9 import install_driver_status_fields
	for installer in (
		install_driver_geo_fields,
		install_driver_app_fields,
		install_logistics_phase2_fields,
		install_geo_optimization_fields,
		install_tracking_token,
		extend_rejection_reasons,
		add_arrival_location_fields,
		install_store_geo_fields,
		install_driver_status_fields,
	):
		try:
			installer.execute()
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"ch_logistics.after_migrate {installer.__name__}",
			)
	try:
		_provision_access_control()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ch_logistics.after_migrate access_control")


def _provision_access_control():
	"""Ensure logistics roles exist + seed the editable role matrix once.

	Seeds CH Logistics Settings → Role Matrix from roles.DEFAULT_ROLE_MATRIX
	only for function keys that have NO rows yet, so admin edits/deletions
	are never overwritten on subsequent migrates.
	"""
	from ch_logistics.roles import DEFAULT_ROLE_MATRIX, ensure_roles

	if not frappe.db.exists("DocType", "CH Logistics Role Rule"):
		return
	ensure_roles()
	from ch_erp15.ch_erp15.default_permissions import seed_default_docperms
	seed_default_docperms({
		"Warehouse": {
			"Delivery Manager": {"read", "write"},
			"Operations Manager": {"read", "write"},
			"Logistics Head": {"read", "write"},
			"Logistic Head": {"read", "write"},
		},
	})

	settings = frappe.get_doc("CH Logistics Settings")
	seeded_keys = {row.function_key for row in (settings.get("role_matrix") or [])}
	changed = False
	for key, roles in DEFAULT_ROLE_MATRIX.items():
		if key in seeded_keys:
			continue
		for role in sorted(roles):
			if not frappe.db.exists("Role", role):
				continue  # legacy alias roles ("Logistic Head") are optional
			settings.append("role_matrix", {"function_key": key, "role": role})
			changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()
