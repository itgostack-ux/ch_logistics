"""Manifest Rejection API — driver rejects with two proof photos."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


def _current_driver() -> str:
	user = frappe.session.user
	driver = (
		frappe.db.get_value("Driver", {"user": user}, "name")
		or frappe.db.get_value("Driver", {"employee": user}, "name")
	)
	if not driver:
		frappe.throw(_("Your user is not linked to any Driver record."))
	return driver


@frappe.whitelist(methods=["POST"])
def reject_manifest(manifest: str, rejection_reason: str,
					proof_image_1: str, proof_image_2: str,
					remarks: str | None = None,
					latitude: float | None = None,
					longitude: float | None = None) -> dict:
	"""Driver rejects a manifest pickup. Both photos mandatory."""
	if not manifest:
		frappe.throw(_("Manifest is required."))
	if not rejection_reason:
		frappe.throw(_("Rejection reason is required."))
	if not proof_image_1 or not proof_image_2:
		frappe.throw(_("Both proof photos are required (FR-024, FR-025)."))
	if proof_image_1 == proof_image_2:
		frappe.throw(_("The two proof photos must be different."))

	driver = _current_driver()
	trip = frappe.db.get_value("CH Transfer Manifest", manifest, "trip")

	doc = frappe.new_doc("CH Manifest Rejection")
	doc.manifest = manifest
	doc.trip = trip
	doc.driver = driver
	doc.rejection_reason = rejection_reason
	doc.proof_image_1 = proof_image_1
	doc.proof_image_2 = proof_image_2
	doc.remarks = remarks
	doc.latitude = flt(latitude) if latitude is not None else None
	doc.longitude = flt(longitude) if longitude is not None else None
	doc.rejected_on = now_datetime()
	doc.rejected_by = frappe.session.user
	doc.insert(ignore_permissions=True)
	doc.submit()
	frappe.db.commit()

	# Record a ping at the rejection point too, so the trail shows it.
	if latitude is not None and longitude is not None:
		try:
			from ch_logistics.api.tracking_api import ping_location
			ping_location(latitude=latitude, longitude=longitude,
						  event_type="Manual", source="App", trip=trip)
		except Exception:
			pass

	return {
		"ok": True,
		"rejection": doc.name,
		"manifest": manifest,
	}


@frappe.whitelist()
def list_rejections(status: str | None = None, driver: str | None = None,
					limit: int = 100) -> list[dict]:
	"""Dispatcher view — recent rejections, optionally filtered."""
	filters = {}
	if status:
		filters["status"] = status
	if driver:
		filters["driver"] = driver
	return frappe.get_all(
		"CH Manifest Rejection",
		fields=["name", "manifest", "trip", "driver", "rejection_reason",
				"status", "rejected_on", "rejected_by",
				"latitude", "longitude"],
		filters=filters,
		order_by="rejected_on desc",
		limit_page_length=int(limit),
	)
