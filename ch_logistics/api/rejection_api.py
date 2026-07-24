"""Manifest Rejection API — driver rejects with two proof photos."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime


def _current_driver() -> str | None:
	"""Resolve the logged-in user's Driver without creating an identity.

	Thin shim over :func:`ch_logistics.api.driver_resolver.resolve_current_driver`.
	"""
	from ch_logistics.api.driver_resolver import resolve_current_driver
	return resolve_current_driver(throw=False)


def create_and_submit_rejection(
	manifest: str,
	rejection_reason: str,
	proof_image_1: str,
	proof_image_2: str,
	remarks: str | None = None,
	latitude: float | None = None,
	longitude: float | None = None,
):
	"""Create the audit record whose submit invokes the canonical lifecycle."""
	if not manifest:
		frappe.throw(_("Manifest is required."))
	if not rejection_reason:
		frappe.throw(_("Rejection reason is required."))
	if not proof_image_1 or not proof_image_2:
		frappe.throw(_("Both proof photos are required (FR-024, FR-025)."))
	if proof_image_1 == proof_image_2:
		frappe.throw(_("The two proof photos must be different."))

	from ch_logistics import roles as role_registry
	from ch_logistics.api.driver_resolver import assert_manifest_driver_access

	role_registry.require("reject_manifest")
	frappe.db.sql(
		"SELECT name FROM `tabCH Transfer Manifest` WHERE name = %s FOR UPDATE",
		(manifest,),
	)
	manifest_doc = frappe.get_doc("CH Transfer Manifest", manifest)
	manifest_doc.check_permission("write")
	assert_manifest_driver_access(manifest_doc, scope_side="source")
	driver = _current_driver() or manifest_doc.driver
	if not driver:
		frappe.throw(_("The manifest has no assigned driver."), frappe.PermissionError)
	if manifest_doc.status not in ("Assigned", "Pickup Started", "In Transit"):
		frappe.throw(_("Only an active pickup or in-transit delivery can be rejected."))
	if frappe.db.exists(
		"CH Manifest Rejection",
		{"manifest": manifest, "docstatus": ["<", 2], "status": ["not in", ["Closed", "Reassigned"]]},
	):
		frappe.throw(_("An active rejection already exists for this manifest."))
	trip = manifest_doc.get("trip")

	doc = frappe.new_doc("CH Manifest Rejection")
	doc.manifest = manifest
	doc.rejection_reason = rejection_reason
	doc.proof_image_1 = proof_image_1
	doc.proof_image_2 = proof_image_2
	doc.remarks = str(remarks or "").strip()[:1000]
	doc.latitude = flt(latitude) if latitude is not None else None
	doc.longitude = flt(longitude) if longitude is not None else None
	doc.insert()
	doc.submit()
	return doc, trip


@frappe.whitelist(methods=["POST"])
def reject_manifest(manifest: str, rejection_reason: str,
					proof_image_1: str, proof_image_2: str,
					remarks: str | None = None,
					latitude: float | None = None,
					longitude: float | None = None) -> dict:
	"""Driver rejects an active manifest. Both proof photos are mandatory."""
	doc, trip = create_and_submit_rejection(
		manifest,
		rejection_reason,
		proof_image_1,
		proof_image_2,
		remarks,
		latitude,
		longitude,
	)

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
	from ch_logistics import roles as role_registry, scope_guard

	role_registry.require("ops_view")
	filters = {}
	if status:
		filters["status"] = status
	if driver:
		filters["driver"] = driver
	rows = frappe.get_list(
		"CH Manifest Rejection",
		fields=["name", "manifest", "trip", "driver", "rejection_reason",
				"status", "rejected_on", "rejected_by",
				"latitude", "longitude"],
		filters=filters,
		order_by="rejected_on desc",
		limit=min(
			max(cint(limit), 1),
			role_registry.get_int_setting("ops_record_row_limit", 500),
		),
	)
	manifest_names = {row.manifest for row in rows if row.manifest}
	manifest_scope = {
		row.name: row
		for row in frappe.get_all(
			"CH Transfer Manifest",
			filters={"name": ["in", list(manifest_names) or ["__none__"]]},
			fields=[
				"name", "source_store", "source_warehouse",
				"destination_store", "destination_warehouse", "company",
			],
		)
	}
	return [
		row for row in rows
		if row.manifest in manifest_scope
		and (
			scope_guard.is_in_scope(
				store=manifest_scope[row.manifest].source_store,
				warehouse=manifest_scope[row.manifest].source_warehouse,
				company=manifest_scope[row.manifest].company,
			)
			or scope_guard.is_in_scope(
				store=manifest_scope[row.manifest].destination_store,
				warehouse=manifest_scope[row.manifest].destination_warehouse,
				company=manifest_scope[row.manifest].company,
			)
		)
	]
