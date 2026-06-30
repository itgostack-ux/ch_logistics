import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


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
# and the trip can be Closed once every shipment is in a terminal logistics
# state (Delivered / Closed / Cancelled).
_MANIFEST_PREDELIVERY = {
    "Draft", "Packed", "Assigned", "Pickup Started", "In Transit", "Recall Initiated",
}
_MANIFEST_UNSETTLED = _MANIFEST_PREDELIVERY


class CHLogisticsTrip(Document):
    def validate(self):
        self._validate_stops()
        self._populate_hub_from_route()
        self._ensure_stop_tokens()
        self._recompute_totals()

    def before_save(self):
        self._enforce_status_transition()

    # ------------------------------------------------------------------
    def _validate_stops(self):
        if not self.stops:
            return
        seen = set()
        for row in self.stops:
            if row.sequence in seen:
                frappe.throw(_("Duplicate stop sequence {0}").format(row.sequence))
            seen.add(row.sequence)
        self.stops.sort(key=lambda r: r.sequence or 0)

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
            fields = ["name"] + (["stop_sequence"] if has_seq else [])
            rows = frappe.get_all(
                "CH Transfer Manifest",
                filters={"trip": self.name, "docstatus": ["<", 2]},
                fields=fields,
            )
            self.total_shipments = len(rows)
        # Per-stop manifest counts for dispatch visibility
        if self.stops:
            counts = {}
            for r in rows:
                seq = r.get("stop_sequence")
                if seq:
                    counts[seq] = counts.get(seq, 0) + 1
            for s in self.stops:
                s.manifest_count = counts.get(s.sequence, 0)
        # Actual duration if both timestamps exist
        if self.actual_start and self.actual_end:
            delta = (self.actual_end - self.actual_start).total_seconds() / 60.0
            self.total_duration_actual_min = int(max(delta, 0))

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
        for rs in route_stops:
            self.append("stops", {
                "sequence": rs.sequence,
                "route_stop": rs.name,
                "warehouse": rs.warehouse,
                "store": rs.store,
                "stop_type": rs.get("stop_type") or default_type,
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

    def mark_closed(self):
        _lk = f"trip_st_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 15)", (_lk,))[0][0]:
            frappe.throw(frappe._("Trip {0} is being updated. Retry.").format(self.name))
        try:
            _cur = frappe.db.get_value("CH Logistics Trip", self.name, "status")
            if _cur != "Completed":
                frappe.throw(_("Trip must be Completed before closing"))
            unsettled = self._blocking_manifests(_MANIFEST_UNSETTLED)
            if unsettled:
                frappe.throw(_(
                    "Cannot close trip {0}. These shipments are not yet delivered/closed/cancelled: {1}. "
                    "Settle each shipment first, then close the trip."
                ).format(self.name, ", ".join(unsettled)))
            self.status = "Closed"
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (_lk,))
