"""CH Tracking Settings (Single) — Google Maps key + tracking cadence."""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class CHTrackingSettings(Document):
	def validate(self) -> None:
		# Floor cadence at 5s to avoid burning device battery / DB writes.
		if self.default_ping_interval_sec and int(self.default_ping_interval_sec) < 5:
			self.default_ping_interval_sec = 5
		if self.trip_ping_interval_sec and int(self.trip_ping_interval_sec) < 5:
			self.trip_ping_interval_sec = 5


def get_public_config() -> dict:
	"""Return tracking config safe to ship to browser (no API key)."""
	s = frappe.get_cached_doc("CH Tracking Settings")
	return {
		"tracking_enabled": bool(s.tracking_enabled),
		"default_ping_interval_sec": int(s.default_ping_interval_sec or 60),
		"trip_ping_interval_sec": int(s.trip_ping_interval_sec or 15),
		"geofence_arrival_meters": int(s.geofence_arrival_meters or 100),
		"default_map_zoom": int(s.default_map_zoom or 12),
		"default_map_center": {
			"lat": float(s.default_map_center_lat or 13.0827),
			"lng": float(s.default_map_center_lng or 80.2707),
		},
	}


def get_google_maps_api_key() -> str:
	"""Return decrypted API key. Use sparingly — never ship to clients raw."""
	try:
		return frappe.utils.password.get_decrypted_password(
			"CH Tracking Settings", "CH Tracking Settings",
			fieldname="google_maps_api_key",
		) or ""
	except Exception:
		return ""
