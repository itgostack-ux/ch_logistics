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
	from ch_logistics.patches.v0_0_1 import install_driver_geo_fields
	from ch_logistics.patches.v0_0_6 import add_arrival_location_fields
	try:
		install_driver_geo_fields.execute()
		add_arrival_location_fields.execute()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ch_logistics.after_migrate")
