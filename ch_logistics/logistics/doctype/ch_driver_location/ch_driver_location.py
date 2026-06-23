"""CH Driver Location — append-only GPS ping log.

Each row represents one location report from a driver's device. The most
recent ping per driver is denormalised onto the upstream Driver record
(custom fields ``current_lat`` / ``current_lng`` / ``last_geo_at``) for cheap
live-map queries.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class CHDriverLocation(Document):
	def validate(self) -> None:
		# Coordinate sanity — anything outside Earth bounds is a bad fix.
		if not (-90.0 <= float(self.latitude or 0) <= 90.0):
			frappe.throw("Latitude must be between -90 and 90.")
		if not (-180.0 <= float(self.longitude or 0) <= 180.0):
			frappe.throw("Longitude must be between -180 and 180.")
		# Clamp obviously-bad accuracy (some devices report 99999).
		if self.accuracy_m and float(self.accuracy_m) > 10000:
			self.accuracy_m = 10000

	def after_insert(self) -> None:
		"""Denormalise latest position onto the Driver row."""
		try:
			from ch_logistics.api.tracking_api import _update_driver_current_position
			_update_driver_current_position(self)
		except Exception:
			# Never let denormalisation break ping ingestion.
			frappe.log_error(frappe.get_traceback(),
							 "CHDriverLocation.after_insert")
