import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


# Status machine -----------------------------------------------------------
# Draft → Assigned → Started → Completed → Closed
#               ↘ Cancelled (from Draft / Assigned only)
_ALLOWED_STATUS_TRANSITIONS = {
    "Draft": {"Assigned", "Cancelled"},
    "Assigned": {"Started", "Draft", "Cancelled"},
    "Started": {"Completed"},
    "Completed": {"Closed"},
    "Closed": set(),
    "Cancelled": set(),
}

# Manifest statuses that block trip lifecycle transitions -------------------
# A trip carries multiple shipments (one per request/manifest). The driver can
# only mark the trip Completed once every shipment has at least been Delivered,
# and the trip can be Closed once every shipment is in a terminal settled
# state (Received / Partially Received / Closed / Cancelled / Returned).
_MANIFEST_PREDELIVERY = {
    "Draft", "Packed", "Assigned", "Pickup Started", "In Transit", "Recall Initiated",
}
# Terminal states that indicate the shipment has been reconciled and no
# further logistics action is required. Anything outside this set — including
# "Delivered" (goods handed over but not yet booked into the receiving
# warehouse) and "Rejected" (refused by the receiving location and awaiting
# a decision) — must block trip close.
_MANIFEST_SETTLED = {
    "Partially Received", "Received", "Closed", "Cancelled", "Returned",
}
_MANIFEST_UNSETTLED = (
    _MANIFEST_PREDELIVERY | {"Delivered", "Rejected"}
)
# Trip CLOSE gate (policy): a Delivered shipment is enough to close the trip.
# The driver / logistics team must NOT wait for the destination store's
# Scan & Receive — that posts stock and settles the manifest later, on the
# store's own time, and is decoupled from trip closure. Only shipments not yet
# handed over (still pre-delivery) or Rejected (awaiting a decision) block a
# trip from closing.
_MANIFEST_BLOCKS_TRIP_CLOSE = _MANIFEST_PREDELIVERY | {"Rejected"}


