"""Tracking API — driver position pings + live fleet queries.

Surfaces three things:
  * ``ping_location`` — driver app submits one GPS fix (writes a row to
	``CH Driver Location`` + denormalises onto Driver).
  * ``get_live_drivers`` — control tower fetches every currently-online
	driver's last known position for the live fleet map.
  * ``get_driver_trail`` — recent positions for a single driver (breadcrumb
	or playback).

Scheduler entry points:
  * ``purge_old_locations`` — daily cleanup of pings beyond the retention
	window (configurable in CH Tracking Settings).
  * ``mark_stale_drivers_offline`` — flips drivers to Offline if no ping
	within the configured stale window.
"""
from __future__ import annotations

import math
from typing import Iterable

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime

from ch_logistics.logistics.doctype.ch_tracking_settings.ch_tracking_settings import (
	get_public_config,
)


# ----- Driver resolution ---------------------------------------------------

def _current_driver(throw: bool = True) -> str | None:
	"""Resolve the logged-in user to a Driver record."""
	user = frappe.session.user
	driver = (
		frappe.db.get_value("Driver", {"user": user}, "name")
		or frappe.db.get_value("Driver", {"employee": user}, "name")
	)
	if not driver and throw:
		frappe.throw(_("Your user is not linked to any Driver record."),
					 title=_("Not a Driver"))
	return driver


def _driver_has_field(field: str) -> bool:
	try:
		return frappe.get_meta("Driver").has_field(field)
	except Exception:
		return False


def _update_driver_current_position(loc) -> None:
	"""Denormalise the latest fix onto the Driver record.

	Called from CH Driver Location.after_insert. Only updates fields that
	actually exist (the geo custom fields are installed by a patch).
	"""
	updates = {}
	if _driver_has_field("current_lat"):
		updates["current_lat"] = flt(loc.latitude)
	if _driver_has_field("current_lng"):
		updates["current_lng"] = flt(loc.longitude)
	if _driver_has_field("last_geo_at"):
		updates["last_geo_at"] = loc.captured_at or now_datetime()
	if _driver_has_field("current_speed_kmh"):
		updates["current_speed_kmh"] = flt(loc.speed_kmh)
	if _driver_has_field("current_heading"):
		updates["current_heading"] = flt(loc.heading)
	if updates:
		frappe.db.set_value("Driver", loc.driver, updates,
							update_modified=False)


# ----- Public API ----------------------------------------------------------

@frappe.whitelist()
def get_config() -> dict:
	"""Return tracking config + per-trip cadence hint."""
	cfg = get_public_config()
	# Hint at which cadence the driver app should use right now.
	driver = _current_driver(throw=False)
	if driver:
		status = frappe.db.get_value("Driver", driver, "availability_status")
		on_trip = status in ("Assigned", "In Transit")
		cfg["recommended_interval_sec"] = (
			cfg["trip_ping_interval_sec"] if on_trip
			else cfg["default_ping_interval_sec"]
		)
		cfg["on_trip"] = on_trip
	else:
		cfg["recommended_interval_sec"] = cfg["default_ping_interval_sec"]
		cfg["on_trip"] = False
	return cfg


