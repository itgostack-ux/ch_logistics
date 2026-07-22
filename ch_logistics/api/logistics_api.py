"""Logistics API — whitelisted methods for the Trip lifecycle.

Reuse-first surface that wraps CH Logistics Trip + CH Transfer Manifest.
Frontend (delivery-app page, desk forms) calls into here.
"""
from __future__ import annotations

import math
import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime
import json

from ch_logistics.api import driver_status as ds
from ch_logistics import roles as role_registry
from ch_logistics import scope_guard


_TRIP_CLOSE_TERMINAL_MANIFEST_STATUSES = {"Closed", "Delivered", "Cancelled"}

# Manifest statuses considered "attachable to a trip" in the Logistics
# Control Tower Operations tab.  Mirrors how TMS dispatcher consoles
# (Oracle WMS, SAP TM, Manhattan WMS) only surface freight that is
# packed/ready-to-load — anything already in motion or terminal is hidden.
# Draft is deliberately excluded: a manifest must be submitted (Packed)
# before it can ride a trip.
_OPS_ATTACHABLE_MANIFEST_STATUSES = ("Packed",)

# Statuses a manifest may hold at the moment it is attached to a trip.
# Rejected is allowed so dispatch can re-plan a bounced shipment onto a
# fresh trip; anything already moving or terminal is refused.
_ATTACHABLE_MANIFEST_STATUSES = {"Packed", "Assigned", "Rejected"}


# ---------------------------------------------------------------------------
# Trip lifecycle
# ---------------------------------------------------------------------------
def _has_manifest_trip_field() -> bool:
    return frappe.db.has_column("CH Transfer Manifest", "trip")


def _has_manifest_direction_field() -> bool:
    return frappe.db.has_column("CH Transfer Manifest", "direction")


def _has_manifest_stop_seq_field() -> bool:
    return frappe.db.has_column("CH Transfer Manifest", "stop_sequence")


def _has_manifest_shipment_priority_field() -> bool:
    return frappe.db.has_column("CH Transfer Manifest", "shipment_priority")


def _has_manifest_box_count_field() -> bool:
    return frappe.db.has_column("CH Transfer Manifest", "box_count")


@frappe.whitelist()
def trip_create(trip_date, company, route=None, driver=None, vehicle=None,
                planned_start=None, planned_end=None, direction="Forward",
                manifests=None):
    """Create a CH Logistics Trip, optionally pre-populating stops from route
    and attaching a list of CH Transfer Manifest names.

    When manifests are given but no route, the trip's hub + stops are seeded
    from the manifests themselves so the trip↔manifest warehouse-match
    validation in _attach_manifests holds by construction."""
    scope_guard.assert_scope(company=company)
    doc = frappe.new_doc("CH Logistics Trip")
    doc.trip_date = trip_date
    doc.company = company
    doc.direction = direction or "Forward"
    if route:
        doc.route = route
    if driver:
        _ensure_single_active_trip_for_driver(driver)
        doc.driver = driver
        doc.status = "Assigned"
    if vehicle:
        doc.vehicle = vehicle
    if planned_start:
        doc.planned_start = planned_start
    if planned_end:
        doc.planned_end = planned_end
    if route:
        doc.populate_stops_from_route()
    doc.insert()

    if manifests:
        if isinstance(manifests, str):
            manifests = frappe.parse_json(manifests)
        if not (doc.get("stops") or []):
            _seed_stops_from_manifests(doc, manifests)
        _attach_manifests(doc.name, manifests)

    return doc.name


def _seed_stops_from_manifests(trip_doc, manifests) -> None:
    """Build the trip's hub + stops from its initial manifests.

    Forward manifests: hub = common source, one Drop stop per destination.
    Reverse manifests: one Pickup stop per source, hub = common destination.
    Mirrors club_transfers_into_trip so the Control Tower "Create & Attach"
    dialog (no route picked) keeps working under the warehouse-match gate.
    """
    if not manifests:
        return
    seq = 0
    seen_targets = set()
    hub_candidates = []
    for manifest_name in manifests:
        target = _manifest_target_for_trip(trip_doc, manifest_name)
        mf = frappe.db.get_value(
            "CH Transfer Manifest", manifest_name,
            ["source_store", "source_warehouse", "destination_store", "destination_warehouse"],
            as_dict=True,
        ) or {}
        # The side NOT served by a stop is the hub side.
        if target.get("stop_type") == "Pickup":
            hub_candidates.append(mf.get("destination_warehouse"))
        else:
            hub_candidates.append(mf.get("source_warehouse"))

        target_store = target.get("store")
        target_wh = target.get("warehouse")
        if not target_wh and target_store:
            target_wh = frappe.db.get_value("CH Store", target_store, "warehouse")
        key = target_store or target_wh
        if not key or key in seen_targets:
            continue
        seen_targets.add(key)
        seq += 1
        trip_doc.append("stops", {
            "sequence": seq,
            "warehouse": target_wh,
            "store": target_store,
            "stop_type": target.get("stop_type") or "Drop",
            "status": "Pending",
        })

    if not trip_doc.get("hub_warehouse"):
        hub = next((h for h in hub_candidates if h), None)
        if hub:
            trip_doc.hub_warehouse = hub
    if seq:
        trip_doc.flags.ignore_mandatory = True
        trip_doc.save(ignore_permissions=True)


@frappe.whitelist()
def trip_assign_driver(trip, driver, vehicle=None):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status not in ("Draft", "Assigned"):
        frappe.throw(_("Can only assign driver while trip is Draft or Assigned"))
    _ensure_single_active_trip_for_driver(driver, target_trip=doc.name)
    doc.driver = driver
    if vehicle:
        doc.vehicle = vehicle
    if doc.status == "Draft":
        doc.status = "Assigned"
    doc.save()
    # Fan the trip's driver assignment out to every manifest already attached
    # to the trip. Without this the driver app's per-manifest filter
    # (driver = X) hides them, even though they ride the same vehicle.
    propagated = _propagate_trip_driver_to_manifests(trip, driver, vehicle)
    _set_driver_availability(driver, "On Trip", doc.name)
    # FR-011: notify the driver of the new assignment.
    try:
        from ch_logistics.api.driver_push import notify_driver
        notify_driver(
            driver,
            _("New Trip Assigned"),
            _("Trip {0} ({1} shipment(s)) has been assigned to you.").format(
                doc.name, cint(doc.total_shipments)),
            data={"type": "trip_assigned", "trip": doc.name},
            reference=("CH Logistics Trip", doc.name),
        )
    except Exception:
        frappe.log_error(title="trip_assign_driver notify failed",
                         message=frappe.get_traceback())
    return {"trip": doc.name, "manifests_assigned": propagated}


def _propagate_trip_driver_to_manifests(trip, driver, vehicle=None):
    """Copy the trip's driver onto every manifest attached to it.

    Only touches manifests that are still pre-pickup (Draft / Packed /
    Assigned) so a driver change cannot retroactively rewrite history on
    rows already In Transit / Delivered / Closed.

    Returns the list of manifest names whose driver was updated.
    """
    if not _has_manifest_trip_field():
        return []
    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters={"trip": trip, "status": ["in", ["Draft", "Packed", "Assigned"]]},
        fields=["name", "docstatus", "status", "driver"],
    )
    # Driver doctype stores name as `full_name` (Frappe native field).
    drv_name = frappe.db.get_value("Driver", driver, "full_name") if driver else None
    drv_phone = frappe.db.get_value("Driver", driver, "cell_number") if driver else None
    updated = []
    for r in rows:
        # Skip rows already driven by someone else — a manager has to detach
        # them first; we never silently steal a manifest from another driver.
        if r.get("driver") and r.get("driver") != driver:
            continue
        payload = {"driver": driver}
        if drv_name:
            payload["driver_name"] = drv_name
        if drv_phone:
            payload["driver_phone"] = drv_phone
        if vehicle and frappe.get_meta("CH Transfer Manifest").has_field("vehicle"):
            payload["vehicle"] = vehicle
        # If the manifest is still Packed and the trip-level assignment is
        # what's putting a driver on it, also flip it to Assigned so the
        # driver app shows it under "To Pick Up".
        if r.get("status") == "Packed":
            payload["status"] = "Assigned"
        frappe.db.set_value("CH Transfer Manifest", r.name, payload, update_modified=False)
        updated.append(r.name)
    if updated:
        frappe.db.commit()
    return updated


def _clear_trip_driver_from_manifests(trip, driver=None):
    """Detach driver attribution from pre-pickup manifests on a trip.

    Used when a trip assignment is rejected/unassigned before execution.
    """
    if not _has_manifest_trip_field():
        return []
    filters = {
        "trip": trip,
        "status": ["in", ["Draft", "Packed", "Assigned", "Pickup Started"]],
    }
    if driver:
        filters["driver"] = driver
    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters=filters,
        fields=["name", "status"],
    )
    updated = []
    for r in rows:
        payload = {
            "driver": None,
            "driver_name": None,
            "driver_phone": None,
        }
        # Keep manifest discoverable to dispatch as pre-pickup work.
        if r.get("status") in ("Assigned", "Pickup Started"):
            payload["status"] = "Packed"
        frappe.db.set_value("CH Transfer Manifest", r.name, payload, update_modified=False)
        updated.append(r.name)
    if updated:
        frappe.db.commit()
    return updated


def _ensure_single_active_trip_for_driver(driver: str, target_trip: str | None = None):
    """Enforce one active trip per driver at any point in time.

    "Active" = Assigned or Started (trip is currently blocking the driver's
    time).  ``Completed`` is a post-run settlement state — driver has already
    finished the physical trip and is free to be dispatched again while the
    ops team closes out documentation.  This matches SAP TM Driver Roster
    and Dynamics 365 Fleet's "planned + in-execution only" rule.
    """
    if not driver:
        return
    rows = frappe.get_all(
        "CH Logistics Trip",
        filters={
            "driver": driver,
            "status": ["in", ["Assigned", "Started"]],
        },
        fields=["name", "status"],
        order_by="modified desc",
        limit=5,
    )
    blocking = [
        f"{r.name} ({r.status})"
        for r in rows
        if not target_trip or r.name != target_trip
    ]
    if blocking:
        frappe.throw(
            _("Driver already has an active trip: {0}. Close it before assigning a new one.").format(
                ", ".join(blocking)
            )
        )


@frappe.whitelist()
def trip_unassign(trip):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status not in ("Assigned",):
        frappe.throw(_("Can only unassign while trip is Assigned"))
    prev_driver = doc.driver
    _clear_trip_driver_from_manifests(doc.name, driver=prev_driver)
    doc.driver = None
    doc.vehicle = None
    doc.status = "Draft"
    doc.save()
    if prev_driver:
        _set_driver_availability(prev_driver, "Available", None)
    return doc.name


@frappe.whitelist()
def driver_accept_trip(trip):
    """Driver acceptance of an assigned trip.

    Keep UX explicit: driver acknowledges assignment, then trip moves to Started.
    """
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status != "Assigned":
        frappe.throw(_("Trip must be Assigned before accepting."))
    current_driver = _resolve_current_driver()
    if not current_driver or doc.driver != current_driver:
        frappe.throw(_("You can only accept a trip assigned to your driver profile."))

    doc.add_comment("Comment", _("Trip accepted by driver {0}.").format(current_driver))
    doc.mark_started()
    doc.save()
    _set_driver_availability(current_driver, "In Transit", doc.name)
    return {"trip": doc.name, "status": doc.status, "started_at": doc.actual_start}


@frappe.whitelist()
def driver_reject_trip(trip, reason, notes=None):
    """Driver rejection of an assigned trip with mandatory reason."""
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Reason is mandatory to reject a trip."))

    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status != "Assigned":
        frappe.throw(_("Trip can be rejected only while Assigned."))
    current_driver = _resolve_current_driver()
    if not current_driver or doc.driver != current_driver:
        frappe.throw(_("You can only reject a trip assigned to your driver profile."))

    _clear_trip_driver_from_manifests(doc.name, driver=current_driver)
    doc.add_comment(
        "Comment",
        _("Trip rejected by driver {0}. Reason: {1}{2}").format(
            current_driver,
            reason,
            (f" | Notes: {notes}" if notes else ""),
        ),
    )
    doc.driver = None
    doc.vehicle = None
    doc.status = "Draft"
    doc.save(ignore_permissions=True)
    _set_driver_availability(current_driver, "Available", None)
    return {"trip": doc.name, "status": doc.status}


@frappe.whitelist()
def trip_start(trip, gps_lat=None, gps_lng=None):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    doc.mark_started()
    doc.save()
    if doc.driver:
        # Driver goes In Transit for the duration of the run (FR-012 → In Transit).
        _set_driver_availability(doc.driver, "In Transit", doc.name)
    return {"trip": doc.name, "started_at": doc.actual_start}