class CHLogisticsTrip(Document):
    def validate(self):
        self._validate_stops()
        self._validate_planned_times()
        self._populate_hub_from_route()
        self._validate_actor_scope()
        self._validate_stop_proof_fields()
        self._ensure_stop_tokens()
        self._recompute_totals()

    def before_save(self):
        self._enforce_status_transition()

    def _validate_actor_scope(self):
        from ch_logistics import roles as role_registry, scope_guard

        if role_registry.is_privileged():
            return
        previous = None if self.is_new() else self.get_doc_before_save()
        if role_registry.user_has("ops_control"):
            scope_guard.assert_scope(warehouse=self.hub_warehouse, company=self.company)
            for stop in self.stops or ():
                scope_guard.assert_scope(
                    store=stop.get("store"),
                    warehouse=stop.get("warehouse"),
                    company=self.company,
                )
            return
        from ch_logistics.api.driver_resolver import resolve_current_driver

        driver = resolve_current_driver(throw=False)
        if not driver or not previous or previous.driver != driver:
            frappe.throw(_("You can only update a trip assigned to your Driver profile."), frappe.PermissionError)
        rejected_assignment = (
            previous.status == "Assigned"
            and self.status == "Draft"
            and not self.driver
        )
        if self.driver != driver and not rejected_assignment:
            frappe.throw(_("Trip assignment fields can only be changed by dispatch."), frappe.PermissionError)
        protected = (
            "company", "trip_date", "direction", "route", "hub_warehouse",
            "vehicle", "planned_start", "planned_end", "stops",
        )
        if any(self.has_value_changed(fieldname) for fieldname in protected):
            frappe.throw(_("Trip planning fields can only be changed by dispatch."), frappe.PermissionError)

    # ------------------------------------------------------------------
    def _validate_stops(self):
        if not self.stops:
            return
        warehouse_names = {row.warehouse for row in self.stops if row.warehouse}
        warehouse_rows = {
            row.name: row
            for row in frappe.get_all(
                "Warehouse",
                filters={"name": ("in", tuple(warehouse_names))},
                fields=["name", "company"],
                limit_page_length=max(len(warehouse_names), 1),
            )
        } if warehouse_names else {}
        store_names = {row.store for row in self.stops if row.store}
        store_fields = ["name", "company"]
        store_meta = frappe.get_meta("CH Store")
        store_warehouse_fields = [
            fieldname
            for fieldname in ("warehouse", "default_warehouse")
            if store_meta.has_field(fieldname)
        ]
        store_fields.extend(store_warehouse_fields)
        store_rows = {
            row.name: row
            for row in frappe.get_all(
                "CH Store",
                filters={"name": ("in", tuple(store_names))},
                fields=store_fields,
                limit_page_length=max(len(store_names), 1),
            )
        } if store_names else {}
        seen = set()
        for row in self.stops:
            if row.sequence in seen:
                frappe.throw(_("Duplicate stop sequence {0}").format(row.sequence))
            seen.add(row.sequence)
            if not row.warehouse or row.warehouse not in warehouse_rows:
                frappe.throw(_("Stop {0} has an unknown warehouse.").format(row.sequence))
            if warehouse_rows[row.warehouse].company != self.company:
                frappe.throw(
                    _("Stop {0} warehouse belongs to another company.").format(row.sequence),
                    frappe.PermissionError,
                )
            if row.store:
                store = store_rows.get(row.store)
                if not store or store.company != self.company:
                    frappe.throw(
                        _("Stop {0} store belongs to another company.").format(row.sequence),
                        frappe.PermissionError,
                    )
                configured = {store.get(fieldname) for fieldname in store_warehouse_fields}
                if configured and row.warehouse not in configured:
                    frappe.throw(
                        _("Stop {0} store is not configured for its warehouse.").format(row.sequence),
                        frappe.PermissionError,
                    )
        self.stops.sort(key=lambda r: r.sequence or 0)

    def _validate_stop_proof_fields(self):
        """Keep QR bearers and scan evidence server-managed on direct saves."""
        from ch_logistics import roles as role_registry

        if role_registry.is_privileged() or self.flags.ignore_validate_update_after_submit:
            return
        protected = (
            "pickup_token", "delivery_token", "pickup_scanned_at",
            "delivery_scanned_at", "pickup_scanned_by", "delivery_scanned_by",
        )
        if self.is_new():
            if any(stop.get(fieldname) for stop in self.stops for fieldname in protected):
                frappe.throw(_("Stop proof fields are generated by the server."), frappe.PermissionError)
            return
        previous = self.get_doc_before_save()
        if not previous:
            return
        previous_by_name = {row.name: row for row in previous.stops or () if row.name}
        for stop in self.stops or ():
            old = previous_by_name.get(stop.name)
            if old and any(stop.get(fieldname) != old.get(fieldname) for fieldname in protected):
                frappe.throw(_("Stop proof fields are generated by the server."), frappe.PermissionError)

    def _validate_planned_times(self):
        """Enforce planned_end > planned_start.

        SAP TM / Oracle OTM both reject freight orders where planned end
        <= planned start.  Without this, trips with zero or negative
        duration slip into KPI reports and skew on-time %.  We rely on
        Frappe's ``reqd: 1`` on both fields (see trip JSON) to guarantee
        presence, so this method only compares.
        """
        if not (self.planned_start and self.planned_end):
            return
        start = get_datetime(self.planned_start)
        end = get_datetime(self.planned_end)
        if end == start:
            frappe.throw(_(
                "Planned Start and Planned End cannot be the same. "
                "Set a realistic drive-time window."
            ), title=_("Invalid Trip Window"))
        if end < start:
            frappe.throw(_(
                "Planned End ({0}) is before Planned Start ({1}). "
                "Correct the trip window."
            ).format(self.planned_end, self.planned_start),
                title=_("Invalid Trip Window"))

    def _populate_hub_from_route(self):
        if self.route and not self.hub_warehouse:
            self.hub_warehouse = frappe.db.get_value("CH Route", self.route, "hub_warehouse")

    def _ensure_stop_tokens(self):
        """Generate per-stop pickup/delivery QR tokens.

        Each stop gets its own random tokens so the packing team can print a
        single consolidated label per destination. The driver scans the
        printed QR once at the source to start pickup for every manifest
        grouped under that stop, and once at the destination to complete
        delivery for the same group. Tokens are random hashes — never derived
        from doc names — so they cannot be guessed.
        """
        if not self.stops:
            return
        for stop in self.stops:
            if not stop.get("pickup_token"):
                stop.pickup_token = frappe.generate_hash(length=22)
            if not stop.get("delivery_token"):
                stop.delivery_token = frappe.generate_hash(length=22)

    def _recompute_totals(self):
        # Total shipments = manifests linked to this trip (resolved on save).
        rows = []
        if self.is_new() or not self.name:
            self.total_shipments = 0
        elif not frappe.db.has_column("CH Transfer Manifest", "trip"):
            self.total_shipments = 0
        else:
            has_seq = frappe.db.has_column("CH Transfer Manifest", "stop_sequence")
            fields = [
                "name", "source_store", "source_warehouse",
                "destination_store", "destination_warehouse",
            ] + (["stop_sequence"] if has_seq else [])
            rows = frappe.get_all(
                "CH Transfer Manifest",
                filters={"trip": self.name, "docstatus": ["<", 2]},
                fields=fields,
            )
            self.total_shipments = len(rows)
        self._reconcile_stops(rows)
        # Actual duration if both timestamps exist
        if self.actual_start and self.actual_end:
            delta = (self.actual_end - self.actual_start).total_seconds() / 60.0
            self.total_duration_actual_min = int(max(delta, 0))

    def _reconcile_stops(self, manifests):
        """Derive each stop's type and manifest count from the shipments on it.

        A CH Route is a planning template; the executable itinerary belongs to
        the trip. Copying the template's stop types verbatim let a stop sit at a
        manifest's ORIGIN while declaring itself a Drop, which made the pickup
        unreachable. Oracle OTM and SAP TM both derive the stop's role from the
        shipment events that occur there, and that is what happens here: the
        declared type is replaced by the union of the roles the attached
        manifests actually imply.

        Counting is role-based too. The old count keyed off ``stop_sequence``
        alone, so a manifest was only ever counted at one end of its journey and
        pickup stops reported zero shipments.

        Stops that serve no shipment keep their declared type and report a count
        of zero rather than being deleted — a dispatcher may have placed a
        waypoint deliberately, and silently dropping planned rows would be worse
        than showing an honest empty stop.
        """
        if not self.stops:
            return
        from ch_logistics.api import stop_roles

        wh_by_store = stop_roles.warehouse_by_store(self.stops, manifests)
        for s in self.stops:
            roles = set()
            count = 0
            for m in manifests or ():
                m_roles = stop_roles.manifest_roles_at_stop(m, s, wh_by_store)
                if not m_roles and m.get("stop_sequence") and m["stop_sequence"] == s.sequence:
                    m_roles = {stop_roles.DROP}
                if m_roles:
                    roles |= m_roles
                    count += 1
            s.manifest_count = count
            derived = stop_roles.combine_roles(roles)
            if derived and derived != s.stop_type:
                s.stop_type = derived

    def _blocking_manifests(self, blocking_statuses):
        """Names of submitted/draft manifests on this trip whose status is in
        ``blocking_statuses`` (i.e. not yet advanced far enough)."""
        if self.is_new() or not self.name:
            return []
        if not frappe.db.has_column("CH Transfer Manifest", "trip"):
            return []
        rows = frappe.get_all(
            "CH Transfer Manifest",
            filters={"trip": self.name, "docstatus": ["<", 2]},
            fields=["name", "status"],
        )
        return [r.name for r in rows if (r.status or "Draft") in blocking_statuses]

    def _enforce_status_transition(self):
        if self.is_new():
            return
        previous = self.get_doc_before_save()
        if not previous or previous.status == self.status:
            return
        if (
            previous.status == "Started"
            and self.status == "Cancelled"
            and self.flags.get("cancel_after_recall")
        ):
            return
        allowed = _ALLOWED_STATUS_TRANSITIONS.get(previous.status, set())
        if self.status not in allowed:
            frappe.throw(
                _("Cannot transition Trip status from {0} to {1}").format(previous.status, self.status)
            )

    # ------------------------------------------------------------------
    # Public helpers used by logistics_api
    # ------------------------------------------------------------------
    def populate_stops_from_route(self):
        if not self.route:
            frappe.throw(_("Set a Route before populating stops"))
        if self.stops:
            frappe.throw(_("Stops already exist; clear them first"))
        has_stop_type = frappe.db.has_column("CH Route Stop", "stop_type")
        route_stops = frappe.get_all(
            "CH Route Stop",
            filters={"parent": self.route, "parenttype": "CH Route"},
            fields=["name", "sequence", "warehouse", "store"] + (["stop_type"] if has_stop_type else []),
            order_by="sequence asc",
        )
        default_type = "Pickup" if self.direction == "Reverse" else "Drop"
        hub_warehouse = frappe.db.get_value("CH Route", self.route, "hub_warehouse")
        for rs in route_stops:
            stop_type = rs.get("stop_type")
            if not stop_type:
                # On a Forward trip the hub is where the load is picked up —
                # typing it "Drop" (the plain default) breaks the driver
                # app's Arrive & Pick Up flow at that stop.
                if (
                    self.direction != "Reverse"
                    and hub_warehouse
                    and rs.warehouse == hub_warehouse
                ):
                    stop_type = "Pickup"
                else:
                    stop_type = default_type
            self.append("stops", {
                "sequence": rs.sequence,
                "route_stop": rs.name,
                "warehouse": rs.warehouse,
                "store": rs.store,
                "stop_type": stop_type,
                "status": "Pending",
            })

    def mark_started(self):
        _lk = f"trip_st_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 15)", (_lk,))[0][0]:
            frappe.throw(frappe._("Trip {0} is being updated. Retry.").format(self.name))
        try:
            _cur = frappe.db.get_value("CH Logistics Trip", self.name, "status")
            if _cur != "Assigned":
                frappe.throw(_("Trip must be Assigned before starting"))
            self.status = "Started"
            self.actual_start = now_datetime()
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (_lk,))

    def mark_completed(self):
        _lk = f"trip_st_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 15)", (_lk,))[0][0]:
            frappe.throw(frappe._("Trip {0} is being updated. Retry.").format(self.name))
        try:
            _cur = frappe.db.get_value("CH Logistics Trip", self.name, "status")
            if _cur != "Started":
                frappe.throw(_("Trip must be Started before completing"))
            pending = self._blocking_manifests(_MANIFEST_PREDELIVERY)
            if pending:
                frappe.throw(_(
                    "Cannot complete trip {0}. These shipments are not yet delivered: {1}. "
                    "Deliver each shipment first, then complete the trip."
                ).format(self.name, ", ".join(pending)))
            self.status = "Completed"
            self.actual_end = now_datetime()
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (_lk,))

    def mark_cancelled_after_recall(self):
        """Finalize an aborted trip after every manifest is back or cancelled."""
        _lk = f"trip_st_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 15)", (_lk,))[0][0]:
            frappe.throw(frappe._("Trip {0} is being updated. Retry.").format(self.name))
        try:
            current = frappe.db.get_value("CH Logistics Trip", self.name, "status")
            if current not in ("Assigned", "Started"):
                frappe.throw(
                    _("A recalled trip can be cancelled only from Assigned or Started.")
                )
            rows = frappe.get_all(
                "CH Transfer Manifest",
                filters={"trip": self.name, "docstatus": ["<", 2]},
                fields=["name", "status"],
            )
            blocking = [
                row.name
                for row in rows
                if (row.status or "Draft") not in ("Returned", "Cancelled")
            ]
            if blocking:
                frappe.throw(
                    _(
                        "Trip {0} cannot be cancelled yet. These manifests have "
                        "not been returned: {1}"
                    ).format(self.name, ", ".join(blocking))
                )
            self.flags.cancel_after_recall = True
            self.status = "Cancelled"
            self.cancelled_by = frappe.session.user
            self.cancelled_on = now_datetime()
            self.actual_end = self.actual_end or now_datetime()
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (_lk,))

    def mark_closed(self):
        _lk = f"trip_st_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 15)", (_lk,))[0][0]:
            frappe.throw(frappe._("Trip {0} is being updated. Retry.").format(self.name))
        try:
            _cur = frappe.db.get_value("CH Logistics Trip", self.name, "status")
            if _cur != "Completed":
                frappe.throw(_("Trip must be Completed before closing"))
            unsettled = self._blocking_manifests(_MANIFEST_BLOCKS_TRIP_CLOSE)
            if unsettled:
                frappe.throw(_(
                    "Cannot close trip {0} yet — these shipments have not been delivered: {1}. "
                    "Deliver them (or resolve any rejected shipment) first. Delivered "
                    "shipments do not block closing — the destination store receives them "
                    "separately, after the trip is closed."
                ).format(self.name, ", ".join(unsettled)))
            self.status = "Closed"
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (_lk,))