@frappe.whitelist(methods=["POST"])
def ping_location(latitude, longitude, accuracy_m=None, speed_kmh=None,
				  heading=None, device_id=None, battery_pct=None,
				  event_type="Heartbeat", is_mock=0, source="App",
				  trip=None) -> dict:
	"""Record one GPS fix for the current driver.

	Returns the new ping name + denormalised driver position. Safe to call
	at high cadence; insertion is the only DB write.
	"""
	driver = _current_driver()

	# Optionally suppress writes when tracking is globally disabled.
	settings = frappe.get_cached_doc("CH Tracking Settings")
	if not cint(settings.tracking_enabled):
		return {"ok": True, "skipped": "tracking_disabled"}

	loc = frappe.new_doc("CH Driver Location")
	loc.driver = driver
	loc.captured_at = now_datetime()
	loc.latitude = flt(latitude)
	loc.longitude = flt(longitude)
	loc.accuracy_m = flt(accuracy_m) if accuracy_m is not None else None
	loc.speed_kmh = flt(speed_kmh) if speed_kmh is not None else None
	loc.heading = flt(heading) if heading is not None else None
	loc.event_type = event_type or "Heartbeat"
	loc.trip = trip or frappe.db.get_value("Driver", driver, "current_trip")
	loc.device_id = device_id
	loc.battery_pct = cint(battery_pct) if battery_pct is not None else None
	loc.is_mock = cint(is_mock)
	loc.source = source or "App"
	loc.insert(ignore_permissions=True)
	frappe.db.commit()

	# Best-effort mock-location alert.
	if cint(is_mock) and cint(settings.alert_on_mock_location):
		_alert("Mock Location Detected", driver,
			   f"Driver {driver} sent a mock-location ping.")

	# Best-effort speed alert.
	threshold = cint(settings.alert_on_speed_kmh_above)
	if threshold and speed_kmh is not None and flt(speed_kmh) > threshold:
		_alert("Speeding Alert", driver,
			   f"Driver {driver} reported {flt(speed_kmh):.1f} km/h")

	# Geofence arrival check (only when actively on a trip).
	if loc.trip and cint(settings.alert_on_geofence_arrival):
		_maybe_mark_stop_arrived(loc, cint(settings.geofence_arrival_meters))

	return {
		"ok": True,
		"name": loc.name,
		"captured_at": str(loc.captured_at),
	}


@frappe.whitelist()
def get_live_drivers(status: str | None = None) -> list[dict]:
	"""Return all on-shift drivers + their last known position.

	Used by the live fleet map page. Filtered down to drivers that are
	online (any status other than Offline) and have a recent ping.
	"""
	# Only return rows where geo fields exist; otherwise nothing to plot.
	if not (_driver_has_field("current_lat") and _driver_has_field("current_lng")):
		return []

	filters = {"current_lat": ["is", "set"], "current_lng": ["is", "set"]}
	if status:
		filters["availability_status"] = status
	else:
		# Default — exclude Offline.
		filters["availability_status"] = ["!=", "Offline"]

	rows = frappe.get_all(
		"Driver",
		fields=[
			"name", "full_name", "cell_number",
			"availability_status",
			"current_trip",
			"current_lat", "current_lng",
			"last_geo_at",
		] + (["current_speed_kmh"] if _driver_has_field("current_speed_kmh") else [])
		  + (["current_heading"] if _driver_has_field("current_heading") else []),
		filters=filters,
		limit_page_length=500,
	)
	# Coerce numeric fields for clean JSON.
	for r in rows:
		r["current_lat"] = flt(r.get("current_lat"))
		r["current_lng"] = flt(r.get("current_lng"))
		r["current_speed_kmh"] = flt(r.get("current_speed_kmh"))
		r["current_heading"] = flt(r.get("current_heading"))
	return rows


@frappe.whitelist()
def get_driver_trail(driver: str | None = None, minutes: int = 60,
					 limit: int = 500) -> list[dict]:
	"""Recent positions for a driver (default: self, last hour, ≤500 pts)."""
	driver = driver or _current_driver()
	since = add_to_date(now_datetime(), minutes=-cint(minutes))
	rows = frappe.get_all(
		"CH Driver Location",
		fields=["name", "captured_at", "latitude", "longitude",
				"speed_kmh", "heading", "event_type", "trip"],
		filters={"driver": driver, "captured_at": [">", since]},
		order_by="captured_at asc",
		limit_page_length=cint(limit),
	)
	for r in rows:
		r["latitude"] = flt(r.get("latitude"))
		r["longitude"] = flt(r.get("longitude"))
	return rows