@frappe.whitelist()
def trip_complete(trip, override_exceptions=0):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    _assert_exception_gate(doc, action="complete trip", override_exceptions=override_exceptions)
    doc.mark_completed()
    doc.save()
    if doc.driver:
        _set_driver_availability(doc.driver, "Available", None)
    return {"trip": doc.name, "ended_at": doc.actual_end}


@frappe.whitelist()
def trip_close(trip, close_as_head=0):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    allow_head_override = cint(close_as_head) and role_registry.user_has("head_override")

    if allow_head_override:
        _close_trip_as_logistics_head(doc)
        return doc.name

    doc.mark_closed()
    doc.save()
    return doc.name


def _blocking_manifests_for_trip_close(trip_name: str) -> list[str]:
    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters={"trip": trip_name, "docstatus": ["<", 2]},
        fields=["name", "status"],
    )
    return [
        f"{r.name} ({r.status or 'Draft'})"
        for r in rows
        if (r.status or "Draft") not in _TRIP_CLOSE_TERMINAL_MANIFEST_STATUSES
    ]


def _close_trip_as_logistics_head(doc):
    """Logistics Head close path.

    Allows closing from Started or Completed only when every attached
    manifest is terminal (Closed / Delivered / Cancelled).
    """
    if doc.status in ("Closed", "Cancelled"):
        return

    if doc.status not in ("Started", "Completed"):
        frappe.throw(
            _("Logistics Head close is allowed only from Started or Completed (current: {0}).").format(doc.status)
        )

    blocking = _blocking_manifests_for_trip_close(doc.name)
    if blocking:
        frappe.throw(
            _("Cannot close trip {0}. Non-terminal manifests: {1}").format(doc.name, ", ".join(blocking))
        )

    if doc.status == "Started":
        doc.mark_completed()
        doc.save(ignore_permissions=True)
        if doc.driver:
            _set_driver_availability(doc.driver, "Available", None)

    doc.mark_closed()
    doc.save(ignore_permissions=True)


@frappe.whitelist()
def trip_cancel(trip):
    """Cancel a trip from Draft or Assigned state."""
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status not in ("Draft", "Assigned"):
        frappe.throw(_("Trip can only be cancelled from Draft or Assigned state"))
    prev_driver = doc.driver
    doc.status = "Cancelled"
    doc.save()
    if prev_driver:
        _set_driver_availability(prev_driver, "Available", None)
    return doc.name


# ---------------------------------------------------------------------------
# Manifest attach / detach
# ---------------------------------------------------------------------------
@frappe.whitelist()
def attach_manifests(trip, manifests):
    """Attach a list of CH Transfer Manifest names to a trip."""
    _attach_manifests(trip, manifests)
    return True


def _manifest_target_for_trip(trip_doc, manifest_name: str) -> dict:
    mf_fields = ["source_store", "source_warehouse", "destination_store", "destination_warehouse"]
    if _has_manifest_direction_field():
        mf_fields.insert(0, "direction")
    mf = frappe.db.get_value("CH Transfer Manifest", manifest_name, mf_fields, as_dict=True) or {}
    is_reverse = (
        (mf.get("direction") == "Reverse")
        if _has_manifest_direction_field()
        else (trip_doc.direction == "Reverse")
    )
    target_store = mf.get("source_store") if is_reverse else mf.get("destination_store")
    target_wh = mf.get("source_warehouse") if is_reverse else mf.get("destination_warehouse")
    stop_type = "Pickup" if is_reverse else "Drop"
    return {
        "store": target_store,
        "warehouse": target_wh,
        "stop_type": stop_type,
    }


def _nearest_insertion_sequence(trip_doc, target_coord) -> int:
    stops = sorted(trip_doc.get("stops") or [], key=lambda r: cint(r.get("sequence") or 0))
    if not stops:
        return 1

    movable = [s for s in stops if not _stop_has_been_reached(s.get("status"))]
    if not movable:
        return max(cint(s.get("sequence") or 0) for s in stops) + 1

    min_seq = min(cint(s.get("sequence") or 0) for s in movable)
    if not target_coord:
        return min_seq

    best_seq = None
    best_cost = None
    prev_coord = _trip_live_origin(trip_doc) or _stop_coords(movable[0])
    for idx in range(len(movable) + 1):
        next_coord = _stop_coords(movable[idx]) if idx < len(movable) else None
        if prev_coord and next_coord:
            cost = (
                _haversine_km(prev_coord[0], prev_coord[1], target_coord[0], target_coord[1])
                + _haversine_km(target_coord[0], target_coord[1], next_coord[0], next_coord[1])
                - _haversine_km(prev_coord[0], prev_coord[1], next_coord[0], next_coord[1])
            )
        elif prev_coord:
            cost = _haversine_km(prev_coord[0], prev_coord[1], target_coord[0], target_coord[1])
        elif next_coord:
            cost = _haversine_km(target_coord[0], target_coord[1], next_coord[0], next_coord[1])
        else:
            cost = 0

        candidate_seq = (
            cint(movable[idx].get("sequence") or min_seq)
            if idx < len(movable)
            else max(cint(s.get("sequence") or 0) for s in stops) + 1
        )
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_seq = candidate_seq

        if idx < len(movable):
            prev_coord = _stop_coords(movable[idx]) or prev_coord

    return best_seq or min_seq


def _shift_sequences_for_insert(trip_doc, insert_seq: int) -> None:
    for s in sorted(
        trip_doc.get("stops") or [],
        key=lambda r: cint(r.get("sequence") or 0),
        reverse=True,
    ):
        if cint(s.get("sequence") or 0) >= cint(insert_seq):
            s.sequence = cint(s.get("sequence") or 0) + 1


def _ensure_dynamic_stop_for_manifest(trip_doc, manifest_name: str):
    target = _manifest_target_for_trip(trip_doc, manifest_name)
    target_store = target.get("store")
    target_wh = target.get("warehouse")
    if not target_wh and target_store:
        target_wh = frappe.db.get_value("CH Store", target_store, "warehouse")
    if not target_wh:
        return None

    for s in sorted(trip_doc.get("stops") or [], key=lambda r: cint(r.get("sequence") or 0)):
        if _is_stop_terminal(s.get("status")):
            continue
        if target_store and s.get("store") == target_store:
            return cint(s.get("sequence") or 0) or None
        if s.get("warehouse") == target_wh:
            return cint(s.get("sequence") or 0) or None

    insert_seq = _nearest_insertion_sequence(
        trip_doc,
        _store_coords(target_store) or _warehouse_coords(target_wh),
    )
    _shift_sequences_for_insert(trip_doc, insert_seq)
    trip_doc.append(
        "stops",
        {
            "sequence": insert_seq,
            "warehouse": target_wh,
            "store": target_store,
            "stop_type": target.get("stop_type") or ("Pickup" if trip_doc.direction == "Reverse" else "Drop"),
            "status": "Pending",
            "notes": _("Inserted on-route for manifest {0}").format(manifest_name),
        },
    )
    _trip_audit(
        trip_doc,
        _(
            "Dynamic stop inserted at sequence #{0} for manifest {1} (warehouse: {2}, store: {3})."
        ).format(
            insert_seq,
            manifest_name,
            target_wh,
            target_store or "-",
        ),
    )
    return insert_seq


def _trip_serviced_locations(trip_doc) -> tuple[set, set]:
    """(warehouses, stores) this trip touches: hub + every stop, with each
    stop's CH Store resolved to its warehouse as well."""
    warehouses, stores = set(), set()
    if trip_doc.get("hub_warehouse"):
        warehouses.add(trip_doc.hub_warehouse)
    for s in trip_doc.get("stops") or []:
        if s.get("warehouse"):
            warehouses.add(s.warehouse)
        if s.get("store"):
            stores.add(s.store)
            store_wh = frappe.db.get_value("CH Store", s.store, "warehouse")
            if store_wh:
                warehouses.add(store_wh)
    return warehouses, stores


def _validate_manifest_attachable(trip_doc, manifest_name: str, mf: dict) -> None:
    """Gate a manifest → trip attachment.

    1. Status gate: Draft manifests (not yet submitted/packed) and manifests
       in motion / terminal states are refused.
    2. Warehouse-match gate: BOTH the manifest's source and destination must
       be locations the trip actually serves (hub warehouse or a stop).
       Exception: on a Started trip the destination may be new — the ops-gated
       dynamic-stop insertion creates the stop for it (on-route add).
    """
    status = (mf.get("status") or "Draft").strip() or "Draft"
    if status == "Draft":
        frappe.throw(
            _("Manifest {0} is still in <b>Draft</b>. Submit it (status Packed) "
              "before assigning it to a trip.").format(manifest_name),
            title=_("Manifest Not Ready"),
        )
    if status not in _ATTACHABLE_MANIFEST_STATUSES:
        frappe.throw(
            _("Manifest {0} has status <b>{1}</b> and cannot be attached to a trip. "
              "Attachable statuses: {2}.").format(
                manifest_name, status, ", ".join(sorted(_ATTACHABLE_MANIFEST_STATUSES))),
            title=_("Manifest Not Attachable"),
        )

    warehouses, stores = _trip_serviced_locations(trip_doc)
    if not warehouses and not stores:
        frappe.throw(
            _("Trip {0} has no hub warehouse or stops yet. Pick a route or add "
              "stops before attaching manifests, so the manifest's source and "
              "destination can be validated against the trip.").format(trip_doc.name),
            title=_("Trip Has No Route"),
        )

    def _served(warehouse, store) -> bool:
        if warehouse and warehouse in warehouses:
            return True
        if store:
            if store in stores:
                return True
            store_wh = frappe.db.get_value("CH Store", store, "warehouse")
            if store_wh and store_wh in warehouses:
                return True
        return False

    if not _served(mf.get("source_warehouse"), mf.get("source_store")):
        frappe.throw(
            _("Manifest {0} source ({1}) does not match this trip — the trip's "
              "hub/stops do not include it. Attach the manifest to a trip that "
              "serves its source warehouse, or correct the trip's route/stops.").format(
                manifest_name,
                mf.get("source_store") or mf.get("source_warehouse") or "—"),
            title=_("Source Warehouse Mismatch"),
        )
    if not _served(mf.get("destination_warehouse"), mf.get("destination_store")):
        if trip_doc.status == "Started":
            return  # ops-gated on-route add: dynamic stop is inserted below
        frappe.throw(
            _("Manifest {0} destination ({1}) does not match this trip — no "
              "stop serves it. Add a stop for the destination or attach the "
              "manifest to the correct trip.").format(
                manifest_name,
                mf.get("destination_store") or mf.get("destination_warehouse") or "—"),
            title=_("Destination Warehouse Mismatch"),
        )


def _attach_manifests(trip, manifests):
    if not _has_manifest_trip_field():
        frappe.throw(
            _("CH Transfer Manifest is missing the 'trip' field. Please run bench migrate."),
            title=_("Schema Mismatch"),
        )

    if isinstance(manifests, str):
        manifests = frappe.parse_json(manifests)
    if not manifests:
        return
    trip_doc = frappe.get_doc("CH Logistics Trip", trip)
    # Internal recompute saves below must not re-litigate desk-form
    # mandatories (driver/vehicle/planned times) on a system-planned Draft
    # trip that has no carrier bound yet.
    trip_doc.flags.ignore_mandatory = True
    if trip_doc.status in ("Closed", "Cancelled"):
        frappe.throw(_("Cannot attach manifests to a {0} trip").format(trip_doc.status))
    if trip_doc.status == "Started":
        _require_ops()

    dynamically_inserted = []
    for manifest_name in manifests:
        mf = frappe.db.get_value(
            "CH Transfer Manifest",
            manifest_name,
            ["name", "status", "trip", "company", "source_store", "source_warehouse",
             "destination_store", "destination_warehouse"],
            as_dict=True,
        )
        if not mf:
            frappe.throw(_("Manifest {0} not found").format(manifest_name))
        current_trip = mf.get("trip")
        if current_trip and current_trip != trip:
            frappe.throw(
                _("Manifest {0} is already attached to trip {1}").format(manifest_name, current_trip)
            )
        scope_guard.assert_manifest_scope(mf)
        _validate_manifest_attachable(trip_doc, manifest_name, mf)
        frappe.db.set_value("CH Transfer Manifest", manifest_name, "trip", trip)
        seq = _assign_stop_sequence(trip_doc, manifest_name)
        if seq is None and trip_doc.status == "Started":
            seq = _ensure_dynamic_stop_for_manifest(trip_doc, manifest_name)
            if seq is not None and _has_manifest_stop_seq_field():
                frappe.db.set_value("CH Transfer Manifest", manifest_name, "stop_sequence", seq)
                dynamically_inserted.append(f"{manifest_name}->#{seq}")

    if dynamically_inserted:
        trip_doc.stops.sort(key=lambda r: cint(r.get("sequence") or 0))
        _trip_audit(
            trip_doc,
            _("On-route manifests inserted with dynamic stops: {0}").format(
                ", ".join(dynamically_inserted)
            ),
        )
        trip_doc.save(ignore_permissions=True)
    # Refresh totals + per-stop manifest counts
    trip_doc.reload()
    trip_doc.flags.ignore_mandatory = True
    trip_doc.save()
    # If the trip already has a driver, pull every just-attached pre-pickup
    # manifest onto that driver as well. Without this, attach-after-assign
    # leaves the new manifests invisible to the driver app.
    if trip_doc.driver:
        _propagate_trip_driver_to_manifests(trip, trip_doc.driver, trip_doc.get("vehicle"))


