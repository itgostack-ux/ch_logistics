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