@frappe.whitelist()
def get_driver_last_position(driver: str | None = None) -> dict | None:
	"""Last-known position for a driver (driver self-view + popup)."""
	driver = driver or _current_driver()
	if not (_driver_has_field("current_lat") and _driver_has_field("current_lng")):
		return None
	d = frappe.db.get_value(
		"Driver", driver,
		["full_name", "availability_status", "current_trip",
		 "current_lat", "current_lng", "last_geo_at"],
		as_dict=True,
	)
	if not d or d.get("current_lat") is None:
		return None
	return {
		"driver": driver,
		"full_name": d.get("full_name"),
		"status": d.get("availability_status"),
		"trip": d.get("current_trip"),
		"lat": flt(d.get("current_lat")),
		"lng": flt(d.get("current_lng")),
		"last_geo_at": str(d.get("last_geo_at") or ""),
	}


# ----- Scheduler entry points ---------------------------------------------

def purge_old_locations() -> int:
	"""Daily — delete CH Driver Location rows older than the retention window."""
	days = cint(
		frappe.db.get_single_value("CH Tracking Settings",
								   "location_retention_days") or 30
	)
	if days <= 0:
		return 0
	cutoff = add_to_date(now_datetime(), days=-days)
	deleted = frappe.db.sql(
		"DELETE FROM `tabCH Driver Location` WHERE captured_at < %s",
		(cutoff,),
	)
	frappe.db.commit()
	return cint(getattr(deleted, "rowcount", 0))


def mark_stale_drivers_offline() -> int:
	"""Every 5 minutes — flip drivers offline if no ping in N minutes."""
	if not _driver_has_field("last_geo_at"):
		return 0
	minutes = cint(
		frappe.db.get_single_value("CH Tracking Settings",
								   "stale_offline_minutes") or 10
	)
	if minutes <= 0:
		return 0
	cutoff = add_to_date(now_datetime(), minutes=-minutes)
	stale = frappe.get_all(
		"Driver",
		filters={
			"availability_status": ["in", ["Available", "Idle"]],
			"last_geo_at": ["<", cutoff],
		},
		pluck="name",
	)
	for d in stale:
		try:
			# Soft transition — never block via raise.
			from ch_logistics.api import driver_status as ds
			ds.set_status(d, ds.OFFLINE, force=True, touch_activity=False)
		except Exception:
			frappe.db.set_value("Driver", d, "availability_status", "Offline",
								update_modified=False)
	if stale:
		frappe.db.commit()
	return len(stale)


# ----- Helpers -------------------------------------------------------------

def _haversine_meters(lat1: float, lng1: float,
					  lat2: float, lng2: float) -> float:
	"""Great-circle distance in meters."""
	R = 6_371_000.0
	phi1, phi2 = math.radians(lat1), math.radians(lat2)
	dphi = math.radians(lat2 - lat1)
	dlmb = math.radians(lng2 - lng1)
	a = (math.sin(dphi / 2) ** 2
		 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2)
	return 2 * R * math.asin(math.sqrt(a))


def _maybe_mark_stop_arrived(loc, geofence_m: int) -> None:
	"""If the driver is within geofence of any pending stop, raise an event."""
	try:
		stops = frappe.get_all(
			"CH Logistics Trip Stop",
			fields=["name", "gps_lat", "gps_lng", "sequence", "status"],
			filters={"parent": loc.trip, "parenttype": "CH Logistics Trip"},
		)
	except Exception:
		return

	for s in stops:
		if (s.get("status") or "").lower() in ("arrived", "departed", "completed"):
			continue
		if not (s.get("gps_lat") and s.get("gps_lng")):
			continue
		dist = _haversine_meters(
			flt(loc.latitude), flt(loc.longitude),
			flt(s.gps_lat), flt(s.gps_lng),
		)
		if dist <= geofence_m:
			frappe.publish_realtime(
				event="ch_logistics:stop_geofence_arrival",
				message={
					"trip": loc.trip,
					"stop": s.name,
					"sequence": s.sequence,
					"driver": loc.driver,
					"distance_m": round(dist, 1),
				},
			)
			return  # only fire once per ping


def _alert(title: str, driver: str, message: str) -> None:
	"""Best-effort dispatcher alert via realtime + System Notification."""
	try:
		frappe.publish_realtime(
			event="ch_logistics:driver_alert",
			message={"title": title, "driver": driver, "message": message},
		)
	except Exception:
		pass