def _assign_stop_sequence(trip_doc, manifest_name):
    """Best-effort: map a manifest to the trip stop that serves its delivery
    (forward) or pickup (reverse) location, and store stop_sequence on it so it
    surfaces under the correct driver-app stop card."""
    if not frappe.get_meta("CH Transfer Manifest").has_field("stop_sequence"):
        return None
    if not trip_doc.stops:
        return None
    target = _manifest_target_for_trip(trip_doc, manifest_name)
    target_store = target.get("store")
    target_wh = target.get("warehouse")
    seq = None
    # Prefer a store match, then fall back to a warehouse match.
    for s in trip_doc.stops:
        if target_store and s.store == target_store:
            seq = s.sequence
            break
    if seq is None:
        for s in trip_doc.stops:
            if target_wh and s.warehouse == target_wh:
                seq = s.sequence
                break
    if seq is not None:
        frappe.db.set_value("CH Transfer Manifest", manifest_name, "stop_sequence", seq)
        return seq
    return None


@frappe.whitelist()
def detach_manifest(manifest):
    if not _has_manifest_trip_field():
        frappe.throw(
            _("CH Transfer Manifest is missing the 'trip' field. Please run bench migrate."),
            title=_("Schema Mismatch"),
        )

    current_trip = frappe.db.get_value("CH Transfer Manifest", manifest, "trip")
    if not current_trip:
        return
    scope_guard.assert_manifest_scope(manifest)
    trip_doc = frappe.get_doc("CH Logistics Trip", current_trip)
    trip_doc.flags.ignore_mandatory = True
    if trip_doc.status == "Started":
        _require_ops()
    if trip_doc.status in ("Completed", "Closed", "Cancelled"):
        frappe.throw(_("Cannot detach manifest from a {0} trip").format(trip_doc.status))
    frappe.db.set_value("CH Transfer Manifest", manifest, "trip", None)
    if _has_manifest_stop_seq_field():
        frappe.db.set_value("CH Transfer Manifest", manifest, "stop_sequence", None)
    _trip_audit(trip_doc, _("Manifest {0} detached by {1}.").format(manifest, frappe.session.user))
    trip_doc.reload()
    trip_doc.flags.ignore_mandatory = True
    trip_doc.save()
    return True


@frappe.whitelist()
def dynamic_insert_stop(trip, manifest=None, store=None, warehouse=None, stop_type=None, notes=None):
    """Insert a stop into an Assigned/Started trip at nearest feasible position."""
    _require_ops()
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status not in ("Assigned", "Started"):
        frappe.throw(_("Dynamic stop insertion is allowed only for Assigned/Started trips."))

    target_store = store
    target_wh = warehouse
    resolved_stop_type = stop_type or ("Pickup" if doc.direction == "Reverse" else "Drop")

    if manifest:
        target = _manifest_target_for_trip(doc, manifest)
        target_store = target.get("store") or target_store
        target_wh = target.get("warehouse") or target_wh
        resolved_stop_type = target.get("stop_type") or resolved_stop_type

    if not target_wh and target_store:
        target_wh = frappe.db.get_value("CH Store", target_store, "warehouse")
    if not target_wh:
        frappe.throw(_("Warehouse is required to insert a stop."))

    for s in sorted(doc.get("stops") or [], key=lambda r: cint(r.get("sequence") or 0)):
        if _is_stop_terminal(s.get("status")):
            continue
        if target_store and s.get("store") == target_store:
            return {"trip": doc.name, "inserted": False, "sequence": s.sequence, "reason": "existing-store-stop"}
        if s.get("warehouse") == target_wh:
            return {"trip": doc.name, "inserted": False, "sequence": s.sequence, "reason": "existing-warehouse-stop"}

    insert_seq = _nearest_insertion_sequence(doc, _store_coords(target_store) or _warehouse_coords(target_wh))
    _shift_sequences_for_insert(doc, insert_seq)
    row = doc.append(
        "stops",
        {
            "sequence": insert_seq,
            "warehouse": target_wh,
            "store": target_store,
            "stop_type": resolved_stop_type,
            "status": "Pending",
            "notes": notes or _("Inserted on-route by {0}").format(frappe.session.user),
        },
    )
    doc.stops.sort(key=lambda r: cint(r.get("sequence") or 0))
    _trip_audit(
        doc,
        _(
            "On-route stop inserted by {0}: #{1}, warehouse={2}, store={3}, stop_type={4}."
        ).format(
            frappe.session.user,
            insert_seq,
            target_wh,
            target_store or "-",
            resolved_stop_type,
        ),
    )
    doc.save(ignore_permissions=True)

    if manifest and _has_manifest_trip_field():
        if frappe.db.get_value("CH Transfer Manifest", manifest, "trip") != doc.name:
            frappe.db.set_value("CH Transfer Manifest", manifest, "trip", doc.name)
        if _has_manifest_stop_seq_field():
            frappe.db.set_value("CH Transfer Manifest", manifest, "stop_sequence", insert_seq)

    return {
        "trip": doc.name,
        "inserted": True,
        "stop_name": row.name,
        "sequence": insert_seq,
        "warehouse": target_wh,
        "store": target_store,
        "stop_type": resolved_stop_type,
    }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
@frappe.whitelist()
def exception_raise(trip, exception_type, severity="Medium", stop_sequence=None,
                    remarks=None, photo=None):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    doc.append("exceptions", {
        "occurred_at": now_datetime(),
        "exception_type": exception_type,
        "severity": severity,
        "stop_sequence": cint(stop_sequence) if stop_sequence else None,
        "remarks": remarks,
        "photo": photo,
        "resolution_status": "Open",
    })
    # If severity High/Critical, mark the corresponding stop as Exception
    if severity in ("High", "Critical") and stop_sequence:
        for s in doc.stops:
            if s.sequence == cint(stop_sequence):
                s.status = "Exception"
                break
    doc.save()
    return True


# ---------------------------------------------------------------------------
# Stop progression (called from driver app)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def stop_arrive(trip, sequence, gps_lat=None, gps_lng=None):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status != "Started":
        frappe.throw(_("Trip must be Started before recording stop arrival"))
    matched = False
    for s in doc.stops:
        if s.sequence == cint(sequence):
            s.status = "Arrived"
            s.ata = now_datetime()
            if gps_lat is not None:
                s.gps_lat = gps_lat
            if gps_lng is not None:
                s.gps_lng = gps_lng
            matched = True
            break
    if not matched:
        frappe.throw(_("Stop sequence {0} not found on trip {1}").format(sequence, trip))
    doc.save()
    return True


@frappe.whitelist()
def stop_complete(trip, sequence, scan_compliance_pct=None, override_exceptions=0):
    doc = frappe.get_doc("CH Logistics Trip", trip)
    if doc.status != "Started":
        frappe.throw(_("Trip must be Started before completing stops"))

    _assert_exception_gate(
        doc,
        action=_("complete stop #{0}").format(cint(sequence)),
        stop_sequence=sequence,
        override_exceptions=override_exceptions,
    )

    matched = False
    for s in doc.stops:
        if s.sequence == cint(sequence):
            s.status = "Completed"
            if scan_compliance_pct is not None:
                s.scan_compliance_pct = scan_compliance_pct
            matched = True
            break
    if not matched:
        frappe.throw(_("Stop sequence {0} not found on trip {1}").format(sequence, trip))
    doc.save()
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _set_driver_availability(driver, status, current_trip):
    """Delegate driver state to the operational status machine.

    Trip lifecycle is authoritative over driver state (``force=True``). Legacy
    labels still passed by older callers are translated to the canonical
    operational states owned by ``driver_status``.
    """
    if not driver:
        return
    translate = {
        "On Trip": ds.ASSIGNED,
        "Available": ds.AVAILABLE,
        "Off Duty": ds.OFFLINE,
        "In Transit": ds.IN_TRANSIT,
    }
    target = translate.get(status, status)
    ds.set_status(driver, target, current_trip=current_trip, force=True)


def _resolve_current_driver():
    """Resolve the Driver doc for the logged-in user.

    Delegates to :func:`ch_logistics.api.driver_resolver.resolve_current_driver`
    so every API surface uses the same lookup chain (User → Driver.user;
    User → Employee.user_id → Driver.employee; Administrator auto-provision).
    """
    from ch_logistics.api.driver_resolver import resolve_current_driver
    return resolve_current_driver(throw=False)


# ---------------------------------------------------------------------------
# Driver-facing read endpoints (consumed by /app/delivery-app)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_driver_trips(driver=None, include_closed_days=0):
    """Return Logistics Trips for the given driver (defaults to current user's
    Driver record). Active trips are returned first; closed/cancelled trips
    within ``include_closed_days`` follow."""
    driver = driver or _resolve_current_driver()
    is_ops = role_registry.user_has("ops_control")
    if not driver and not is_ops:
        return []

    filters = {}
    if driver:
        filters["driver"] = driver

    active_statuses = ("Assigned", "Started")
    active_filters = dict(filters, status=["in", list(active_statuses)])
    active = frappe.get_all(
        "CH Logistics Trip",
        filters=active_filters,
        fields=[
            "name", "trip_date", "status", "direction", "route", "hub_warehouse",
            "driver", "driver_name", "vehicle", "vehicle_number",
            "planned_start", "planned_end", "actual_start", "total_shipments",
        ],
        order_by="planned_start asc, trip_date asc",
        limit=50,
    )

    history = []
    if cint(include_closed_days) > 0:
        cutoff = frappe.utils.add_days(frappe.utils.today(), -cint(include_closed_days))
        hist_filters = dict(filters,
                            status=["in", ["Completed", "Closed", "Cancelled"]],
                            trip_date=[">=", cutoff])
        history = frappe.get_all(
            "CH Logistics Trip",
            filters=hist_filters,
            fields=[
                "name", "trip_date", "status", "direction", "route",
                "driver", "driver_name", "vehicle_number",
                "actual_start", "actual_end", "total_shipments",
            ],
            order_by="trip_date desc",
            limit=50,
        )

    return {"active": active, "history": history}


def _annotate_manifest_stop_links(stops, manifests):
    """Stamp ``pickup_stop_sequence`` / ``drop_stop_sequence`` on each manifest
    dict by matching its source / destination against the trip's stops.

    Matching is tolerant of store-vs-warehouse asymmetry: a stop that only
    carries a CH Store still matches a manifest that only carries the store's
    warehouse (and vice versa) via the CH Store → Warehouse link.
    """
    if not stops or not manifests:
        return

    # One lookup for every CH Store referenced on either side.
    store_names = set()
    for s in stops:
        if s.get("store"):
            store_names.add(s["store"])
    for m in manifests:
        for key in ("source_store", "destination_store"):
            if m.get(key):
                store_names.add(m[key])
    wh_by_store = {}
    if store_names and frappe.db.exists("DocType", "CH Store"):
        for r in frappe.get_all(
            "CH Store",
            filters={"name": ["in", list(store_names)]},
            fields=["name", "warehouse"],
        ):
            if r.warehouse:
                wh_by_store[r.name] = r.warehouse

    def _matches(stop, store, warehouse):
        s_store, s_wh = stop.get("store"), stop.get("warehouse")
        if store and s_store and store == s_store:
            return True
        if warehouse and s_wh and warehouse == s_wh:
            return True
        if warehouse and s_store and wh_by_store.get(s_store) == warehouse:
            return True
        if store and s_wh and wh_by_store.get(store) == s_wh:
            return True
        return False

    def _find(store, warehouse, preferred_types):
        if not (store or warehouse):
            return None
        # Prefer stops typed for this side (Pickup for source, Drop for
        # destination), then fall back to any location match so legacy
        # routes with mis-typed stops still resolve.
        for only_preferred in (True, False):
            for stop in stops:
                stype = (stop.get("stop_type") or "").strip().lower()
                if only_preferred and stype not in preferred_types:
                    continue
                if _matches(stop, store, warehouse):
                    return stop.get("sequence")
        return None

    for m in manifests:
        m["pickup_stop_sequence"] = _find(
            m.get("source_store"), m.get("source_warehouse"),
            ("pickup", "pickup+drop"),
        )
        m["drop_stop_sequence"] = _find(
            m.get("destination_store"), m.get("destination_warehouse"),
            ("drop", "pickup+drop"),
        ) or m.get("stop_sequence")


