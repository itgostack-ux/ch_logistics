from __future__ import annotations

import frappe
from frappe import _


def get_locked_trip(trip: str):
	if not isinstance(trip, str) or not trip.strip() or len(trip) > 140:
		frappe.throw(_("Invalid logistics trip."), frappe.ValidationError)
	trip = trip.strip()
	rows = frappe.db.sql(
		"SELECT name FROM `tabCH Logistics Trip` WHERE name = %s FOR UPDATE",
		(trip,),
	)
	if not rows:
		frappe.throw(_("Logistics Trip {0} was not found.").format(trip), frappe.DoesNotExistError)
	return frappe.get_doc("CH Logistics Trip", trip)


def lock_manifests(manifests) -> None:
	names = tuple(sorted({name for name in manifests if isinstance(name, str) and name}))
	if not names:
		return
	rows = frappe.db.sql(
		"""SELECT name FROM `tabCH Transfer Manifest`
		   WHERE name IN %(names)s ORDER BY name FOR UPDATE""",
		{"names": names},
	)
	locked = {row[0] for row in rows}
	missing = sorted(set(names) - locked)
	if missing:
		frappe.throw(
			_("Transfer Manifest {0} was not found.").format(missing[0]),
			frappe.DoesNotExistError,
		)


def set_manifest_trip(manifest: str, trip: str | None, extra: dict | None = None) -> None:
	"""Attach or detach a manifest from a trip, stamping WHEN it happened.

	The trip link used to be written straight through ``frappe.db.set_value``
	from four separate places. That bypasses the version trail, so nothing
	anywhere recorded the moment a consignment went onto a trip — the pipeline
	report had to infer it from the trip's own creation, which can be days out
	(one manifest raised 30 Jul was inferred onto a trip created 8 Aug).

	Every attach and detach now goes through here so the stamp cannot be
	forgotten by a future caller, and so attach-time extras (stop sequence and
	the like) land in the same write rather than a second one.
	"""
	if not isinstance(manifest, str) or not manifest.strip():
		frappe.throw(_("Invalid transfer manifest."), frappe.ValidationError)

	payload = dict(extra or {})
	payload["trip"] = trip or None

	if frappe.get_meta("CH Transfer Manifest").has_field("trip_attached_at"):
		# Detaching clears the stamp: the moment it went on a trip is no longer
		# true of a manifest that is off every trip, and leaving it would read
		# as still-attached on the report.
		payload["trip_attached_at"] = frappe.utils.now_datetime() if trip else None

	frappe.db.set_value("CH Transfer Manifest", manifest, payload)
