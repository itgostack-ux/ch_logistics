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


def _split_row_into_new_manifest(manifest_doc, row):
	"""Pull ONE `transfers` row out of a submitted manifest into its own
	fresh, submitted, single-row manifest carrying the same driver/trip
	assignment — so the existing whole-manifest rejection lifecycle can run
	on it untouched, while the rest of `manifest_doc` stays Assigned with
	the driver. Mirrors the direct-write-on-a-submitted-doc pattern already
	used by `detach_manifest` (logistics_api.py) — the `transfers` table
	field itself isn't `allow_on_submit`, so removing a row has to go
	through SQL rather than `.save()`.

	The row is deleted from the OLD manifest FIRST, before the new manifest
	is even inserted: `_validate_transfers()` throws "already in active
	manifest" for any Stock Entry that still appears on a different
	docstatus=1 manifest, and the old manifest is still submitted — so
	building the new doc first would trip that guard on itself.
	"""
	stock_entry, from_wh, to_wh = row.stock_entry, row.from_warehouse, row.to_warehouse
	driver_accepted_at = row.driver_accepted_at

	frappe.db.sql("DELETE FROM `tabCH Transfer Manifest Item` WHERE name = %s", (row.name,))
	remaining = [r for r in manifest_doc.transfers if r.name != row.name]
	frappe.db.set_value(
		"CH Transfer Manifest", manifest_doc.name,
		{
			"total_stock_entries": len(remaining),
			"total_items": sum(cint(r.item_count) for r in remaining),
			"total_qty": sum(flt(r.total_qty) for r in remaining),
		},
		update_modified=False,
	)
	manifest_doc.add_comment(
		"Comment",
		_("Stock Entry {0} split off for individual rejection.").format(stock_entry),
	)

	new_doc = frappe.new_doc("CH Transfer Manifest")
	new_doc.manifest_date = frappe.utils.today()
	new_doc.company = manifest_doc.company
	new_doc.source_warehouse = from_wh
	new_doc.destination_warehouse = to_wh
	new_doc.driver = manifest_doc.driver
	new_doc.driver_name = manifest_doc.driver_name
	new_doc.driver_phone = manifest_doc.driver_phone
	new_doc.vehicle_number = manifest_doc.vehicle_number
	new_doc.trip = manifest_doc.trip
	new_doc.append("transfers", {"stock_entry": stock_entry})
	new_doc.flags.ignore_mandatory = True
	new_doc.insert(ignore_permissions=True)
	new_doc.status = "Assigned"
	new_doc.driver_accepted_at = driver_accepted_at or frappe.utils.now_datetime()
	new_doc.flags.ignore_mandatory = True
	new_doc.submit()

	manifest_doc.add_comment(
		"Comment",
		_("Stock Entry {0} split into manifest {1} for individual rejection.").format(stock_entry, new_doc.name),
	)
	return new_doc.name


def _maybe_complete_manifest_pickup(manifest_name: str) -> None:
	"""After a sibling leg is rejected/split off, check whether every
	remaining leg on `manifest_name` has already individually captured its
	own pickup evidence (accepted-and-picked-up via driver_accept_manifest_row
	in logistics_api.py). If so, roll the manifest up to In Transit exactly
	as that function does when the LAST leg is accepted.

	That rollup only runs at the moment a leg is accepted — removing the
	one row still holding it back (by splitting it off here for rejection)
	never re-triggers it on its own. Without this, "accept leg A, then
	reject sibling leg B" leaves the manifest stuck at Assigned forever,
	with leg A fully picked up but no action left to move it to delivery.

	No-op if the manifest isn't fully accepted yet, or is already rolled up.
	"""
	from ch_logistics.api.trip_lock import get_locked_trip
	from ch_logistics.api.logistics_api import _set_driver_availability

	lock_key = f"manifest_status_{frappe.scrub(manifest_name)}"
	if not frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]:
		frappe.throw(_("Manifest {0} is being updated by another user. Please refresh and try again.").format(manifest_name))
	try:
		mf = frappe.get_doc("CH Transfer Manifest", manifest_name)
		if mf.driver_accepted_at or mf.status != "Assigned":
			return
		if not mf.transfers or not all(r.driver_accepted_at for r in mf.transfers):
			return

		last_accepted = max(mf.transfers, key=lambda r: r.driver_accepted_at)
		mf.driver_accepted_at = last_accepted.driver_accepted_at

		if mf.trip:
			trip_doc = get_locked_trip(mf.trip)
			if trip_doc.status == "Assigned":
				trip_doc.add_comment(
					"Comment",
					_("Trip auto-started: first manifest ({0}) fully accepted by {1}.").format(
						mf.name, frappe.session.user
					),
				)
				trip_doc.mark_started()
				trip_doc.save()
				if trip_doc.driver:
					_set_driver_availability(trip_doc.driver, "In Transit", trip_doc.name)

		mf._apply_pickup_in_transit_transition(
			last_accepted.pickup_photo, last_accepted.pickup_lat, last_accepted.pickup_lng,
		)
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))


@frappe.whitelist(methods=["POST"])
def driver_reject_manifest_row(manifest: str, stock_entry: str, rejection_reason: str,
								proof_image_1: str, proof_image_2: str,
								remarks: str | None = None,
								latitude: float | None = None,
								longitude: float | None = None) -> dict:
	"""Driver rejects ONE Stock Entry leg inside a multi-entry manifest.

	If it's the only leg left on the manifest this degrades to exactly
	today's whole-manifest `reject_manifest` behavior (no split). Otherwise
	the leg is split into its own single-row manifest first, and the
	existing, unmodified rejection lifecycle (stock status sync, trip
	exception, driver-state recompute, dispatcher notification) runs on
	that new manifest — the rest of the original manifest's legs stay
	Assigned with the driver, untouched.
	"""
	frappe.db.sql(
		"SELECT name FROM `tabCH Transfer Manifest` WHERE name = %s FOR UPDATE",
		(manifest,),
	)
	manifest_doc = frappe.get_doc("CH Transfer Manifest", manifest)
	from ch_logistics.api.driver_resolver import assert_manifest_driver_access

	assert_manifest_driver_access(manifest_doc, scope_side="source")
	if manifest_doc.status not in ("Assigned", "Pickup Started", "In Transit"):
		frappe.throw(_("Only an active pickup or in-transit delivery can be rejected."))

	row = next((r for r in manifest_doc.transfers if r.stock_entry == stock_entry), None)
	if not row:
		frappe.throw(_("Stock Entry {0} is not on manifest {1}.").format(stock_entry, manifest))

	if len(manifest_doc.transfers) == 1:
		target_manifest = manifest
	else:
		target_manifest = _split_row_into_new_manifest(manifest_doc, row)
		# The sibling(s) left behind on `manifest` may have already been
		# individually accepted+picked-up before this rejection — removing
		# the rejected row can be the very thing that makes them "fully
		# accepted" now. Nothing else re-checks that on its own.
		_maybe_complete_manifest_pickup(manifest)

	doc, trip = create_and_submit_rejection(
		target_manifest, rejection_reason, proof_image_1, proof_image_2, remarks, latitude, longitude,
	)

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
		"manifest": target_manifest,
		"original_manifest": manifest,
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