@frappe.whitelist()
def get_trip_detail(trip):
    """Return a Trip with stops, attached manifests, and open exceptions."""
    doc = frappe.get_doc("CH Logistics Trip", trip)

    # Pre-resolve geocodes for any store / warehouse referenced by the stops
    # so the Delivery App trip map can plot points even before stop GPS or
    # manifest pickup/delivery coords are captured.
    store_meta = frappe.get_meta("CH Store") if frappe.db.exists("DocType", "CH Store") else None
    store_has_lat = bool(store_meta and store_meta.has_field("latitude"))
    store_has_lng = bool(store_meta and store_meta.has_field("longitude"))
    warehouse_meta = frappe.get_meta("Warehouse")
    wh_has_lat = warehouse_meta.has_field("custom_latitude")
    wh_has_lng = warehouse_meta.has_field("custom_longitude")

    store_geo: dict[str, dict[str, float]] = {}
    warehouse_geo: dict[str, dict[str, float]] = {}
    if store_has_lat and store_has_lng:
        store_keys = {s.store for s in doc.stops if s.store}
        for sname in store_keys:
            row = frappe.db.get_value(
                "CH Store", sname, ["latitude", "longitude"], as_dict=True
            ) or {}
            if row.get("latitude") and row.get("longitude"):
                store_geo[sname] = {"lat": row.get("latitude"), "lng": row.get("longitude")}
    if wh_has_lat and wh_has_lng:
        wh_keys = {s.warehouse for s in doc.stops if s.warehouse}
        for wname in wh_keys:
            row = frappe.db.get_value(
                "Warehouse", wname, ["custom_latitude", "custom_longitude"], as_dict=True
            ) or {}
            if row.get("custom_latitude") and row.get("custom_longitude"):
                warehouse_geo[wname] = {
                    "lat": row.get("custom_latitude"),
                    "lng": row.get("custom_longitude"),
                }

    stops = []
    for s in doc.stops:
        sg = store_geo.get(s.store) if s.store else None
        wg = warehouse_geo.get(s.warehouse) if s.warehouse else None
        stops.append({
            "sequence": s.sequence,
            "warehouse": s.warehouse,
            "store": s.store,
            "stop_type": s.stop_type,
            "status": s.status,
            "eta": s.eta,
            "ata": s.ata,
            "gps_lat": s.gps_lat,
            "gps_lng": s.gps_lng,
            "store_lat": sg["lat"] if sg else None,
            "store_lng": sg["lng"] if sg else None,
            "warehouse_lat": wg["lat"] if wg else None,
            "warehouse_lng": wg["lng"] if wg else None,
            "manifest_count": s.manifest_count,
            "scan_compliance_pct": s.scan_compliance_pct,
            "notes": s.notes,
            # Bundle-QR awareness — the driver UI uses these booleans to
            # decide whether to show the consolidated "scan once" dialog
            # (start_stop_pickup / complete_stop_delivery) instead of the
            # legacy per-manifest QR loop. The actual token strings are
            # NEVER returned over the wire; only their presence.
            "has_pickup_token": bool((s.pickup_token or "").strip()),
            "has_delivery_token": bool((s.delivery_token or "").strip()),
        })

    _has_stop_seq = _has_manifest_stop_seq_field()
    _has_shipment_priority = _has_manifest_shipment_priority_field()
    _has_box_count = _has_manifest_box_count_field()
    manifest_meta = frappe.get_meta("CH Transfer Manifest")
    has_pickup_lat = manifest_meta.has_field("pickup_latitude")
    has_pickup_lng = manifest_meta.has_field("pickup_longitude")
    has_delivery_lat = manifest_meta.has_field("delivery_latitude")
    has_delivery_lng = manifest_meta.has_field("delivery_longitude")
    manifest_fields = [
        "name", "status",
        "source_warehouse", "destination_warehouse",
        "source_store", "destination_store",
        "total_stock_entries", "total_items", "total_qty",
        # Proof-of-handling — the driver app renders these on the stop
        # cards so pickup photo / delivery photo stay visible after the
        # combined Arrive flows run.
        "pickup_photo", "pickup_datetime",
        "delivery_photo", "delivery_datetime", "receiver_name",
    ]
    if _has_box_count:
        manifest_fields.append("box_count")
    if _has_shipment_priority:
        manifest_fields.append("shipment_priority")
    if _has_stop_seq:
        manifest_fields.insert(2, "stop_sequence")
    if _has_manifest_direction_field():
        manifest_fields.insert(2, "direction")
    if has_pickup_lat:
        manifest_fields.append("pickup_latitude")
    if has_pickup_lng:
        manifest_fields.append("pickup_longitude")
    if has_delivery_lat:
        manifest_fields.append("delivery_latitude")
    if has_delivery_lng:
        manifest_fields.append("delivery_longitude")

    manifests = frappe.get_all(
        "CH Transfer Manifest",
        filters=({"trip": trip, "docstatus": ["<", 2]} if _has_manifest_trip_field() else {"docstatus": ["<", 2]}),
        fields=manifest_fields,
        order_by=("stop_sequence asc, creation asc" if _has_stop_seq else "creation asc"),
    )
    if not _has_manifest_trip_field():
        manifests = []

    # Resolve, per manifest, WHICH stop serves its pickup (source) and which
    # its delivery (destination). ``stop_sequence`` alone points at only one
    # side (destination for forward trips), so the driver app could never
    # list a manifest — or its pickup photo / QR — under the pickup stop.
    _annotate_manifest_stop_links(stops, manifests)

    exceptions = []
    for e in doc.exceptions:
        exceptions.append({
            "occurred_at": e.occurred_at,
            "exception_type": e.exception_type,
            "severity": e.severity,
            "stop_sequence": e.stop_sequence,
            "remarks": e.remarks,
            "photo": e.photo,
            "resolution_status": e.resolution_status,
        })

    return {
        "name": doc.name,
        "trip_date": doc.trip_date,
        "status": doc.status,
        "direction": doc.direction,
        "route": doc.route,
        "hub_warehouse": doc.hub_warehouse,
        "driver": doc.driver,
        "driver_name": doc.driver_name,
        "driver_phone": doc.driver_phone,
        "vehicle": doc.vehicle,
        "vehicle_number": doc.vehicle_number,
        "planned_start": doc.planned_start,
        "planned_end": doc.planned_end,
        "actual_start": doc.actual_start,
        "actual_end": doc.actual_end,
        "total_shipments": doc.total_shipments,
        "total_distance_actual_km": doc.total_distance_actual_km,
        "total_duration_actual_min": doc.total_duration_actual_min,
        "stops": stops,
        "manifests": manifests,
        "exceptions": exceptions,
    }


# ---------------------------------------------------------------------------
# Ops Control Tower endpoints (consumed by /app/logistics-control-tower)
# ---------------------------------------------------------------------------
def _require_ops():
    role_registry.require("ops_control", _("perform operations-console actions"))


def _trip_audit(doc, message: str) -> None:
    if not message:
        return
    try:
        doc.add_comment("Comment", message)
    except Exception:
        frappe.log_error(
            title=f"trip audit comment failed for {doc.name}",
            message=frappe.get_traceback(),
        )


def _can_override_exception_gate() -> bool:
    return role_registry.user_has("head_override")


def _blocking_high_critical_exceptions(doc, stop_sequence=None) -> list[dict]:
    blocked = []
    target_seq = cint(stop_sequence) if stop_sequence is not None else None
    for row in doc.get("exceptions") or []:
        severity = (row.get("severity") or "").strip()
        resolution = (row.get("resolution_status") or "Open").strip()
        if severity not in ("High", "Critical"):
            continue
        if resolution in ("Resolved",):
            continue
        if target_seq is not None and cint(row.get("stop_sequence") or 0) != target_seq:
            continue
        blocked.append(
            {
                "severity": severity,
                "stop_sequence": cint(row.get("stop_sequence") or 0) or None,
                "exception_type": row.get("exception_type") or _("Unknown"),
                "resolution_status": resolution or "Open",
            }
        )
    return blocked


def _assert_exception_gate(doc, *, action: str, stop_sequence=None, override_exceptions=0):
    blocked = _blocking_high_critical_exceptions(doc, stop_sequence=stop_sequence)
    if not blocked:
        return

    if cint(override_exceptions):
        if not _can_override_exception_gate():
            frappe.throw(
                _("Only Logistics Head / System Manager can override unresolved High/Critical exceptions."),
                frappe.PermissionError,
            )
        _trip_audit(
            doc,
            _(
                "Exception override approved by {0} for action '{1}'. Open critical rows: {2}"
            ).format(
                frappe.session.user,
                action,
                ", ".join(
                    f"{b['exception_type']}@#{b['stop_sequence'] or '-'} ({b['severity']}/{b['resolution_status']})"
                    for b in blocked
                ),
            ),
        )
        return

    raise_rows = ", ".join(
        f"{b['exception_type']}@#{b['stop_sequence'] or '-'} ({b['severity']}/{b['resolution_status']})"
        for b in blocked
    )
    frappe.throw(
        _(
            "Cannot {0}. Unresolved High/Critical exceptions exist: {1}. "
            "Resolve them first, or use Logistics Head override."
        ).format(action, raise_rows),
        title=_("Open Critical Exceptions"),
    )


def _is_stop_terminal(status: str | None) -> bool:
    return (status or "").strip() in ("Completed", "Skipped")


def _stop_has_been_reached(status: str | None) -> bool:
    return (status or "").strip() in ("Arrived", "Completed", "Skipped")


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    lat1, lng1, lat2, lng2 = map(flt, (lat1, lng1, lat2, lng2))
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _store_coords(store: str | None):
    if not store:
        return None
    lat, lng = frappe.db.get_value("CH Store", store, ["latitude", "longitude"]) or (None, None)
    if lat and lng:
        return (flt(lat), flt(lng))
    return None


def _warehouse_coords(warehouse: str | None):
    if not warehouse:
        return None
    # Geo custom fields are installed by patch v0_0_4 — tolerate their
    # absence (e.g. a prod restore that lost Custom Fields) instead of
    # crashing every coord lookup with an unknown-column error.
    if frappe.db.has_column("Warehouse", "custom_latitude"):
        lat, lng = frappe.db.get_value(
            "Warehouse", warehouse, ["custom_latitude", "custom_longitude"]
        ) or (None, None)
        if lat and lng:
            return (flt(lat), flt(lng))

    store = (
        frappe.db.get_value("Warehouse", warehouse, "ch_store")
        or frappe.db.get_value("CH Store", {"warehouse": warehouse}, "name")
    )
    return _store_coords(store)


def _stop_coords(stop):
    if flt(stop.get("gps_lat")) and flt(stop.get("gps_lng")):
        return (flt(stop.get("gps_lat")), flt(stop.get("gps_lng")))
    return _store_coords(stop.get("store")) or _warehouse_coords(stop.get("warehouse"))


def _trip_live_origin(trip_doc):
    if trip_doc.driver and frappe.db.has_column("Driver", "current_lat") and frappe.db.has_column("Driver", "current_lng"):
        row = frappe.db.get_value(
            "Driver",
            trip_doc.driver,
            ["current_lat", "current_lng"],
            as_dict=True,
        ) or {}
        if row.get("current_lat") and row.get("current_lng"):
            return (flt(row.get("current_lat")), flt(row.get("current_lng")))

    if trip_doc.name:
        row = frappe.db.get_value(
            "CH Driver Location",
            {"trip": trip_doc.name},
            ["latitude", "longitude"],
            order_by="captured_at desc",
            as_dict=True,
        ) or {}
        if row.get("latitude") and row.get("longitude"):
            return (flt(row.get("latitude")), flt(row.get("longitude")))

    reached = sorted(
        [s for s in (trip_doc.stops or []) if _stop_has_been_reached(s.get("status"))],
        key=lambda r: cint(r.get("sequence") or 0),
    )
    if reached:
        c = _stop_coords(reached[-1])
        if c:
            return c

    return _warehouse_coords(trip_doc.get("hub_warehouse"))


