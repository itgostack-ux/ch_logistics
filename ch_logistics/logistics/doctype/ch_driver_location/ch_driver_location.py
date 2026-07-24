"""CH Driver Location — append-only GPS ping log.

Each row represents one location report from a driver's device. The most
recent ping per driver is denormalised onto the upstream Driver record
(custom fields ``current_lat`` / ``current_lng`` / ``last_geo_at``) for cheap
live-map queries.
"""
from __future__ import annotations

import math

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class CHDriverLocation(Document):
	def before_insert(self) -> None:
		from ch_logistics import roles as role_registry
		from ch_logistics.api.driver_resolver import resolve_current_driver

		if not role_registry.is_privileged():
			driver = resolve_current_driver(throw=True)
			if self.driver != driver:
				frappe.throw("Location pings can only be recorded for your Driver profile.", frappe.PermissionError)
			if self.trip and frappe.db.get_value("CH Logistics Trip", self.trip, "driver") != driver:
				frappe.throw("Location pings can only reference your assigned trip.", frappe.PermissionError)
		self.captured_at = now_datetime()

	def validate(self) -> None:
		latitude = float(self.latitude or 0)
		longitude = float(self.longitude or 0)
		if not math.isfinite(latitude) or not (-90.0 <= latitude <= 90.0):
			frappe.throw("Latitude must be between -90 and 90.")
		if not math.isfinite(longitude) or not (-180.0 <= longitude <= 180.0):
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