@frappe.whitelist()
def ops_board(trip_date=None, include_days=1):
    """Trips for the day grouped by status, with light KPI summary.

    In addition to date-windowed trips, always surface every Draft
    trip (the dispatcher's backlog) regardless of trip_date.  Draft
    trips with stale or missing planned dates are precisely the ones
    that need scheduling — hiding them behind the date filter created
    the bug where freshly Packed manifests attached to a stale-dated
    Draft trip "vanished" from Operations.  Mirrors SAP TM Freight
    Order Cockpit / Oracle TM Transportation Cockpit behaviour where
    every un-dispatched freight order stays in the cockpit until it is
    either scheduled + assigned or cancelled.
    """
    _require_ops()
    trip_date = trip_date or frappe.utils.today()
    end_date = frappe.utils.add_days(trip_date, max(cint(include_days) - 1, 0))

    fields = [
        "name", "trip_date", "status", "direction", "route",
        "hub_warehouse", "driver", "driver_name", "vehicle_number",
        "planned_start", "planned_end", "actual_start", "actual_end",
        "total_shipments",
    ]

    dated_rows = frappe.get_all(
        "CH Logistics Trip",
        filters={"trip_date": ["between", [trip_date, end_date]]},
        fields=fields,
        order_by="trip_date asc, planned_start asc, name asc",
        limit=200,
    )

    # Backlog: Draft trips outside the date window (un-scheduled or
    # carrying a stale planned date).  Returned regardless of the date
    # filter so the dispatcher can schedule + assign drivers.  Cap
    # separately so a huge backlog can't crowd out today's view.
    dated_names = {r.name for r in dated_rows}
    backlog_rows = frappe.get_all(
        "CH Logistics Trip",
        filters={"status": "Draft", "name": ["not in", list(dated_names) or [""]]},
        fields=fields,
        order_by="creation desc, name asc",
        limit=50,
    )

    # Merge while keeping order: dated first, then backlog.  Dedupe by
    # name in case a trip qualifies for both.
    seen = set()
    rows = []
    for r in dated_rows + backlog_rows:
        if r.name in seen:
            continue
        seen.add(r.name)
        rows.append(r)

    # Count open exceptions per trip in one query
    exc_rows = frappe.db.sql(
        """
        SELECT parent AS trip, COUNT(*) AS open_count,
               SUM(CASE WHEN severity IN ('High', 'Critical') THEN 1 ELSE 0 END) AS sev_high
        FROM `tabCH Logistics Exception`
        WHERE parenttype = 'CH Logistics Trip'
          AND IFNULL(resolution_status, 'Open') = 'Open'
          AND parent IN %(trips)s
        GROUP BY parent
        """,
        {"trips": tuple([r.name for r in rows]) or ("__none__",)},
        as_dict=True,
    )
    exc_map = {r.trip: r for r in exc_rows}
    for r in rows:
        e = exc_map.get(r.name)
        r["open_exceptions"] = cint(e and e.open_count)
        r["critical_exceptions"] = cint(e and e.sev_high)

    buckets = {"Draft": [], "Assigned": [], "Started": [], "Completed": [], "Closed": [], "Cancelled": []}
    for r in rows:
        buckets.setdefault(r.status, []).append(r)

    return {
        "trip_date": trip_date,
        "end_date": end_date,
        "buckets": buckets,
        "totals": {k: len(v) for k, v in buckets.items()},
    }


@frappe.whitelist()
def ops_lifecycle_counts(trip_date=None, include_days=1):
    """Per-stage counts for the Operations lifecycle strip.

    Mirrors how SAP TM Freight Order Cockpit / Manhattan TMS Active Dock
    show the journey of a shipment as discrete chips above the trip board:

        Draft Manifest → Packed → Trip Planned → In Transit → Delivered

    Each chip is a count of *real* docs currently in that stage; the
    front-end lets the dispatcher click a chip to filter the canvas.
    Counts are scoped to the same date window the trip board uses so the
    numbers stay consistent with what the dispatcher sees on the board.
    """
    _require_ops()
    trip_date = trip_date or frappe.utils.today()
    end_date = frappe.utils.add_days(trip_date, max(cint(include_days) - 1, 0))

    # Manifest-side counts (not bound to a trip yet).
    manifest_base = {"docstatus": ["<", 2]}
    if _has_manifest_trip_field():
        manifest_base["trip"] = ["in", [None, ""]]

    draft_manifests = frappe.db.count(
        "CH Transfer Manifest",
        filters={**manifest_base, "status": "Draft"},
    )
    packed_manifests = frappe.db.count(
        "CH Transfer Manifest",
        filters={**manifest_base, "status": "Packed"},
    )

    # Trip-side counts inside the date window. We aggregate Draft+Assigned
    # under "Trip Planned" because both represent a trip that is built but
    # has not physically left the hub — Draft = still being assembled,
    # Assigned = driver attached, waiting to start. This is the same
    # grouping SAP TM uses in the Freight Order Monitor ("Open" + "Ready").
    trip_filters = {"trip_date": ["between", [trip_date, end_date]]}
    trip_status_count = frappe.db.sql(
        """
        SELECT status, COUNT(*) AS cnt
        FROM `tabCH Logistics Trip`
        WHERE trip_date BETWEEN %(start)s AND %(end)s
        GROUP BY status
        """,
        {"start": trip_date, "end": end_date},
        as_dict=True,
    )
    by_status = {r.status: cint(r.cnt) for r in trip_status_count}
    planned_trips = by_status.get("Draft", 0) + by_status.get("Assigned", 0)
    in_transit_trips = by_status.get("Started", 0)
    delivered_trips = by_status.get("Completed", 0) + by_status.get("Closed", 0)

    # Also count manifests in transit / delivered today, because a trip can
    # carry multiple manifests and the dispatcher cares about shipment-level
    # throughput, not just truck-level.
    in_transit_manifests = frappe.db.count(
        "CH Transfer Manifest",
        filters={"docstatus": ["<", 2], "status": "In Transit"},
    )
    delivered_manifests_today = frappe.db.sql(
        """
        SELECT COUNT(*) AS cnt
        FROM `tabCH Transfer Manifest`
        WHERE status IN ('Delivered', 'Closed')
          AND DATE(IFNULL(delivery_datetime, modified)) BETWEEN %(start)s AND %(end)s
        """,
        {"start": trip_date, "end": end_date},
        as_dict=True,
    )
    delivered_manifests = cint(delivered_manifests_today[0].cnt if delivered_manifests_today else 0)

    return {
        "trip_date": trip_date,
        "end_date": end_date,
        "stages": [
            {
                "key": "draft",
                "label": _("Draft Manifests"),
                "count": draft_manifests,
                "hint": _("Manifests being prepared at pack station."),
                "icon": "fa-file-text-o",
            },
            {
                "key": "packed",
                "label": _("Packed"),
                "count": packed_manifests,
                "hint": _("Packed manifests waiting to be attached to a trip."),
                "icon": "fa-cube",
            },
            {
                "key": "planned",
                "label": _("Trip Planned"),
                "count": planned_trips,
                "hint": _("Trips Draft + Assigned. Driver may still be unassigned."),
                "icon": "fa-list-ol",
            },
            {
                "key": "in_transit",
                "label": _("In Transit"),
                "count": in_transit_trips,
                "secondary_count": in_transit_manifests,
                "hint": _("Trips currently on the road (manifests in transit also shown)."),
                "icon": "fa-truck",
            },
            {
                "key": "delivered",
                "label": _("Delivered"),
                "count": delivered_trips,
                "secondary_count": delivered_manifests,
                "hint": _("Trips completed/closed within the date window."),
                "icon": "fa-check-circle",
            },
        ],
    }


@frappe.whitelist()
def ops_unassigned_manifests(direction=None, hub=None, limit=100):
    """CH Transfer Manifests that are ready to be attached to a trip.

    Only returns manifests in attachable (pre-dispatch) statuses — anything
    already Assigned/In Transit/Delivered/Closed/Cancelled/etc. is hidden
    from the dispatcher's worklist, matching market-standard TMS cockpit
    behaviour (Oracle Transportation Management, SAP TM, Manhattan TMS).
    """
    _require_ops()
    filters = {
        "docstatus": ["<", 2],
        "status": ["in", list(_OPS_ATTACHABLE_MANIFEST_STATUSES)],
    }
    if _has_manifest_trip_field():
        filters["trip"] = ["in", [None, ""]]
    has_direction = _has_manifest_direction_field()
    if direction and has_direction:
        filters["direction"] = direction
    if hub:
        # Hub matches the source for forward lanes and the destination for
        # reverse lanes (returns/pickups flow back into the hub).
        if direction == "Reverse":
            filters["destination_warehouse"] = hub
        else:
            filters["source_warehouse"] = hub
    has_stop_seq = _has_manifest_stop_seq_field()
    has_shipment_priority = _has_manifest_shipment_priority_field()
    has_box_count = _has_manifest_box_count_field()
    fields = [
        "name", "status",
        "source_warehouse", "destination_warehouse",
        "source_store", "destination_store",
        "total_stock_entries", "total_items", "total_qty",
        "creation",
    ]
    if has_box_count:
        fields.append("box_count")
    if has_shipment_priority:
        fields.append("shipment_priority")
    if has_stop_seq:
        fields.insert(2, "stop_sequence")
    if has_direction:
        fields.insert(2, "direction")

    return frappe.get_all(
        "CH Transfer Manifest",
        filters=filters,
        fields=fields,
        order_by=("shipment_priority desc, creation asc" if has_shipment_priority else "creation asc"),
        limit=cint(limit) or 100,
    )


@frappe.whitelist()
def ops_recall_inbox(limit=100):
    """In-flight transfer recalls awaiting physical return + stock reversal.

    Mirrors the "Returns Cockpit" pattern from SAP TM / Oracle TM /
    Manhattan TMS: every recalled manifest stays in the dispatcher's
    inbox with its original trip, driver, vehicle and a recall-age clock
    until someone at the source warehouse confirms physical return
    (which reverses the underlying Stock Entries).
    """
    _require_ops()
    has_trip = _has_manifest_trip_field()
    fields = [
        "name", "status",
        "source_warehouse", "destination_warehouse",
        "source_store", "destination_store",
        "total_stock_entries", "total_items", "total_qty",
        "recall_reason", "recall_notes",
        "recall_initiated_by", "recall_initiated_at",
        "creation",
    ]
    if has_trip:
        fields.append("trip")
    if _has_manifest_direction_field():
        fields.append("direction")

    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters={"status": "Recall Initiated"},
        fields=fields,
        order_by="recall_initiated_at desc, modified desc",
        limit=cint(limit) or 100,
    )

    # Enrich with driver / vehicle context from the originating trip so the
    # dispatcher can chase the driver without opening the manifest form.
    trip_names = {r.get("trip") for r in rows if r.get("trip")}
    trip_info = {}
    if trip_names:
        for t in frappe.get_all(
            "CH Logistics Trip",
            filters={"name": ["in", list(trip_names)]},
            fields=[
                "name", "status", "driver", "driver_name",
                "driver_phone", "vehicle_number",
            ],
        ):
            trip_info[t.name] = t

    now = now_datetime()
    for r in rows:
        ti = trip_info.get(r.get("trip")) if r.get("trip") else None
        r["trip_status"] = ti.status if ti else None
        r["driver"] = ti.driver if ti else None
        r["driver_name"] = ti.driver_name if ti else None
        r["driver_phone"] = ti.driver_phone if ti else None
        r["vehicle_number"] = ti.vehicle_number if ti else None
        if r.get("recall_initiated_at"):
            age_seconds = (now - r["recall_initiated_at"]).total_seconds()
            r["recall_age_hours"] = round(age_seconds / 3600.0, 1)
        else:
            r["recall_age_hours"] = None

    return rows


@frappe.whitelist()
def ops_packing_queue(limit=100):
    """Draft manifests awaiting pack-station processing.

    Returns each Draft manifest plus the running packing totals derived
    from the CH Transfer Package child table (carton count, packed
    qty, total weight, last packer).  Mirrors the "Pack Station Work
    Queue" view from Oracle WMS Cloud and Manhattan Active WMS where
    packers see every open order with its remaining-to-pack count.
    """
    _require_ops()
    fields = [
        "name", "status",
        "source_warehouse", "destination_warehouse",
        "source_store", "destination_store",
        "total_stock_entries", "total_items", "total_qty",
        "manifest_date", "creation",
    ]
    if _has_manifest_box_count_field():
        fields.append("box_count")
    if _has_manifest_shipment_priority_field():
        fields.append("shipment_priority")
    if _has_manifest_direction_field():
        fields.append("direction")

    rows = frappe.get_all(
        "CH Transfer Manifest",
        filters={"docstatus": 0, "status": "Draft"},
        fields=fields,
        order_by=(
            "shipment_priority desc, creation asc"
            if _has_manifest_shipment_priority_field() else "creation asc"
        ),
        limit=cint(limit) or 100,
    )

    if not rows:
        return rows

    names = [r["name"] for r in rows]
    # Aggregate packed_qty / total weight / max packed_at per parent
    # in a single query so the queue is O(1) round-trips even with a
    # large worklist.
    pkg_rows = frappe.db.sql(
        """
        SELECT parent,
               COUNT(*)               AS box_count,
               COALESCE(SUM(packed_qty), 0)  AS packed_qty,
               COALESCE(SUM(weight_kg), 0)   AS total_weight_kg,
               MAX(packed_at)         AS last_packed_at,
               MAX(packed_by)         AS last_packed_by
        FROM `tabCH Transfer Package`
        WHERE parent IN %(names)s
        GROUP BY parent
        """,
        {"names": tuple(names)},
        as_dict=True,
    )
    agg = {p["parent"]: p for p in pkg_rows}
    now = now_datetime()
    for r in rows:
        a = agg.get(r["name"]) or {}
        r["pkg_box_count"] = int(a.get("box_count") or 0)
        r["pkg_packed_qty"] = float(a.get("packed_qty") or 0)
        r["pkg_total_weight_kg"] = float(a.get("total_weight_kg") or 0)
        r["pkg_last_packed_at"] = a.get("last_packed_at")
        r["pkg_last_packed_by"] = a.get("last_packed_by")
        total_qty = float(r.get("total_qty") or 0)
        r["pkg_remaining_qty"] = max(total_qty - r["pkg_packed_qty"], 0)
        if r.get("creation"):
            r["age_hours"] = round((now - r["creation"]).total_seconds() / 3600.0, 1)
        else:
            r["age_hours"] = None

    return rows


@frappe.whitelist()
def ops_exception_inbox(resolution_status="Open", limit=100):
    """Open exceptions across all trips, newest first."""
    _require_ops()
    rs = resolution_status or "Open"
    rows = frappe.db.sql(
        """
        SELECT e.name AS row_name, e.parent AS trip, e.idx, e.occurred_at,
               e.exception_type, e.severity, e.stop_sequence,
               e.remarks, e.photo, e.resolution_status, e.escalated_to,
               t.driver, t.driver_name, t.hub_warehouse, t.status AS trip_status
        FROM `tabCH Logistics Exception` e
        INNER JOIN `tabCH Logistics Trip` t ON t.name = e.parent
        WHERE e.parenttype = 'CH Logistics Trip'
          AND IFNULL(e.resolution_status, 'Open') = %(rs)s
        ORDER BY FIELD(e.severity, 'Critical', 'High', 'Medium', 'Low'),
                 e.occurred_at DESC
        LIMIT %(limit)s
        """,
        {"rs": rs, "limit": cint(limit) or 100},
        as_dict=True,
    )
    return rows


@frappe.whitelist()
def ops_drivers_available():
    """Drivers with their current availability + active trip if any."""
    _require_ops()
    meta = frappe.get_meta("Driver")
    has_avail = meta.has_field("availability_status")
    has_curr = meta.has_field("current_trip")

    fields = ["name", "full_name", "status", "cell_number"]
    if has_avail:
        fields.append("availability_status")
    if has_curr:
        fields.append("current_trip")

    rows = frappe.get_all(
        "Driver",
        filters={"status": ["!=", "Suspended"]},
        fields=fields,
        order_by="full_name asc",
        limit=200,
    )
    return rows


@frappe.whitelist()
def exception_resolve(trip, row_name, resolution_status="Resolved"):
    """Close or update an exception row by its child name."""
    _require_ops()
    doc = frappe.get_doc("CH Logistics Trip", trip)
    matched = False
    for e in doc.exceptions:
        if e.name == row_name:
            e.resolution_status = resolution_status
            if resolution_status == "Resolved":
                e.resolved_at = now_datetime()
            matched = True
            break
    if not matched:
        frappe.throw(_("Exception {0} not found on trip {1}").format(row_name, trip))
    doc.save()
    return True

# ---------------------------------------------------------------------------
# Phase B — Reverse Logistics helpers
# ---------------------------------------------------------------------------
def _resolve_buyback_bin(company):
    """Return the company's Buyback Bin warehouse if any (best effort).
    Matches Warehouse.warehouse_name == 'Buyback Bin' (common ERPNext
    convention where ``name`` is suffixed with the company abbr)."""
    if not company:
        return None
    rows = frappe.get_all(
        "Warehouse",
        filters={
            "company": company,
            "is_group": 0,
            "warehouse_name": "Buyback Bin",
        },
        pluck="name",
        order_by="disabled asc, name asc",
        limit=1,
    )
    if rows:
        return rows[0]
    # Fallback: name starts with "Buyback Bin"
    rows = frappe.get_all(
        "Warehouse",
        filters={
            "company": company,
            "is_group": 0,
            "name": ["like", "Buyback Bin%"],
        },
        pluck="name",
        order_by="disabled asc, name asc",
        limit=1,
    )
    return rows[0] if rows else None


@frappe.whitelist()
def get_reverse_destination(company):
    """Default destination warehouse for reverse movements (Buyback Bin)."""
    return _resolve_buyback_bin(company)


@frappe.whitelist()
def reverse_manifest_create(company, source_store, source_warehouse,
                            stock_entries=None, destination_warehouse=None,
                            manifest_date=None, notes=None):
    """Create a Draft CH Transfer Manifest with direction='Reverse', wrapping
    the supplied stock entries that move stock from source_warehouse → hub
    (Buyback Bin by default)."""
    if not destination_warehouse:
        destination_warehouse = _resolve_buyback_bin(company)
        if not destination_warehouse:
            frappe.throw(_("No Buyback Bin warehouse configured for company {0}").format(company))

    if isinstance(stock_entries, str):
        stock_entries = frappe.parse_json(stock_entries) or []
    stock_entries = stock_entries or []

    doc = frappe.new_doc("CH Transfer Manifest")
    doc.manifest_date = manifest_date or frappe.utils.today()
    doc.company = company
    doc.source_store = source_store
    doc.source_warehouse = source_warehouse
    doc.destination_warehouse = destination_warehouse
    if doc.meta.has_field("direction"):
        doc.direction = "Reverse"
    if notes:
        doc.notes = notes
    for se in stock_entries:
        doc.append("transfers", {"stock_entry": se})
    doc.insert(ignore_mandatory=True)
    return doc.name


# ---------------------------------------------------------------------------
# Phase C — Trip auto-planner
# ---------------------------------------------------------------------------
@frappe.whitelist()
def auto_plan_trips(trip_date=None, direction="Forward", hub_warehouse=None,
                    company=None, max_stops=20, driver=None, commit=0):
    """Group unassigned manifests by store and propose (or commit) trips.

    For Forward: source_warehouse = hub, group by destination_store (Drop stops).
    For Reverse: destination_warehouse = hub, group by source_store (Pickup stops).
    For Mixed:   pull BOTH lanes and merge per store/warehouse, so a single
                 visit can drop outbound shipments and pick up returns in one
                 optimized trip (Pickup+Drop stops where both apply).
    """
    _require_ops()
    trip_date = trip_date or frappe.utils.today()
    max_stops = max(cint(max_stops) or 20, 1)
    direction = direction or "Forward"

    if not company:
        company = frappe.defaults.get_user_default("company") or frappe.db.get_value("Company", {}, "name")

    has_direction = frappe.get_meta("CH Transfer Manifest").has_field("direction")
    has_stop_seq = frappe.get_meta("CH Transfer Manifest").has_field("stop_sequence")
    has_shipment_priority = _has_manifest_shipment_priority_field()

    has_box_count = _has_manifest_box_count_field()
    fields = [
        "name",
        "source_warehouse", "source_store",
        "destination_warehouse", "destination_store",
        "total_qty",
    ]
    if has_box_count:
        fields.append("box_count")
    if has_shipment_priority:
        fields.append("shipment_priority")
    if has_direction:
        fields.insert(1, "direction")

    def _pull(lane):
        f = {"trip": ["in", [None, ""]], "docstatus": ["<", 2]}
        if has_direction:
            f["direction"] = lane
        if hub_warehouse:
            if lane == "Forward":
                f["source_warehouse"] = hub_warehouse
            else:
                f["destination_warehouse"] = hub_warehouse
        if company:
            f["company"] = company
        return frappe.get_all(
            "CH Transfer Manifest", filters=f, fields=fields,
            order_by=("shipment_priority desc, creation asc" if has_shipment_priority else "creation asc"),
            limit=500,
        )

    if direction == "Mixed":
        # De-dup in case the same manifest is returned by both lanes (it won't
        # under normal data, but guard anyway).
        seen = set()
        mfs = []
        for m in _pull("Forward") + _pull("Reverse"):
            if m["name"] in seen:
                continue
            seen.add(m["name"])
            mfs.append(m)
    else:
        mfs = _pull(direction)

    if not mfs:
        return {"proposals": [], "created": [], "skipped_reason": _("No unassigned manifests match the filter.")}

    def _stop_key(m):
        """(store, warehouse, role) for the stop that serves this manifest."""
        eff = (m.get("direction") if direction == "Mixed" else direction) or "Forward"
        if eff == "Reverse":
            return (m.get("source_store") or "", m.get("source_warehouse") or "", "Pickup")
        return (m.get("destination_store") or "", m.get("destination_warehouse") or "", "Drop")

    def _stop_type(roles):
        if "Pickup" in roles and "Drop" in roles:
            return "Pickup+Drop"
        if "Pickup" in roles:
            return "Pickup"
        return "Drop"

    # Group manifests by (store, warehouse); a single physical stop merges
    # outbound drops and return pickups at the same location.
    groups = {}
    for m in mfs:
        store, warehouse, role = _stop_key(m)
        if not warehouse:
            continue
        g = groups.setdefault((store, warehouse), {"roles": set(), "manifests": []})
        g["roles"].add(role)
        g["manifests"].append(m["name"])

    # Bucket into trips of max_stops; each store/warehouse contributes 1 stop
    proposals = []
    bucket = {"stops": [], "manifests": []}
    for (store, warehouse), g in groups.items():
        bucket["stops"].append({
            "store": store or None,
            "warehouse": warehouse,
            "stop_type": _stop_type(g["roles"]),
            "manifest_count": len(g["manifests"]),
        })
        bucket["manifests"].extend(g["manifests"])
        if len(bucket["stops"]) >= max_stops:
            proposals.append(bucket)
            bucket = {"stops": [], "manifests": []}
    if bucket["stops"]:
        proposals.append(bucket)

    if not cint(commit):
        return {"proposals": proposals, "created": []}

    # Commit: create Route + Trip per proposal, attach manifests
    created = []
    for idx, p in enumerate(proposals, start=1):
        route = frappe.new_doc("CH Route")
        route.route_name = f"AUTO-{direction[:3].upper()}-{frappe.utils.now_datetime().strftime('%Y%m%d-%H%M%S')}-{idx}"
        route.company = company
        route.hub_warehouse = hub_warehouse or p["stops"][0]["warehouse"]
        for seq, s in enumerate(p["stops"], start=1):
            route.append("stops", {
                "sequence": seq,
                "warehouse": s["warehouse"],
                "store": s["store"] or None,
                "stop_type": s.get("stop_type") or "Drop",
            })
        route.insert(ignore_permissions=True)

        trip_name = trip_create(
            trip_date=trip_date,
            company=company,
            route=route.name,
            driver=driver,
            direction=direction,
        )
        # Map each stop's (store, warehouse) to its sequence
        stop_seq_map = {}
        for seq, s in enumerate(p["stops"], start=1):
            stop_seq_map[(s["store"] or "", s["warehouse"] or "")] = seq

        for mf_name in p["manifests"]:
            mf_fields = ["source_store", "source_warehouse", "destination_store", "destination_warehouse"]
            if has_direction:
                mf_fields.insert(0, "direction")
            mf = frappe.db.get_value("CH Transfer Manifest", mf_name, mf_fields, as_dict=True)
            eff = ((mf.get("direction") if has_direction and direction == "Mixed" else direction) or "Forward")
            if eff == "Reverse":
                store, wh = mf.source_store, mf.source_warehouse
            else:
                store, wh = mf.destination_store, mf.destination_warehouse
            seq = stop_seq_map.get((store or "", wh or ""))
            if seq and has_stop_seq:
                frappe.db.set_value("CH Transfer Manifest", mf_name, "stop_sequence", seq)

        _attach_manifests(trip_name, p["manifests"])
        created.append({"trip": trip_name, "route": route.name, "stops": len(p["stops"]),
                        "manifests": len(p["manifests"])})

    return {"proposals": proposals, "created": created}


# ---------------------------------------------------------------------------
# Phase F: Map data for Logistics Control Tower
# ---------------------------------------------------------------------------

@frappe.whitelist()
def ops_map_data(trip_date=None, include_days=1, statuses=None):
    """Pickup/delivery coordinates for trips in the visible date range.

    Returns a flat list of trip points so the front-end can plot Leaflet
    markers + per-trip polylines without further joins.

    Only manifests that carry at least one valid coordinate are returned.
    Coordinates live on ``CH Transfer Manifest`` (``pickup_latitude``,
    ``pickup_longitude``, ``delivery_latitude``, ``delivery_longitude``)
    and are populated by the driver/agent when picking up or delivering.
    """
    _require_ops()
    trip_date = trip_date or frappe.utils.today()
    end_date = frappe.utils.add_days(trip_date, max(cint(include_days) - 1, 0))

    status_list = None
    if statuses:
        if isinstance(statuses, str):
            try:
                status_list = json.loads(statuses)
            except Exception:
                status_list = [s.strip() for s in statuses.split(",") if s.strip()]
        else:
            status_list = list(statuses)

    trip_filters = {"trip_date": ["between", [trip_date, end_date]]}
    if status_list:
        trip_filters["status"] = ["in", status_list]

    trips = frappe.get_all(
        "CH Logistics Trip",
        filters=trip_filters,
        fields=["name", "status", "direction", "driver_name", "vehicle_number",
                "trip_date", "planned_start"],
        limit=200,
    )
    if not trips:
        return {"trips": [], "manifests_with_coords": 0, "manifests_total": 0}

    trip_map = {t.name: t for t in trips}

    if not _has_manifest_trip_field():
        return {"trips": [], "manifests_with_coords": 0, "manifests_total": 0}

    manifest_fields = [
        "name", "trip", "status",
        "source_warehouse", "destination_warehouse",
        "source_store", "destination_store",
        "pickup_lat", "pickup_lng",
        "delivery_lat", "delivery_lng",
    ]
    has_direction = _has_manifest_direction_field()
    if has_direction:
        manifest_fields.insert(3, "direction")

    manifests = frappe.get_all(
        "CH Transfer Manifest",
        filters={"trip": ["in", list(trip_map.keys())], "docstatus": ["<", 2]},
        fields=manifest_fields,
        limit=2000,
    )

    total = len(manifests)
    with_coords = 0
    by_trip: dict[str, dict] = {}
    for m in manifests:
        pickup = None
        delivery = None
        if m.pickup_lat and m.pickup_lng:
            pickup = {
                "lat": flt(m.pickup_lat),
                "lng": flt(m.pickup_lng),
                "warehouse": m.source_warehouse,
                "store": m.source_store,
            }
        if m.delivery_lat and m.delivery_lng:
            delivery = {
                "lat": flt(m.delivery_lat),
                "lng": flt(m.delivery_lng),
                "warehouse": m.destination_warehouse,
                "store": m.destination_store,
            }
        if not pickup and not delivery:
            continue
        with_coords += 1
        bucket = by_trip.setdefault(m.trip, {
            "trip": m.trip,
            "status": trip_map[m.trip].status,
            "direction": trip_map[m.trip].direction,
            "driver_name": trip_map[m.trip].driver_name,
            "vehicle_number": trip_map[m.trip].vehicle_number,
            "manifests": [],
        })
        bucket["manifests"].append({
            "name": m.name, "status": m.status, "direction": m.get("direction"),
            "pickup": pickup, "delivery": delivery,
        })

    return {
        "trips": list(by_trip.values()),
        "manifests_with_coords": with_coords,
        "manifests_total": total,
    }


# ---------------------------------------------------------------------------
# Stop-level QR scanning
# ---------------------------------------------------------------------------
#
# Business model
# ──────────────
# A single trip may carry many CH Transfer Manifests for the same destination
# (e.g. five Store-1 transfer requests on one truck). Today every manifest has
# its own QR. That works for full traceability, but the packing/driver team
# wants ONE scannable label per destination drop so a multi-request load can
# be picked up and dropped with a single scan.
#
# Implementation
# ──────────────
# 1. CH Logistics Trip Stop now owns two random tokens (pickup_token,
#    delivery_token) generated at trip save (see _ensure_stop_tokens).
# 2. The packing team prints one consolidated label per stop using
#    get_stop_label() — the QR encodes the pickup_token.
# 3. start_stop_pickup() / complete_stop_delivery() validate the scan against
#    the stop tokens and cascade to every manifest sitting on that stop using
#    the existing transfer-manifest pickup / delivery flow. Per-manifest audit
#    fields (photos, GPS, scanned_qr) are still stamped on each manifest, so
#    nothing in the downstream stock receipt or compliance reports changes.
#
# Stock semantics are unchanged: the source Stock Entry already moved goods
# to "Goods In Transit" before the manifest was assigned. The stop scan only
# governs the physical handover; the destination Stock Entry / receipt fires
# from the per-manifest accept_delivery / receive flow as before.

def _get_trip_stop(trip_doc, sequence):
    seq = cint(sequence)
    for s in trip_doc.stops:
        if cint(s.sequence) == seq:
            return s
    frappe.throw(
        _("Stop sequence {0} not found on trip {1}").format(seq, trip_doc.name),
        title=_("API Error"),
    )


def _stop_manifest_rows(trip, sequence):
    """All non-cancelled manifests bound to this trip + stop sequence."""
    if not _has_manifest_trip_field() or not _has_manifest_stop_seq_field():
        return []
    return frappe.get_all(
        "CH Transfer Manifest",
        filters={"trip": trip, "stop_sequence": cint(sequence), "docstatus": ["<", 2]},
        fields=["name", "status"],
        order_by="creation asc",
    )


@frappe.whitelist()
def start_stop_pickup(trip, sequence, scanned_qr, pickup_photo,
                      lat=None, lng=None, notes=None):
    """Driver scans the consolidated pickup label for a whole stop.

    Validates the scanned token against CH Logistics Trip Stop.pickup_token
    and runs start_pickup for every manifest under that stop. Per-manifest QR
    is also stamped (scanned_qr=stop_token) so each manifest's individual
    audit log still records what was scanned at pickup time.
    """
    trip_doc = frappe.get_doc("CH Logistics Trip", trip)
    trip_doc.check_permission("write")
    stop = _get_trip_stop(trip_doc, sequence)
    scanned = (scanned_qr or "").strip()
    if not scanned:
        frappe.throw(_("QR scan is mandatory to start pickup for this stop."),
                     title=_("Scan Required"))
    expected = (stop.get("pickup_token") or "").strip()
    if not expected:
        frappe.throw(_("This stop has no pickup token yet. Re-save the trip to mint one."),
                     title=_("Token Missing"))
    if scanned != expected:
        frappe.throw(_("Scanned QR does not match the pickup label for stop #{0}.").format(stop.sequence),
                     title=_("Wrong Label"))

    rows = _stop_manifest_rows(trip_doc.name, stop.sequence)
    if not rows:
        frappe.throw(_("No manifests are attached to stop #{0}.").format(stop.sequence),
                     title=_("Empty Stop"))

    started, skipped = [], []
    audit_suffix = _("Consolidated stop pickup — driver scanned stop QR {0}.").format(scanned)
    for r in rows:
        try:
            doc = frappe.get_doc("CH Transfer Manifest", r.name)
            doc.check_permission("write")
            # The driver scanned the consolidated stop label, not each carton.
            # The per-manifest _validate_pickup_qr expects the manifest's own
            # qr_payload, so we hand that through and log the stop token in
            # pickup_notes for the audit trail — same pattern Ekart uses when
            # a single "hand-over scan" covers multiple AWBs on one route.
            manifest_note = audit_suffix if not notes else (notes + " | " + audit_suffix)
            doc.start_pickup(
                pickup_photo=pickup_photo,
                lat=lat,
                lng=lng,
                notes=manifest_note,
                scanned_qr=(doc.qr_payload or doc.name),
            )
            started.append(r.name)
        except frappe.ValidationError as exc:
            # Surface why a single manifest could not advance (e.g. already
            # picked up) without aborting the whole stop.
            skipped.append({"name": r.name, "reason": str(exc)})

    if started:
        # start_pickup's driver/stop cascade may save the trip — reload the
        # handle before stamping so we never save a stale doc.
        trip_doc.reload()
        stop = _get_trip_stop(trip_doc, stop.sequence)
        stop.pickup_scanned_at = now_datetime()
        stop.pickup_scanned_by = frappe.session.user
        stop.status = "Arrived"
        trip_doc.flags.ignore_mandatory = True
        trip_doc.save(ignore_permissions=True)

    return {
        "trip": trip_doc.name,
        "stop": stop.sequence,
        "started": started,
        "skipped": skipped,
    }


@frappe.whitelist()
def complete_stop_delivery(trip, sequence, scanned_qr, delivery_photo,
                           receiver_name, otp=None, lat=None, lng=None,
                           notes=None):
    """Driver scans the consolidated drop label for a whole stop.

    Validates the scanned token against CH Logistics Trip Stop.delivery_token
    and runs complete_delivery for every manifest under that stop. Receiver
    name + OTP + GPS + photo are recorded per manifest so accept_delivery
    downstream still has the data it needs.
    """
    trip_doc = frappe.get_doc("CH Logistics Trip", trip)
    trip_doc.check_permission("write")
    stop = _get_trip_stop(trip_doc, sequence)
    scanned = (scanned_qr or "").strip()
    if not scanned:
        frappe.throw(_("QR scan is mandatory to complete delivery for this stop."),
                     title=_("Scan Required"))
    expected = (stop.get("delivery_token") or "").strip()
    if not expected:
        frappe.throw(_("This stop has no delivery token yet. Re-save the trip to mint one."),
                     title=_("Token Missing"))
    if scanned != expected:
        frappe.throw(_("Scanned QR does not match the drop label for stop #{0}.").format(stop.sequence),
                     title=_("Wrong Label"))

    rows = _stop_manifest_rows(trip_doc.name, stop.sequence)
    if not rows:
        frappe.throw(_("No manifests are attached to stop #{0}.").format(stop.sequence),
                     title=_("Empty Stop"))

    delivered, skipped = [], []
    for r in rows:
        try:
            doc = frappe.get_doc("CH Transfer Manifest", r.name)
            doc.check_permission("write")
            doc.complete_delivery(
                delivery_photo=delivery_photo,
                receiver_name=receiver_name,
                otp=otp,
                lat=lat,
                lng=lng,
                # Same translation as start_stop_pickup: consolidated stop
                # token is the actual authorization; per-manifest validators
                # still want each manifest's own qr_payload.
                scanned_qr=(doc.qr_payload or doc.name),
            )
            delivered.append(r.name)
        except frappe.ValidationError as exc:
            skipped.append({"name": r.name, "reason": str(exc)})

    if delivered:
        # complete_delivery's stop-status cascade saves the trip itself, so
        # our pre-loop handle is stale — reload before stamping the stop or
        # the save dies with TimestampMismatchError.
        trip_doc.reload()
        stop = _get_trip_stop(trip_doc, stop.sequence)
        stop.delivery_scanned_at = now_datetime()
        stop.delivery_scanned_by = frappe.session.user
        stop.status = "Completed"
        if not stop.get("ata"):
            stop.ata = now_datetime()
        trip_doc.flags.ignore_mandatory = True
        trip_doc.save(ignore_permissions=True)

    return {
        "trip": trip_doc.name,
        "stop": stop.sequence,
        "delivered": delivered,
        "skipped": skipped,
    }


@frappe.whitelist()
def request_stop_otp(trip, sequence, lat=None, lng=None):
    """Mint ONE OTP for a whole consolidated stop.

    Driver-app companion to ``complete_stop_delivery``. Carrier apps that
    bundle multiple shipments under one drop (Delhivery, Ekart, Bringg)
    issue a SINGLE receiver OTP per stop, not one per AWB — otherwise the
    receiver would have to read out three codes for one handover.

    Pipeline (mirrors what the per-manifest path does, but in one shot):
      1. Every manifest at the stop must be In Transit.
      2. ``mark_reached_destination`` is called on each manifest with the
         driver's current GPS so the per-manifest arrival ping is on file
         (needed by the existing ``complete_delivery`` guard).
      3. A single 6-digit OTP is generated and written to every manifest's
         ``delivery_otp`` so that the subsequent
         ``complete_stop_delivery`` cascade validates against one shared
         secret instead of N independent ones.
      4. The OTP is dispatched via the first manifest's destination-store
         contacts — by the bundling invariant every manifest in the stop
         shares the same destination, so one notification covers them all.

    Returns masked recipients (same shape as ``request_delivery_otp``) so
    the UI can confirm where the code went without leaking PII.
    """
    from ch_logistics.api.transfer_manifest_api import (
        _send_delivery_otp,
        _require_stage_role,
    )

    _require_stage_role("complete_delivery")
    trip_doc = frappe.get_doc("CH Logistics Trip", trip)
    trip_doc.check_permission("write")
    stop = _get_trip_stop(trip_doc, sequence)

    rows = _stop_manifest_rows(trip_doc.name, stop.sequence)
    if not rows:
        frappe.throw(_("No manifests are attached to stop #{0}.").format(stop.sequence),
                     title=_("Empty Stop"))

    # Pre-flight: every manifest needs to be In Transit before OTP makes
    # sense (request_delivery_otp enforces the same rule per manifest).
    not_ready = [r.name for r in rows if r.status != "In Transit"]
    if not_ready:
        frappe.throw(
            _("Cannot request stop OTP — these manifest(s) are not In Transit yet: {0}.")
                .format(", ".join(not_ready)),
            title=_("Pickup Not Done"),
        )

    # Mint one OTP using the first manifest's generator (so the secrets
    # source and audit trail are identical to the per-manifest flow), then
    # broadcast that same OTP across every other manifest at the stop.
    first = frappe.get_doc("CH Transfer Manifest", rows[0].name)
    first.check_permission("write")
    # Step 1: arrival ping for the first manifest (so its delivery_otp can
    # be requested via the same path as the per-manifest flow).
    first.mark_reached_destination(lat=lat, lng=lng)
    first._generate_delivery_otp()
    first.flags.ignore_validate_update_after_submit = True
    first.save()
    shared_otp = first.delivery_otp

    for r in rows[1:]:
        doc = frappe.get_doc("CH Transfer Manifest", r.name)
        doc.check_permission("write")
        doc.mark_reached_destination(lat=lat, lng=lng)
        doc.delivery_otp = shared_otp
        doc.delivery_otp_verified = 0
        doc.flags.ignore_validate_update_after_submit = True
        doc.save()

    frappe.db.commit()

    # Send via the first manifest's destination — same store for all of
    # them by the bundle invariant.
    recipients = _send_delivery_otp(first) or {}

    def _mask_email(addr):
        if not addr or "@" not in addr:
            return addr
        local, _, domain = addr.partition("@")
        if len(local) <= 1:
            return f"{local[:1]}***@{domain}"
        return f"{local[:1]}***{local[-1:]}@{domain}"

    def _mask_mobile(num):
        if not num:
            return num
        s = str(num)
        if len(s) <= 4:
            return s
        return s[:2] + "*" * (len(s) - 4) + s[-2:]

    return {
        "message": _("OTP sent to the destination store. The same code unlocks every manifest at this stop."),
        "manifest_count": len(rows),
        "masked_emails": [_mask_email(e) for e in recipients.get("emails", [])],
        "masked_mobiles": [_mask_mobile(m) for m in recipients.get("mobiles", [])],
        "email_count": len(recipients.get("emails", [])),
        "sms_count": len(recipients.get("mobiles", [])),
    }


# ---------------------------------------------------------------------------
# Clubbing — one truck, one stop per destination
# ---------------------------------------------------------------------------

def _destination_key(m):
    """Group key for clubbing.

    Prefer destination store (CH Store) — that's what the driver actually
    drops at. Fall back to destination warehouse for manifests that aren't
    store-bound (e.g. inter-DC moves).
    """
    return m.get("destination_store") or m.get("destination_warehouse")


@frappe.whitelist()
def club_transfers_into_trip(source_warehouse, trip_date=None, company=None,
                             manifests=None, vehicle=None, driver=None,
                             enforce_single_destination=False):
    """Group ready-to-ship manifests from one warehouse into one trip.

    For the dispatcher who has five transfer requests like:
        10:00 Warehouse → Store 1
        10:15 Warehouse → Store 2
        10:20 Warehouse → Store 1
        10:30 Warehouse → Store 1
        10:45 Warehouse → Store 2

    Calling this once with source_warehouse=W produces:
        Trip
          Stop 1 — Store 1 (3 manifests, one pickup_token, one delivery_token)
          Stop 2 — Store 2 (2 manifests, one pickup_token, one delivery_token)

    Selection rules
    ───────────────
    * ``manifests`` (optional): explicit list of manifest names. When given,
      only those are clubbed.
    * Otherwise: every CH Transfer Manifest whose source_warehouse matches
      and whose status is Packed (the dispatcher-attachable set mirrored by
      Operations Tab — Draft manifests must be submitted first) and which is
      not yet on a trip.
    * ``trip_date`` (optional): when scanning by status, restricts to the
      given date. Defaults to today.
    * ``enforce_single_destination`` (default False): when True, refuses
      to club manifests that span multiple destination stores/warehouses
      — this is the rule the "Bundle & Print Pickup QR" UI sends so that
      one bundle = one pickup + one delivery point.
    """
    if not _has_manifest_trip_field():
        frappe.throw(_("CH Transfer Manifest is missing the 'trip' field. Run bench migrate."),
                     title=_("Schema Mismatch"))

    scope_guard.assert_scope(warehouse=source_warehouse)

    if isinstance(manifests, str):
        try:
            manifests = frappe.parse_json(manifests)
        except Exception:
            manifests = [m.strip() for m in manifests.split(",") if m.strip()]

    trip_date = trip_date or frappe.utils.today()
    company = company or frappe.defaults.get_user_default("company")
    if not company:
        frappe.throw(_("Company is required to create a trip."), title=_("API Error"))

    filters = {
        "source_warehouse": source_warehouse,
        "trip": ["in", [None, ""]],
        "docstatus": ["<", 2],
    }
    if manifests:
        filters["name"] = ["in", list(manifests)]
    else:
        filters["status"] = ["in", _OPS_ATTACHABLE_MANIFEST_STATUSES]

    fields = ["name", "destination_store", "destination_warehouse", "status"]
    pool = frappe.get_all("CH Transfer Manifest", filters=filters, fields=fields,
                          order_by="creation asc")
    if not pool:
        frappe.throw(
            _("No attachable manifests found from {0}. They must be submitted "
              "(status Packed) and not already on a trip.")
                .format(source_warehouse),
            title=_("Nothing to Club"),
        )

    # Group by destination — preserve first-seen order for deterministic stops.
    groups = {}
    order = []
    for m in pool:
        key = _destination_key(m)
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    if not groups:
        frappe.throw(_("No manifests in the pool have a destination set."),
                     title=_("Nothing to Club"))

    if enforce_single_destination and len(order) > 1:
        # Mirrors the client-side guard on the "Bundle & Print Pickup QR"
        # button: a single bundle = one pickup point + one delivery point.
        frappe.throw(
            _("Cannot bundle: selected manifests drop at {0} different destinations ({1}). "
              "For a single consolidated QR all manifests must share the same destination store.")
                .format(len(order), ", ".join(order)),
            title=_("Mixed Destinations"),
        )

    # Create the trip. System-generated Draft — driver/vehicle/planned times
    # are bound right after via assign_driver (or later by dispatch), so the
    # desk-form mandatory rule is relaxed here (matches how a TMS plans a
    # tour before carrier assignment).
    trip = frappe.new_doc("CH Logistics Trip")
    trip.trip_date = trip_date
    trip.company = company
    trip.direction = "Forward"
    trip.hub_warehouse = source_warehouse
    if vehicle:
        trip.vehicle = vehicle
    if driver:
        trip.driver = driver
    trip.insert(ignore_permissions=True, ignore_mandatory=True)

    # One stop per destination.
    for idx, key in enumerate(order, start=1):
        store_name = None
        warehouse_name = None
        # Resolve store vs warehouse — the key may be either.
        if frappe.db.exists("CH Store", key):
            store_name = key
            warehouse_name = frappe.db.get_value("CH Store", key, "warehouse")
        else:
            warehouse_name = key
        if not warehouse_name:
            # Fall back to the manifest's destination warehouse.
            warehouse_name = groups[key][0].get("destination_warehouse")

        trip.append("stops", {
            "sequence": idx,
            "warehouse": warehouse_name,
            "store": store_name,
            "stop_type": "Drop",
            "status": "Pending",
        })

    trip.save(ignore_permissions=True)

    # Attach manifests + stamp stop_sequence so existing
    # _assign_stop_sequence semantics are honoured.
    seq_by_key = {key: idx for idx, key in enumerate(order, start=1)}
    for key, rows in groups.items():
        _attach_manifests(trip.name, [r.name for r in rows])
        seq = seq_by_key[key]
        if _has_manifest_stop_seq_field():
            for r in rows:
                frappe.db.set_value("CH Transfer Manifest", r.name, "stop_sequence", seq)

    # Optional driver assignment after the trip is built.
    if driver:
        assign_driver(trip.name, driver, vehicle)

    trip.reload()
    return {
        "trip": trip.name,
        "stops": [
            {
                "sequence": s.sequence,
                "store": s.store,
                "warehouse": s.warehouse,
                "pickup_token": s.pickup_token,
                "delivery_token": s.delivery_token,
                "manifests": [m.name for m in groups[order[s.sequence - 1]]],
            }
            for s in trip.stops
        ],
    }


# ---------------------------------------------------------------------------
# Printable consolidated stop label (QR)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_stop_label(trip, sequence, kind="pickup"):
    """Return printable HTML for the consolidated stop label.

    kind:
        "pickup"   → QR encodes pickup_token
        "delivery" → QR encodes delivery_token

    Renders pure-HTML markup that the print preview can show, embed in the
    desk \"Print\" dialog, or any client-side wrapper can paste into a
    label-printer driver. The QR is a Code 128 / Data URI rendered by
    frappe.utils.qrcode so no extra dependency is required.
    """
    from frappe.utils import escape_html
    from urllib.parse import quote

    if kind not in ("pickup", "delivery"):
        frappe.throw(_("kind must be 'pickup' or 'delivery'."), title=_("API Error"))

    trip_doc = frappe.get_doc("CH Logistics Trip", trip)
    trip_doc.check_permission("read")
    stop = _get_trip_stop(trip_doc, sequence)

    token = stop.get("pickup_token") if kind == "pickup" else stop.get("delivery_token")
    if not token:
        frappe.throw(_("This stop is missing a {0} token. Re-save the trip.").format(kind),
                     title=_("Token Missing"))

    manifests = _stop_manifest_rows(trip_doc.name, stop.sequence)
    manifest_list_html = "".join(
        f"<li>{escape_html(m.name)} — {escape_html(m.status or '')}</li>" for m in manifests
    ) or "<li><i>No manifests attached yet</i></li>"

    title = _("Pickup Label") if kind == "pickup" else _("Drop Label")
    qr_src = f"/api/method/frappe.utils.print_format.print_by_server?qr_text={quote(token)}"
    # Use the public QR helper that's already bundled with frappe.
    try:
        from frappe.utils.image import generate_qrcode_dataurl
        qr_src = generate_qrcode_dataurl(token)
    except Exception:
        try:
            from frappe.utils import get_qr_code_data_url
            qr_src = get_qr_code_data_url(token)
        except Exception:
            # Final fallback: an external QR service is NOT used to avoid
            # leaking the token. We print the raw token instead so the
            # operator can re-mint a barcode label-side.
            qr_src = ""

    return {
        "trip": trip_doc.name,
        "stop": stop.sequence,
        "kind": kind,
        "token": token,
        "manifest_count": len(manifests),
        "html": f"""
            <div style=\"font-family: Arial, sans-serif; width: 384px; padding: 12px; border: 2px solid #000;\">
                <h2 style=\"margin:0 0 4px 0;\">{escape_html(title)}</h2>
                <div style=\"font-size: 12px; color:#444;\">Trip: <b>{escape_html(trip_doc.name)}</b></div>
                <div style=\"font-size: 12px; color:#444;\">Stop #{stop.sequence} — {escape_html(stop.store or stop.warehouse or '')}</div>
                <div style=\"text-align:center; margin: 12px 0;\">
                    {f'<img src=\"{qr_src}\" alt=\"QR\" style=\"width:220px;height:220px;\"/>' if qr_src else f'<pre style=\"font-size:14px;\">{escape_html(token)}</pre>'}
                </div>
                <div style=\"font-size: 11px; word-break: break-all;\">{escape_html(token)}</div>
                <hr/>
                <div style=\"font-size: 12px;\">Shipments ({len(manifests)}):</div>
                <ul style=\"font-size: 11px; margin: 4px 0 0 18px; padding: 0;\">{manifest_list_html}</ul>
            </div>
        """,
    }
