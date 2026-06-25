"""CH Transfer Manifest controller.

Lifecycle:
    Draft → Packed → Assigned → Pickup Started → In Transit → Delivered → Received → Closed
"""

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, cint, flt, getdate, nowdate


class CHTransferManifest(Document):

    def validate(self):
        if not self.status:
            self.status = "Draft"
        self._populate_transfer_details()
        self._compute_totals()
        self._validate_transfers()

    def before_submit(self):
        if not self.transfers:
            frappe.throw(_("Add at least one Stock Entry to the manifest."), title=_("Ch Transfer Manifest Error"))
        # Trip linking is optional at submit time — manifests can be submitted
        # standalone and attached to a trip via the Logistics Control Tower.
        if self.status in ("Draft", None, ""):
            self.status = "Packed"

    def on_submit(self):
        self._update_stock_entries_manifest()

    def on_cancel(self):
        if self.status in ("In Transit", "Delivered", "Received", "Partially Received"):
            frappe.throw(
                frappe._("Manifest {0} is {1}. Use 'Initiate Recall' to return goods first.").format(self.name, self.status),
                title=frappe._("Cannot Cancel In-Transit Manifest")
            )
        self.db_set("status", "Cancelled")
        self._clear_stock_entries_manifest()

    # ── Helpers ─────────────────────────────────────────────────────────

    def _populate_transfer_details(self):
        """Auto-fill warehouse, item count, MR link from each Stock Entry."""
        for row in self.transfers:
            if not row.stock_entry:
                continue
            se = frappe.db.get_value(
                "Stock Entry", row.stock_entry,
                ["from_warehouse", "to_warehouse",
                 "docstatus", "stock_entry_type"],
                as_dict=True,
            )
            if not se:
                frappe.throw(_("Stock Entry {0} not found.").format(row.stock_entry), title=_("Ch Transfer Manifest Error"))
            if se.docstatus == 2:
                frappe.throw(_("Stock Entry {0} is cancelled.").format(row.stock_entry), title=_("Ch Transfer Manifest Error"))
            if se.stock_entry_type != "Material Transfer":
                frappe.throw(
                    _("Stock Entry {0} is not a Material Transfer (type: {1}).").format(
                        row.stock_entry, se.stock_entry_type
                    )
                )
            row.from_warehouse = se.from_warehouse
            row.to_warehouse = se.to_warehouse
            # Get material_request from items (it's on the child table)
            row.material_request = frappe.db.get_value(
                "Stock Entry Detail",
                {"parent": row.stock_entry, "material_request": ("is", "set")},
                "material_request",
            ) or ""
            row.transfer_status = frappe.db.get_value(
                "Stock Entry", row.stock_entry, "custom_status"
            ) or "Draft"

            items = frappe.db.sql(
                """SELECT COUNT(*) as cnt, SUM(IFNULL(qty,0)) as total_qty
                   FROM `tabStock Entry Detail` WHERE parent=%s""",
                row.stock_entry, as_dict=True,
            )
            row.item_count = cint(items[0].cnt) if items else 0
            row.total_qty = flt(items[0].total_qty) if items else 0

    def _compute_totals(self):
        self.total_stock_entries = len(self.transfers)
        self.total_items = sum(cint(r.item_count) for r in self.transfers)
        self.total_qty = sum(flt(r.total_qty) for r in self.transfers)
        self._compute_freight()

    def _validate_transfers(self):
        """Ensure no Stock Entry is already on another active manifest."""
        seen = set()
        for row in self.transfers:
            if row.stock_entry in seen:
                frappe.throw(_("Duplicate Stock Entry {0} in manifest.").format(row.stock_entry), title=_("Ch Transfer Manifest Error"))
            seen.add(row.stock_entry)

            existing = frappe.db.get_value(
                "CH Transfer Manifest Item",
                {
                    "stock_entry": row.stock_entry,
                    "parent": ("!=", self.name or ""),
                    "parenttype": "CH Transfer Manifest",
                },
                ["parent"],
            )
            if existing:
                parent_status = frappe.db.get_value("CH Transfer Manifest", existing, "docstatus")
                if parent_status == 1:
                    frappe.throw(
                        _("Stock Entry {0} is already in active manifest {1}.").format(
                            row.stock_entry, existing
                        )
                    )

    def _update_stock_entries_manifest(self):
        """Link Stock Entries back to this manifest via custom field."""
        for row in self.transfers:
            frappe.db.set_value(
                "Stock Entry", row.stock_entry,
                "custom_transfer_manifest", self.name,
                update_modified=False,
            )

    def _clear_stock_entries_manifest(self):
        for row in self.transfers:
            frappe.db.set_value(
                "Stock Entry", row.stock_entry,
                "custom_transfer_manifest", "",
                update_modified=False,
            )

    # ── Status Transitions (called from API) ───────────────────────────

    def assign_driver(self, driver, courier_partner=None, vehicle_number=None,
                      tracking_number=None, estimated_delivery_date=None,
                      vehicle=None, external_booking_id=None):
        # Pre-lock input validation — fail fast before touching DB state.
        if not driver:
            frappe.throw(_("Driver is mandatory to assign a manifest."),
                         title=_("Driver Required"))

        lock_key = f"manifest_status_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(frappe._("Manifest {0} is being updated by another user. Please refresh and try again.").format(self.name))
        try:
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status

            if self.status not in ("Packed",):
                frappe.throw(_("Can only assign driver when status is Packed."), title=_("Ch Transfer Manifest Error"))
            self.driver = driver
            self.driver_name = frappe.db.get_value("Driver", driver, "full_name")
            self.driver_phone = frappe.db.get_value("Driver", driver, "cell_number")

            # GST Rule 138 + transporter SOP: driver phone is mandatory (transporter
            # contact recorded against EWB Part-B and used by destination for ETA calls).
            if not self.driver_phone:
                frappe.throw(
                    _("Driver {0} has no phone number on record. Update the Driver master before assignment.").format(driver),
                    title=_("Driver Phone Required"),
                )

            # Auto-pull partner type + courier_partner from driver profile if not supplied
            if not courier_partner:
                courier_partner = frappe.db.get_value("Driver", driver, "custom_courier_partner")
            self.courier_partner = courier_partner or self.courier_partner

            # Vehicle (custom field) — fall back to driver default
            if not vehicle:
                vehicle = frappe.db.get_value("Driver", driver, "custom_default_vehicle")
            if vehicle:
                self.custom_vehicle = vehicle
                if not vehicle_number:
                    vehicle_number = frappe.db.get_value("Vehicle", vehicle, "license_plate")
                # Capacity warning (non-blocking — Dunzo/3PL drivers may not register vehicles)
                self._check_vehicle_capacity(vehicle)

            # Normalise: NIC e-Way Bill API rejects spaces and lowercase plates.
            resolved_vehicle = (vehicle_number or self.vehicle_number or "").strip().upper().replace(" ", "")
            self.vehicle_number = resolved_vehicle

            # GST Rule 138, Part-B: vehicle number is mandatory before goods move.
            # Without Part-B, the EWB is not valid for transit and goods are liable to
            # detention / penalty. Block assignment outright.
            if not self.vehicle_number:
                frappe.throw(
                    _("Vehicle Number is mandatory before assigning a driver. "
                      "Required for Part-B of the e-Way Bill (GST Rule 138)."),
                    title=_("Vehicle Number Required"),
                )

            self.tracking_number = tracking_number or self.tracking_number
            self.estimated_delivery_date = estimated_delivery_date
            if external_booking_id:
                self.custom_external_booking_id = external_booking_id

            # Snapshot planned weight for later variance reporting
            try:
                planned = sum(flt(p.weight_kg) for p in (self.packages or []))
                self.custom_total_weight_kg_planned = planned
            except Exception:
                pass

            self.status = "Assigned"
            self._generate_delivery_otp()
            # Seed the pickup-scan token so QR enforcement has something to
            # match against (older manifests are backfilled lazily here).
            if not self.qr_payload:
                self.qr_payload = self.name
            # Issue the public track-and-trace token once, at assignment.
            if not self.get("tracking_token"):
                from ch_logistics.api.customer_tracking import ensure_token
                ensure_token(self)
            self.flags.ignore_validate_update_after_submit = True
            self.save()

            # GST e-Way Bill: now that driver + vehicle are confirmed, generate
            # (or Part-B-update) the EWB for every Stock Entry on this manifest
            # so the driver leaves with a fully valid printout in hand.
            # Wrapped so EWB API failures do NOT block the assignment itself
            # — the manifest status_change must succeed; EWB issues are logged
            # for HO Admin to retry via the "Refresh e-Way Bills" button.
            try:
                self._sync_ewaybills_for_transfers()
            except Exception:
                frappe.log_error(
                    title=f"EWB sync failed on assign_driver {self.name}",
                    message=frappe.get_traceback(),
                )

            # Phase 5: stamp SLA target now that the clock is running
            try:
                from ch_erp15.ch_erp15.sla_engine import set_manifest_sla
                set_manifest_sla(self)
                self.reload()
            except Exception:
                frappe.log_error(
                    title=f"set_manifest_sla failed for {self.name}",
                    message=frappe.get_traceback(),
                )
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    # ── e-Way Bill orchestration ───────────────────────────────────────

    def _sync_ewaybills_for_transfers(self):
        """Generate (or Part-B-update) GST e-Way Bills for every Stock Entry
        on this manifest.

        GST Rule 138 + India Compliance reality:
          * One e-Way Bill per consignment (= per Stock Entry). India Compliance
            does not expose the Consolidated EWB (CEWB) API, so the driver
            carries one printout per Stock Entry — bundled into a single print
            job by the "Print e-Way Bills" button on this manifest.
          * Part-A (invoice + parties + items + value) and Part-B (vehicle +
            transporter) are submitted in a single call here, because both are
            known the moment the driver is assigned.
          * If an EWB already exists (e.g. raised earlier as Part-A only),
            we push the vehicle update instead of regenerating.

        Side effects:
          * Stamps ``vehicle_no``, ``lr_no``, ``lr_date``, ``mode_of_transport``,
            ``gst_vehicle_type`` on each linked Stock Entry.
          * Enqueues ``generate_e_waybill`` or ``update_vehicle_info`` jobs
            (queue=short, after_commit=True) — failures are logged, not raised.
          * Sets ``ewaybill_status`` on this manifest to one of
            Not Required / Generating / Generated / Partial / Failed.

        Safe to re-run (idempotent on Stock Entries that already have an EWB —
        a Part-B update is the cheapest no-op-ish operation against NIC).
        """
        # India Compliance not installed → no EWB anywhere on this stack.
        try:
            from india_compliance.gst_india.utils.e_waybill import (  # noqa: F401
                generate_e_waybill,
                update_vehicle_info,
            )
        except ImportError:
            self.db_set("ewaybill_status", "Not Required", update_modified=False)
            return

        settings = frappe.get_cached_doc("GST Settings")
        if not (settings.enable_e_waybill and settings.enable_api):
            self.db_set("ewaybill_status", "Not Required", update_modified=False)
            return

        rows = self.transfers or []
        if not rows:
            self.db_set("ewaybill_status", "Not Required", update_modified=False)
            return

        vehicle_no = (self.vehicle_number or "").strip().upper().replace(" ", "")
        vehicle_values = {
            "vehicle_no": vehicle_no,
            # NIC limits LR no. to 15 chars on some shapes; manifest names fit.
            "lr_no": self.name[:30],
            "lr_date": str(getdate(nowdate())),
            "mode_of_transport": "Road",
            "gst_vehicle_type": "Regular",
        }

        enqueued_new = 0
        enqueued_update = 0
        skipped = 0
        skipped_reasons = []

        for row in rows:
            se_name = getattr(row, "stock_entry", None)
            if not se_name:
                continue
            se = frappe.db.get_value(
                "Stock Entry",
                se_name,
                ["docstatus", "ewaybill", "bill_from_address", "bill_to_address"],
                as_dict=True,
            )
            if not se:
                skipped += 1
                continue
            if se.docstatus != 1:
                skipped += 1
                skipped_reasons.append(f"{se_name}: not submitted")
                continue
            if not (se.bill_from_address and se.bill_to_address):
                skipped += 1
                skipped_reasons.append(f"{se_name}: missing bill_from/bill_to address")
                continue

            # Stamp Part-B fields onto SE regardless of branch — both
            # generate_e_waybill and update_vehicle_info read them.
            frappe.db.set_value(
                "Stock Entry",
                se_name,
                {
                    "vehicle_no": vehicle_values["vehicle_no"],
                    "lr_no": vehicle_values["lr_no"],
                    "lr_date": vehicle_values["lr_date"],
                    "mode_of_transport": vehicle_values["mode_of_transport"],
                    "gst_vehicle_type": vehicle_values["gst_vehicle_type"],
                },
                update_modified=False,
            )

            if se.ewaybill:
                # Existing EWB — push vehicle/driver as a Part-B update.
                frappe.enqueue(
                    "india_compliance.gst_india.utils.e_waybill.update_vehicle_info",
                    enqueue_after_commit=True,
                    queue="short",
                    doctype="Stock Entry",
                    docname=se_name,
                    values=vehicle_values,
                )
                enqueued_update += 1
            else:
                # No EWB yet — generate fresh (Part-A + Part-B in one call).
                frappe.enqueue(
                    "india_compliance.gst_india.utils.e_waybill.generate_e_waybill",
                    enqueue_after_commit=True,
                    queue="short",
                    doctype="Stock Entry",
                    docname=se_name,
                )
                enqueued_new += 1

        total_enqueued = enqueued_new + enqueued_update
        if total_enqueued == 0 and skipped == len(rows):
            status = "Failed"
        elif total_enqueued and skipped:
            status = "Generating"  # partial-set; flip to Partial/Generated on refresh
        elif total_enqueued:
            status = "Generating"
        else:
            status = "Not Generated"

        self.db_set(
            {
                "ewaybill_status": status,
                "ewaybill_count": total_enqueued,
                "ewaybill_last_synced_at": now_datetime(),
            },
            update_modified=False,
        )
        if skipped_reasons:
            frappe.log_error(
                title=f"EWB sync — skipped Stock Entries on {self.name}",
                message="\n".join(skipped_reasons),
            )

    def refresh_ewaybill_summary(self):
        """Walk each linked Stock Entry, refresh the cached EWB summary +
        status counter on this manifest, and return a structured list for
        the client (print modal, dashboards, etc.).

        Returns:
            list[dict] with keys: stock_entry, ewaybill, ewaybill_validity, status
        """
        rows = self.transfers or []
        if not rows:
            return []

        results = []
        generated = 0
        for row in rows:
            se_name = getattr(row, "stock_entry", None)
            if not se_name:
                continue
            data = frappe.db.get_value(
                "Stock Entry",
                se_name,
                ["ewaybill", "vehicle_no"],
                as_dict=True,
            ) or {}
            ewb_no = data.get("ewaybill")
            validity = None
            ewb_status = "Pending"
            if ewb_no:
                generated += 1
                ewb_status = "Generated"
                ewb_doc = frappe.db.get_value(
                    "e-Waybill Log",
                    {"name": ewb_no},
                    ["valid_upto", "status"],
                    as_dict=True,
                ) or {}
                validity = ewb_doc.get("valid_upto")
                if ewb_doc.get("status"):
                    ewb_status = ewb_doc["status"]
            results.append({
                "stock_entry": se_name,
                "ewaybill": ewb_no,
                "ewaybill_validity": validity,
                "status": ewb_status,
                "vehicle_no": data.get("vehicle_no"),
            })

        total = len(results)
        if generated == 0:
            status = "Not Generated"
        elif generated == total:
            status = "Generated"
        else:
            status = "Partial"

        # Human-readable cached summary for the form field.
        lines = []
        for r in results:
            if r["ewaybill"]:
                v = f" (valid till {r['ewaybill_validity']})" if r["ewaybill_validity"] else ""
                lines.append(f"{r['stock_entry']} → EWB {r['ewaybill']}{v}")
            else:
                lines.append(f"{r['stock_entry']} → (pending)")

        self.db_set(
            {
                "ewaybill_status": status,
                "ewaybill_count": generated,
                "ewaybill_summary": "\n".join(lines),
                "ewaybill_last_synced_at": now_datetime(),
            },
            update_modified=False,
        )
        return results

    def _check_vehicle_capacity(self, vehicle):
        """Emit non-blocking warning if planned package weight exceeds vehicle capacity."""
        capacity = flt(frappe.db.get_value("Vehicle", vehicle, "custom_capacity_kg"))
        if capacity <= 0:
            return
        try:
            total_weight = sum(flt(p.weight_kg) for p in (self.packages or []))
        except Exception:
            return
        if total_weight > capacity:
            frappe.msgprint(
                _("Planned weight {0} kg exceeds vehicle capacity {1} kg for {2}.").format(
                    total_weight, capacity, vehicle,
                ),
                indicator="orange",
                alert=True,
            )

    def start_pickup(self, pickup_photo, lat=None, lng=None, notes=None, scanned_qr=None):
        lock_key = f"manifest_status_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(frappe._("Manifest {0} is being updated by another user. Please refresh and try again.").format(self.name))
        try:
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status

            if self.status not in ("Assigned",):
                frappe.throw(_("Can only start pickup when status is Assigned."), title=_("Ch Transfer Manifest Error"))
            # FR-014/015/017/021: mandatory QR scan validation before pickup.
            self._validate_pickup_qr(scanned_qr)
            if not pickup_photo:
                frappe.throw(_("Pickup photo is mandatory."), title=_("Ch Transfer Manifest Error"))
            # Mandatory driver GPS at pickup location (proof of presence).
            lat_f, lng_f = self._validate_geo(lat, lng, kind="pickup")
            self.pickup_photo = pickup_photo
            self.pickup_datetime = now_datetime()
            self.pickup_lat = lat_f
            self.pickup_lng = lng_f
            self.pickup_notes = notes
            # Reset any prior arrival capture so a re-picked manifest forces a
            # fresh "Reached Location" tap before delivery can be completed.
            if frappe.get_meta(self.doctype).has_field("arrival_datetime"):
                self.arrival_datetime = None
                self.arrival_lat = None
                self.arrival_lng = None
            self.status = "In Transit"
            self.flags.ignore_validate_update_after_submit = True
            self.save()
            self._sync_logistics_status_to_entries("In Transit")
            # Lifecycle: this manifest is now physically being moved, so the
            # driver must show as IN_TRANSIT regardless of whether the parent
            # trip's trip_start API was called explicitly. Carrier driver apps
            # (Delhivery / BlueDart / Ekart) all drive duty status from the
            # first manifest pickup, not from a separate "trip start" button.
            self._sync_driver_state_after_action(target_hint="In Transit")
            # Proactive "out for delivery" to the destination store + track link.
            from ch_logistics.api.customer_tracking import notify_destination
            notify_destination(self.name, "out_for_delivery")
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def _validate_pickup_qr(self, scanned_qr):
        """Enforce the mandatory pickup scan (Ekart/Delhivery: every shipment is
        scanned at handover). The scanned payload must match this manifest's
        ``qr_payload`` token (or, for legacy rows, the manifest name)."""
        enforce = frappe.db.get_single_value("CH Logistics Settings", "enforce_pickup_qr")
        if enforce is not None and not int(enforce):
            return
        expected = (self.qr_payload or self.name or "").strip()
        scanned = (scanned_qr or "").strip()
        if not scanned:
            frappe.throw(_("QR scan is mandatory. Scan the manifest/order QR to start pickup."),
                         title=_("Scan Required"))
        if scanned != expected:
            frappe.throw(_("Scanned QR does not match this manifest. Expected {0}.").format(expected),
                         title=_("Wrong QR"))

    def _validate_delivery_qr(self, scanned_qr):
        """Enforce the mandatory delivery scan (same handover ritual as pickup,
        on the receiver side). Gated by ``enforce_delivery_qr`` so it can be
        relaxed for last-mile B2C lanes that don't carry a returnable QR."""
        enforce = frappe.db.get_single_value("CH Logistics Settings", "enforce_delivery_qr")
        # Default ON when the flag has never been set (matches JSON default=1).
        if enforce is not None and not int(enforce):
            return
        expected = (self.qr_payload or self.name or "").strip()
        scanned = (scanned_qr or "").strip()
        if not scanned:
            frappe.throw(_("QR scan is mandatory. Scan the manifest/order QR to complete delivery."),
                         title=_("Scan Required"))
        if scanned != expected:
            frappe.throw(_("Scanned QR does not match this manifest. Expected {0}.").format(expected),
                         title=_("Wrong QR"))

    def _validate_geo(self, lat, lng, kind: str):
        """Mandatory driver-location proof for pickup/delivery.

        Treats null / blank / non-numeric / sentinel (0, 0) / out-of-bounds
        coordinates as a missing capture and throws. The (0, 0) sentinel is
        what the driver app emits when the browser/device denies geolocation,
        so accepting it would defeat the proof-of-presence requirement.

        Returns the parsed (lat, lng) floats so callers can store them.
        """
        labels = {"pickup": _("pickup"), "arrival": _("arrival at destination")}
        label = labels.get(kind, _("delivery"))
        try:
            lat_f = float(lat) if lat not in (None, "") else None
            lng_f = float(lng) if lng not in (None, "") else None
        except (TypeError, ValueError):
            lat_f = lng_f = None
        if lat_f is None or lng_f is None:
            frappe.throw(_("Driver location (latitude & longitude) is mandatory at {0}. "
                           "Enable location on the device and retry.").format(label),
                         title=_("Location Required"))
        if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lng_f <= 180.0):
            frappe.throw(_("Driver location for {0} is out of range "
                           "(lat {1}, lng {2}).").format(label, lat_f, lng_f),
                         title=_("Invalid Location"))
        if lat_f == 0.0 and lng_f == 0.0:
            frappe.throw(_("Driver location for {0} could not be captured "
                           "(GPS returned 0, 0). Enable location on the device and retry.").format(label),
                         title=_("Location Required"))
        return lat_f, lng_f

    def _sync_driver_state_after_action(self, target_hint: str | None = None):
        """Reconcile the driver's operational status after a manifest action.

        Called from ``start_pickup`` (target_hint='In Transit'),
        ``complete_delivery`` and ``reject_manifest`` so the duty-status
        machine in ``ch_logistics.api.driver_status`` always reflects what
        the driver is actually doing right now, even when the trip-level
        ``trip_start`` / ``trip_complete`` APIs are bypassed.

        Logic mirrors the dispatch model used by Delhivery / BlueDart /
        Ekart driver apps:

        * Pickup-start  → drop the driver to IN_TRANSIT immediately.
        * Delivered / Rejected → if any other manifest is still Assigned /
          Pickup Started / In Transit on this driver, stay IN_TRANSIT;
          otherwise reset to AVAILABLE so dispatch can pick them up for
          the next trip. The trip-level ``current_trip`` link is cleared
          only when the driver fully unloads.

        Best-effort and silent if the driver-status fields aren't installed
        or the manifest carries no driver — the delivery flow must never
        break because the duty machine has a problem.
        """
        driver = self.get("driver")
        if not driver:
            return
        try:
            from ch_logistics.api import driver_status as ds
        except Exception:
            return
        try:
            if target_hint == "In Transit":
                ds.set_status(driver, ds.IN_TRANSIT,
                              current_trip=self.get("trip") or None,
                              force=True)
                return

            # Delivered / Rejected: look at every other manifest this driver
            # is still carrying. A driver who still has Assigned / Pickup
            # Started / In Transit work stays busy; a driver with no
            # outstanding work drops to AVAILABLE.
            still_busy = frappe.db.count(
                "CH Transfer Manifest",
                filters={
                    "driver": driver,
                    "status": ["in", ["Assigned", "Pickup Started", "In Transit"]],
                    "docstatus": ["<", 2],
                    "name": ["!=", self.name],
                },
            )
            if still_busy:
                # Don't downgrade an already-IN_TRANSIT driver to ASSIGNED.
                current = ds.get_status(driver)
                if current != ds.IN_TRANSIT:
                    ds.set_status(driver, ds.ASSIGNED,
                                  current_trip=self.get("trip") or None,
                                  force=True)
            else:
                # Clear current_trip too — driver is fully unloaded.
                ds.set_status(driver, ds.AVAILABLE,
                              current_trip=None, force=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"driver-state sync failed after action on {self.name}",
            )

    def reject_manifest(self, rejection_reason, rejection_photo, rejection_notes=None):
        """Driver rejects a manifest that cannot be completed.

        Two rejection paths are supported — they share this entry point but
        differ in stock handling:

        * **Pickup-time rejection** (status in Assigned / Pickup Started) —
          goods never left the source warehouse, so child Stock Entries
          revert to ``Pending Pickup`` and the manifest is set to
          ``Rejected``. Standard carrier vocabulary: pickup failure.

        * **In-transit rejection** (status In Transit) — goods are with the
          driver and physically cannot stay with the receiver (Customer
          Not Available / Address Not Found / Receiver Refused / Damaged in
          Transit / Vehicle Breakdown). Child Stock Entries flip to
          ``Return to Source`` so ops knows the load is coming back, and a
          trip-level CH Logistics Exception is auto-raised so the control
          tower sees the failed delivery attempt the same way Delhivery /
          BlueDart / FedEx surface \"Delivery Exception\".

        Reason + proof photo are mandatory in both paths."""
        lock_key = f"manifest_status_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]:
            frappe.throw(_("Manifest {0} is being updated by another user. Please refresh and try again.").format(self.name))
        try:
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status
            if self.status not in ("Assigned", "Pickup Started", "In Transit"):
                frappe.throw(_("Can only reject a manifest before delivery is completed (current: {0}).").format(self.status),
                             title=_("Ch Transfer Manifest Error"))
            during = "In Transit" if self.status == "In Transit" else "Pickup"
            pickup_reasons = {"Material Not Ready", "Wrong Package", "Store Closed",
                              "Damaged Package", "Other"}
            in_transit_reasons = {"Customer Not Available", "Address Not Found",
                                  "Receiver Refused", "Damaged in Transit",
                                  "Vehicle Breakdown", "Other"}
            valid_reasons = in_transit_reasons if during == "In Transit" else pickup_reasons
            if rejection_reason not in valid_reasons:
                frappe.throw(_("'{0}' is not a valid rejection reason at {1}. Allowed: {2}.").format(
                                 rejection_reason, during, ", ".join(sorted(valid_reasons))),
                             title=_("Ch Transfer Manifest Error"))
            if not rejection_photo:
                frappe.throw(_("Rejection proof photo is mandatory."), title=_("Ch Transfer Manifest Error"))

            self.rejection_reason = rejection_reason
            self.rejection_photo = rejection_photo
            self.rejection_notes = rejection_notes
            self.rejected_by = frappe.session.user
            self.rejected_at = now_datetime()
            if frappe.get_meta(self.doctype).has_field("rejected_during"):
                self.rejected_during = during
            self.status = "Rejected"
            self.flags.ignore_validate_update_after_submit = True
            self.save()
            if during == "In Transit":
                # Goods are physically with the driver — they need to come
                # back to source. Source store will receive them as a return.
                self._sync_logistics_status_to_entries("Return to Source")
                self._raise_trip_exception_for_rejection(rejection_reason, rejection_notes)
            else:
                # Nothing was picked up — child entries go back to the queue.
                self._sync_logistics_status_to_entries("Pending Pickup")
            # Lifecycle: a rejection releases this manifest from the driver's
            # workload exactly like a Delivered does. Recompute residual state
            # so a driver who rejected their last Assigned manifest goes back
            # to AVAILABLE and is eligible for the next dispatch.
            self._sync_driver_state_after_action()
            self._notify_dispatcher_rejection()
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def _raise_trip_exception_for_rejection(self, reason, notes):
        """Surface an in-transit rejection on the parent trip's exception log.

        Best-effort — a trip exception is informational for the control
        tower; if we cannot write it (no trip attached, schema mismatch,
        etc.) the rejection itself must still succeed."""
        try:
            trip_name = self.get("trip")
            if not trip_name:
                return
            trip = frappe.get_doc("CH Logistics Trip", trip_name)
            # Map carrier-grade reason → trip exception type taxonomy.
            exc_type_map = {
                "Customer Not Available": "Customer Not Available",
                "Address Not Found": "Address Issue",
                "Receiver Refused": "Customer Not Available",
                "Damaged in Transit": "Damage",
                "Vehicle Breakdown": "Vehicle Breakdown",
            }
            exc_type = exc_type_map.get(reason, "Other")
            severity = "High" if reason in ("Damaged in Transit", "Vehicle Breakdown") else "Medium"
            trip.append("exceptions", {
                "occurred_at": now_datetime(),
                "exception_type": exc_type,
                "severity": severity,
                "stop_sequence": self.get("stop_sequence") or 0,
                "remarks": _("Manifest {0} rejected in transit: {1}. {2}").format(
                    self.name, reason, notes or ""),
                "photo": self.rejection_photo,
                "resolution_status": "Open",
            })
            trip.flags.ignore_validate_update_after_submit = True
            trip.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(title=f"trip exception raise failed for {self.name}",
                             message=frappe.get_traceback())

    def _notify_dispatcher_rejection(self):
        """FR-026: alert the dispatch desk that a manifest was rejected."""
        try:
            recipients = set()
            if self.owner:
                recipients.add(self.owner)
            for u in frappe.get_all(
                "Has Role", filters={"role": "Delivery Manager", "parenttype": "User"},
                pluck="parent",
            ):
                recipients.add(u)
            subject = _("Manifest {0} rejected: {1}").format(self.name, self.rejection_reason)
            body = _("Driver {0} rejected manifest {1}. Reason: {2}.").format(
                self.driver_name or self.driver or "", self.name, self.rejection_reason)
            for user in recipients:
                if not user or user == "Administrator":
                    continue
                frappe.get_doc({
                    "doctype": "Notification Log",
                    "for_user": user,
                    "type": "Alert",
                    "subject": subject,
                    "email_content": body,
                    "document_type": "CH Transfer Manifest",
                    "document_name": self.name,
                }).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(title=f"rejection notify failed for {self.name}",
                             message=frappe.get_traceback())

    def mark_reached_destination(self, lat, lng):
        """Driver taps 'Reached Location' when they arrive at the receiver.

        Operationally this is the arrival-geofence ping used by every major
        carrier (Delhivery, BlueDart, Ekart, FedEx, Oracle TMS, SAP TM): the
        driver has to confirm presence at the destination before the
        delivery-completion form unlocks. Status stays at 'In Transit' but
        arrival_datetime + arrival_lat/lng are recorded, which is what
        ``complete_delivery`` gates on.

        Returns the dict ``complete_delivery`` callers can echo back so the
        UI knows when the arrival ping was accepted.
        """
        lock_key = f"manifest_arrival_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]:
            frappe.throw(_("Manifest {0} is being updated by another user. Please refresh and try again.").format(self.name))
        try:
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status
            if self.status != "In Transit":
                frappe.throw(_("Can only record arrival while the manifest is In Transit (current: {0}).")
                             .format(self.status),
                             title=_("Ch Transfer Manifest Error"))
            if not frappe.get_meta(self.doctype).has_field("arrival_datetime"):
                frappe.throw(_("Arrival capture fields not installed. Run patch "
                               "ch_logistics.patches.v0_0_6.add_arrival_location_fields."),
                             title=_("Schema Mismatch"))
            lat_f, lng_f = self._validate_geo(lat, lng, kind="arrival")
            self.arrival_datetime = now_datetime()
            self.arrival_lat = lat_f
            self.arrival_lng = lng_f
            self.flags.ignore_validate_update_after_submit = True
            self.save()
            # Tell the destination store / warehouse the driver is at the door
            # so they can prepare to receive — reuses the existing customer
            # tracking notification pipeline.
            try:
                from ch_logistics.api.customer_tracking import notify_destination
                notify_destination(self.name, "arrived_at_destination")
            except Exception:
                # Non-fatal — the geofence ping itself is the source of truth;
                # the notification is best-effort.
                frappe.log_error(frappe.get_traceback(),
                                 f"arrived_at_destination notify failed for {self.name}")
            return {
                "arrival_datetime": str(self.arrival_datetime),
                "arrival_lat": self.arrival_lat,
                "arrival_lng": self.arrival_lng,
            }
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def complete_delivery(self, delivery_photo, receiver_name, otp=None,
                          lat=None, lng=None, scanned_qr=None):
        lock_key = f"manifest_status_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(frappe._("Manifest {0} is being updated by another user. Please refresh and try again.").format(self.name))
        try:
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status

            if self.status not in ("In Transit",):
                frappe.throw(_("Can only deliver when status is In Transit."), title=_("Ch Transfer Manifest Error"))
            # Two-stage POD: the driver must have explicitly tapped "Reached
            # Location" before the receiver-side handover can be recorded.
            # Carrier apps (Delhivery / BlueDart / Ekart / FedEx) all require
            # the arrival geofence ping before they unlock the delivery form.
            if frappe.get_meta(self.doctype).has_field("arrival_datetime") and not self.arrival_datetime:
                frappe.throw(
                    _("Tap 'Reached Location' to record arrival at the destination before completing delivery."),
                    title=_("Arrival Not Recorded"),
                )
            if not delivery_photo:
                frappe.throw(_("Delivery photo is mandatory."), title=_("Ch Transfer Manifest Error"))
            if not receiver_name:
                frappe.throw(_("Receiver name is mandatory."), title=_("Ch Transfer Manifest Error"))
            # Mandatory delivery-side QR scan (parallel to pickup scan).
            self._validate_delivery_qr(scanned_qr)
            # Mandatory driver GPS at the receiver's doorstep (proof of presence).
            lat_f, lng_f = self._validate_geo(lat, lng, kind="delivery")
            # OTP verification (secondary factor, gated by its own flag).
            if self.delivery_otp:
                if not otp:
                    frappe.throw(_("Delivery OTP is required."), title=_("Ch Transfer Manifest Error"))
                if str(otp).strip() != str(self.delivery_otp).strip():
                    frappe.throw(_("Invalid OTP. Please check and try again."), title=_("Ch Transfer Manifest Error"))
                self.delivery_otp_verified = 1

            self.delivery_photo = delivery_photo
            self.delivery_datetime = now_datetime()
            self.delivery_lat = lat_f
            self.delivery_lng = lng_f
            self.receiver_name = receiver_name
            self.status = "Delivered"
            self.flags.ignore_validate_update_after_submit = True
            self.save()
            self._sync_logistics_status_to_entries("Delivered")
            # Lifecycle: this manifest is done. If the driver has no other
            # Assigned / Pickup Started / In Transit manifests, recomputer
            # drops them back to AVAILABLE so dispatch can re-assign them.
            # If they're still carrying other loads, IN_TRANSIT is preserved.
            self._sync_driver_state_after_action()
            from ch_logistics.api.customer_tracking import notify_destination
            notify_destination(self.name, "delivered")
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def accept_delivery(self, received_by=None, damage_reported=False,
                        damage_notes=None, damage_photo=None,
                        received_lines=None):
        """Receive a delivered manifest. Supports per-row partial receipt.

        received_lines (optional): list of {stock_entry, received_qty} dicts.
            When omitted, every row is treated as fully received (legacy behavior).
            When supplied, rows whose received_qty < total_qty cause the
            manifest to settle into "Partially Received" status and a
            shortage CH Delivery Claim is auto-raised.

        GAP-2: Auto-submits linked Draft Stock Entries so the stock ledger
        immediately reflects physical receipt without manual intervention.
        Partial-receipt rows have their SE qty trimmed to what was received.
        """
        lock_key = f"manifest_accept_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 15)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(frappe._("Manifest {0} is being accepted by another user. Please refresh and try again.").format(self.name))
        try:
            # Re-read status after acquiring lock to avoid race
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status

            if self.status not in ("Delivered",):
                frappe.throw(_("Can only accept when status is Delivered."), title=_("Ch Transfer Manifest Error"))
            self.received_by = received_by or frappe.session.user
            self.received_datetime = now_datetime()
            self.damage_reported = cint(damage_reported)
            self.damage_notes = damage_notes
            self.damage_photo = damage_photo

            # Phase 3: per-row received qty + shortage detection
            if isinstance(received_lines, str):
                try:
                    received_lines = frappe.parse_json(received_lines)
                except Exception:
                    received_lines = None
            received_map = {}
            if received_lines:
                for r in received_lines:
                    if not isinstance(r, dict):
                        continue
                    se_name = r.get("stock_entry")
                    if se_name:
                        received_map[se_name] = flt(r.get("received_qty"))

            total_shortage = 0.0
            shortage_rows = []
            for row in self.transfers:
                if received_map:
                    rcv = received_map.get(row.stock_entry, flt(row.total_qty))
                else:
                    rcv = flt(row.total_qty)
                rcv = max(0.0, min(rcv, flt(row.total_qty)))
                row.custom_received_qty = rcv
                shortage = flt(row.total_qty) - rcv
                row.custom_shortage_qty = shortage
                if shortage > 0:
                    total_shortage += shortage
                    shortage_rows.append({
                        "stock_entry": row.stock_entry,
                        "expected": flt(row.total_qty),
                        "received": rcv,
                        "shortage": shortage,
                    })

            partially_received = bool(received_map) and total_shortage > 0
            self.status = "Partially Received" if partially_received else "Received"
            self.flags.ignore_validate_update_after_submit = True
            self.save()

            if cint(damage_reported) and damage_notes:
                self._auto_create_damage_claim(damage_notes, damage_photo)
            if partially_received:
                self._auto_create_shortage_claim(shortage_rows, total_shortage)

            # GAP-2: auto-submit Draft SEs so ledger reflects physical receipt immediately
            self._auto_submit_stock_entries(received_map)
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def close_manifest(self):
        if self.status not in ("Received", "Partially Received"):
            frappe.throw(_("Can only close when status is Received or Partially Received."), title=_("Ch Transfer Manifest Error"))
        self.status = "Closed"
        self.flags.ignore_validate_update_after_submit = True
        self.save()
        if flt(self.freight_amount) > 0 and not self.freight_journal_entry:
            self._post_freight_gl()
        # GAP-8: distribute freight to item valuation via Landed Cost Voucher
        if flt(self.freight_amount) > 0:
            self._create_landed_cost_voucher()

    # ── Private ─────────────────────────────────────────────────────────

    def _auto_submit_stock_entries(self, received_map: dict) -> None:
        """GAP-2: Submit Draft Stock Entries on delivery acceptance.

        For full receipt rows: submit SE as-is.
        For partial receipt rows: trim SE item quantities to received amounts
        before submitting, so the stock ledger is accurate. Untouched items
        within a partially-received SE are trimmed proportionally.
        """
        submitted = []
        errors = []
        for row in self.transfers:
            try:
                se_doc = frappe.get_doc("Stock Entry", row.stock_entry)
                if se_doc.docstatus != 0:
                    continue  # already submitted or cancelled — skip

                rcv = flt(row.custom_received_qty)
                planned = flt(row.total_qty)

                # Trim item quantities for partial receipts
                if received_map and planned > 0 and rcv < planned:
                    ratio = rcv / planned
                    for item in se_doc.items:
                        item.qty = flt(item.qty * ratio, 3)
                        item.transfer_qty = item.qty
                    se_doc.flags.ignore_validate = False

                _ok = True
                for _si in se_doc.items:
                    if not _si.s_warehouse: continue
                    _avail = frappe.db.sql("SELECT SUM(actual_qty) FROM `tabStock Ledger Entry` WHERE item_code=%s AND warehouse=%s AND is_cancelled=0", (_si.item_code, _si.s_warehouse))[0][0] or 0
                    if flt(_avail) < flt(_si.qty):
                        frappe.log_error(f"SE {se_doc.name}: {_si.item_code} needs {_si.qty}, available {_avail} in {_si.s_warehouse}", "Manifest SE Insufficient Stock")
                        _ok = False
                        break
                if not _ok:
                    continue  # skip this SE

                se_doc.flags.ignore_permissions = True
                se_doc.submit()
                submitted.append(row.stock_entry)
            except Exception:
                errors.append(row.stock_entry)
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Auto-submit SE failed for manifest {self.name}: {row.stock_entry}",
                )

        if submitted:
            self.add_comment(
                "Comment",
                _("Auto-submitted {0} Stock Entries on delivery acceptance: {1}").format(
                    len(submitted), ", ".join(submitted)
                ),
            )
        if errors:
            frappe.msgprint(
                _("Could not auto-submit {0} Stock Entries: {1}. Please submit manually.").format(
                    len(errors), ", ".join(errors)
                ),
                indicator="orange",
                alert=True,
            )

    def _create_landed_cost_voucher(self) -> None:
        """GAP-8: Distribute manifest freight cost across items via Landed Cost Voucher.

        Creates a Draft LCV linked to all submitted Stock Entries in this manifest.
        The LCV proportionally adjusts item valuation rates so the total landed
        cost (transfer price + freight) is correctly reflected in the stock ledger.
        """
        if frappe.db.get_value("CH Transfer Manifest", self.name, "custom_landed_cost_voucher"):
            return  # already created

        submitted_ses = [
            row.stock_entry for row in self.transfers
            if frappe.db.get_value("Stock Entry", row.stock_entry, "docstatus") == 1
        ]
        if not submitted_ses:
            return

        try:
            lcv = frappe.new_doc("Landed Cost Voucher")
            lcv.company = self.company
            lcv.posting_date = frappe.utils.today()
            lcv.distribute_charges_based_on = "Amount"

            for se_name in submitted_ses:
                lcv.append("purchase_receipts", {
                    "receipt_document_type": "Stock Entry",
                    "receipt_document": se_name,
                })

            freight_account = self.freight_account or frappe.db.get_value(
                "Company", self.company, "default_expense_account"
            )
            if freight_account:
                lcv.append("taxes", {
                    "expense_account": freight_account,
                    "description": _("Freight — manifest {0}").format(self.name),
                    "amount": flt(self.freight_amount),
                })

            lcv.flags.ignore_permissions = True
            lcv.insert(ignore_permissions=True)
            frappe.db.set_value(
                "CH Transfer Manifest", self.name,
                "custom_landed_cost_voucher", lcv.name,
                update_modified=False,
            )
            self.add_comment(
                "Comment",
                _("Landed Cost Voucher {0} created for freight ₹{1:.2f}.").format(
                    lcv.name, flt(self.freight_amount)
                ),
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Landed Cost Voucher creation failed for manifest {self.name}",
            )

    def _auto_create_damage_claim(self, damage_notes: str, damage_photo: str = None) -> None:
        """Auto-create a CH Delivery Claim when damage is reported on acceptance."""
        try:
            claim = frappe.new_doc("CH Delivery Claim")
            claim.manifest = self.name
            claim.company = self.company
            claim.claim_date = frappe.utils.today()
            claim.damage_notes = damage_notes
            if damage_photo:
                claim.damage_photo = damage_photo
            claim.claim_type = "Courier" if self.courier_partner else "Internal"
            claim.responsible_party = self.courier_partner or self.driver_name or ""
            claim.insert(ignore_permissions=True)
            self.add_comment(
                "Comment",
                _("Damage claim {0} auto-created on delivery acceptance.").format(claim.name),
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Auto damage claim failed: {self.name}")

    def _auto_create_shortage_claim(self, shortage_rows, total_shortage):
        """Auto-create a CH Delivery Claim when partial-receipt shortage is recorded."""
        try:
            lines_html = "".join(
                f"<li>{r['stock_entry']}: expected {r['expected']:g}, "
                f"received {r['received']:g}, shortage <b>{r['shortage']:g}</b></li>"
                for r in shortage_rows
            )
            damage_notes = (
                f"Partial receipt shortage recorded on {self.name}. "
                f"Total shortage: {total_shortage:g} units.<ul>{lines_html}</ul>"
            )
            claim = frappe.new_doc("CH Delivery Claim")
            claim.manifest = self.name
            claim.company = self.company
            claim.claim_date = frappe.utils.today()
            claim.damage_notes = damage_notes
            claim.claim_type = "Courier" if self.courier_partner else "Internal"
            claim.responsible_party = self.courier_partner or self.driver_name or ""
            claim.insert(ignore_permissions=True)
            self.add_comment(
                "Comment",
                _("Shortage claim {0} auto-created — total {1} units short.").format(
                    claim.name, total_shortage
                ),
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Auto shortage claim failed: {self.name}")

    def _compute_freight(self):
        """Compute freight_amount from courier rate card × total package weight."""
        if not self.courier_partner:
            return
        courier = frappe.get_cached_doc("Courier Partner", self.courier_partner)
        rate_per_kg = flt(courier.rate_per_kg)
        if not rate_per_kg:
            return
        # Total weight from packages child table
        total_weight = sum(flt(p.get("weight_kg")) for p in self.packages) if self.packages else 0
        self.total_weight_kg = total_weight
        min_weight = flt(courier.min_billable_weight_kg) or 0.5
        billable_weight = max(total_weight, min_weight) if total_weight > 0 else 0
        base_charge = billable_weight * rate_per_kg
        fuel_surcharge = base_charge * (flt(courier.fuel_surcharge_pct) / 100)
        self.freight_amount = flt(base_charge + fuel_surcharge, 2)

    def _post_freight_gl(self):
        """Post journal entry: Dr Freight Expense → Cr Freight Payable on manifest close."""
        if not self.company:
            return
        amount = flt(self.freight_amount)
        if amount <= 0:
            return

        freight_account = self.freight_account
        if not freight_account:
            freight_account = frappe.db.get_value(
                "Company", self.company, "default_expense_account"
            )
        if not freight_account:
            frappe.log_error(
                f"Freight GL skipped for {self.name}: no freight expense account configured.",
                "Manifest Freight GL",
            )
            frappe.throw(frappe._("Freight GL posting failed for manifest {0}. Check Error Log and retry.").format(self.name))

        payable_account = frappe.db.get_value("Company", self.company, "default_payable_account")
        if not payable_account:
            frappe.log_error(
                f"Freight GL skipped for {self.name}: no default payable account on Company.",
                "Manifest Freight GL",
            )
            frappe.throw(frappe._("Freight GL posting failed for manifest {0}. Check Error Log and retry.").format(self.name))

        cost_center = frappe.db.get_value("Company", self.company, "cost_center")

        try:
            je = frappe.new_doc("Journal Entry")
            je.update({
                "voucher_type": "Journal Entry",
                "company": self.company,
                "posting_date": frappe.utils.today(),
                "cheque_no": self.name,
                "cheque_date": frappe.utils.today(),
                "remark": _("Freight charge — manifest {0} via {1}").format(
                    self.name, self.courier_partner or "Own Transport"
                ),
                "accounts": [
                    {
                        "account": freight_account,
                        "debit_in_account_currency": amount,
                        "cost_center": cost_center,
                        "reference_type": "CH Transfer Manifest",
                        "reference_name": self.name,
                    },
                    {
                        "account": payable_account,
                        "credit_in_account_currency": amount,
                        "cost_center": cost_center,
                        "reference_type": "CH Transfer Manifest",
                        "reference_name": self.name,
                    },
                ],
            })
            je.flags.ignore_permissions = True
            je.flags.ch_system_generated_je = True
            je.insert(ignore_permissions=True)
            je.submit()
            self.db_set("freight_journal_entry", je.name, update_modified=False)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Freight GL failed for manifest {self.name}")
            frappe.throw(frappe._("Freight GL posting failed for manifest {0}. Check Error Log and retry.").format(self.name))

    def _generate_delivery_otp(self):
        self.delivery_otp = str(secrets.randbelow(900000) + 100000)

    def _sync_logistics_status_to_entries(self, logistics_status):
        """Push logistics status change to all child Stock Entries.

        Two parallel fields on Stock Entry drive the receiving-side UX:

        * ``custom_logistics_status``  — courier-leg state machine
          (Pending Pickup / In Transit / Delivered / Revert Requested …).
          Used by tracking widgets and the logistics badge in the POS
          Stock Transfer workspace.
        * ``custom_status``            — receiving-side workflow state
          (Pending With Goods / Ready For Pickup / In Transit /
           Ready For Receive / Receive At Transit / Transferred). The POS
          Stock Transfer workspace gates its "Scan & Receive" CTA on
          ``custom_status in ("Ready For Receive", "Receive At Transit")``,
          so the receiving store cannot acknowledge goods until this
          field advances.

        The SE-level legacy APIs (``ch_erp15.custom.stock_entry.logistics_pickup``
        and ``logistics_deliver``) used to be the only path that advanced
        ``custom_status`` — but the driver app now runs the manifest's
        ``start_pickup`` / ``complete_delivery`` instead, which left
        ``custom_status`` stuck at the pre-pickup value (typically
        "Ready For Pickup") even though the goods had already been
        delivered. As a result the receiving store's "Scan & Receive"
        button never appeared.

        We now advance ``custom_status`` in lockstep with every
        ``custom_logistics_status`` transition, and stamp
        ``custom_pickup_datetime`` / ``custom_delivery_datetime`` so the
        SE-level audit trail matches what the SE-level API would have
        recorded.
        """
        # logistics_status → custom_status (receiving-side workflow)
        custom_status_map = {
            "In Transit":       "In Transit",
            "Delivered":        "Ready For Receive",
            "Return to Source": "Pending With Goods",
            "Pending Pickup":   "Ready For Pickup",
        }
        target_custom_status = custom_status_map.get(logistics_status)

        # Only set columns that actually exist on this tenant's Stock Entry
        # meta — keeps the patch idempotent across upgrades.
        meta = frappe.get_meta("Stock Entry")
        now = now_datetime()

        for row in self.transfers:
            update = {}
            if meta.has_field("custom_logistics_status"):
                update["custom_logistics_status"] = logistics_status
            if target_custom_status and meta.has_field("custom_status"):
                update["custom_status"] = target_custom_status
            if logistics_status == "In Transit" and meta.has_field("custom_pickup_datetime"):
                # Don't overwrite a previously-stamped pickup — only fill if blank.
                existing = frappe.db.get_value(
                    "Stock Entry", row.stock_entry, "custom_pickup_datetime"
                )
                if not existing:
                    update["custom_pickup_datetime"] = now
            if logistics_status == "Delivered" and meta.has_field("custom_delivery_datetime"):
                existing = frappe.db.get_value(
                    "Stock Entry", row.stock_entry, "custom_delivery_datetime"
                )
                if not existing:
                    update["custom_delivery_datetime"] = now
            if not update:
                continue
            frappe.db.set_value(
                "Stock Entry", row.stock_entry, update,
                update_modified=False,
            )

    # ── Recall / Reversal ───────────────────────────────────────────────

    def initiate_recall(self, reason, notes=None):
        """Store/warehouse manager initiates a transfer recall.

        Allowed from: Packed, Assigned, In Transit, Delivered.
        Sends email + in-app notification to driver and store contacts.
        """
        lock_key = f"manifest_recall_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(frappe._("Manifest {0} is being recalled by another user. Please refresh and try again.").format(self.name))
        try:
            allowed = ("Packed", "Assigned", "In Transit", "Delivered")
            if self.status not in allowed:
                frappe.throw(
                    _("Recall can only be initiated when status is one of: {0}. Current status: {1}.").format(
                        ", ".join(allowed), self.status
                    ),
                    title=_("Transfer Recall Error"),
                )
            if not reason:
                frappe.throw(_("Recall reason is mandatory."), title=_("Transfer Recall Error"))

            prev_status = self.status
            self.recall_reason = reason
            self.recall_notes = notes or ""
            self.recall_initiated_by = frappe.session.user
            self.recall_initiated_at = now_datetime()
            self.status = "Recall Initiated"
            self.flags.ignore_validate_update_after_submit = True
            self.save()

            self.add_comment(
                "Comment",
                _("Transfer Recall initiated by {0}. Reason: {1}. Previous status: {2}.").format(
                    frappe.session.user, reason, prev_status
                ),
            )

            # Notify driver + stores asynchronously (don't block on failure)
            try:
                self._notify_recall_driver()
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Recall driver notification failed: {self.name}")

            try:
                self._notify_recall_stores()
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Recall store notification failed: {self.name}")
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def confirm_return(self, return_photo, confirmed_by=None):
        """Delivery person confirms all items have been physically returned to source.

        Creates reverse Stock Entries (cancels original SEs) to reinstate stock at source.
        Status → Returned.
        """
        lock_key = f"manifest_return_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(frappe._("Manifest {0} return is being confirmed by another user. Please refresh and try again.").format(self.name))
        try:
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status

            if self.status != "Recall Initiated":
                frappe.throw(
                    _("Return can only be confirmed when status is 'Recall Initiated'. Current: {0}.").format(self.status),
                    title=_("Transfer Return Error"),
                )
            if not return_photo:
                frappe.throw(_("Return photo is mandatory."), title=_("Transfer Return Error"))

            self.return_photo = return_photo
            self.return_confirmed_by = confirmed_by or frappe.session.user
            self.return_confirmed_at = now_datetime()

            reversed_ses = self._reverse_stock_entries()
            self.reversed_stock_entries = ", ".join(reversed_ses) if reversed_ses else "—"

            self.status = "Returned"
            self.flags.ignore_validate_update_after_submit = True
            self.save()

            self.add_comment(
                "Comment",
                _("Return confirmed by {0} at {1}. Stock reversed: {2}").format(
                    self.return_confirmed_by,
                    self.return_confirmed_at,
                    self.reversed_stock_entries,
                ),
            )

            return reversed_ses
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def _reverse_stock_entries(self):
        """Cancel the underlying submitted Stock Entries to return stock to source.

        Falls back to creating a reverse Material Transfer SE if cancel fails
        (e.g. dependent documents exist).
        Returns list of action strings for audit log.
        """
        results = []
        for row in self.transfers:
            se_name = row.stock_entry
            try:
                se = frappe.get_doc("Stock Entry", se_name)
                if se.docstatus != 1:
                    results.append(f"{se_name} (skipped — not submitted)")
                    continue
                se.cancel()
                results.append(f"{se_name} (cancelled)")
            except Exception as primary_err:
                # Cancel failed — create a reverse SE instead
                try:
                    reverse_name = self._create_reverse_se(se_name)
                    results.append(f"{se_name} → reverse {reverse_name}")
                except Exception as reverse_err:
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"Recall: could not reverse SE {se_name} for manifest {self.name}",
                    )
                    results.append(f"{se_name} (ERROR: {str(primary_err)[:80]})")
        return results

    def _create_reverse_se(self, se_name):
        """Create a new Material Transfer SE that reverses the direction (dest → source)."""
        original = frappe.get_doc("Stock Entry", se_name)
        reverse = frappe.new_doc("Stock Entry")
        reverse.stock_entry_type = "Material Transfer"
        reverse.from_warehouse = original.to_warehouse
        reverse.to_warehouse = original.from_warehouse
        reverse.company = original.company
        reverse.remarks = _("Reverse transfer for recall of manifest {0} (original SE: {1})").format(
            self.name, se_name
        )
        for item in original.items:
            reverse.append("items", {
                "item_code": item.item_code,
                "qty": item.qty,
                "uom": item.uom,
                "serial_no": item.serial_no,
                "batch_no": item.batch_no,
                "s_warehouse": original.to_warehouse,
                "t_warehouse": original.from_warehouse,
            })
        reverse.insert(ignore_permissions=True)
        reverse.submit()
        return reverse.name

    def _notify_recall_driver(self):
        """Send email + in-app notification to the assigned driver."""
        if not self.driver:
            return

        driver_user = frappe.db.get_value("Driver", self.driver, "user")
        driver_name = self.driver_name or self.driver
        driver_phone = self.driver_phone or "—"

        subject = _("⚠ URGENT: Transfer Recall — {0}").format(self.name)
        manifest_url = frappe.utils.get_url_to_form(self.doctype, self.name)
        company_name = self.company or "Congruence Holdings"
        message = _("""
            <div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
            <div style="background:#7f1d1d;color:#ffffff;padding:12px 16px;font-weight:600">{company_name} — Urgent Recall Notice</div>
            <div style="padding:16px">
            <p>Dear <strong>{driver_name}</strong>,</p>
            <p>The transfer manifest <strong>{manifest}</strong> has been <strong>recalled</strong> by the warehouse.</p>
            <table style="border-collapse:collapse;width:100%;font-size:14px">
                <tr><td style="padding:6px;font-weight:bold">Recall Reason</td><td style="padding:6px">{reason}</td></tr>
                <tr><td style="padding:6px;font-weight:bold">Source Warehouse</td><td style="padding:6px">{source}</td></tr>
                <tr><td style="padding:6px;font-weight:bold">Destination Warehouse</td><td style="padding:6px">{dest}</td></tr>
            </table>
            <br/>
            <p><strong>Action Required:</strong></p>
            <ol>
                <li>Stop the current delivery immediately.</li>
                <li>Return <strong>all items</strong> to the source warehouse: <strong>{source}</strong>.</li>
                <li>Once returned, scan each item and confirm the return in the app.</li>
                <li>Take a photo of returned items as proof.</li>
            </ol>
            <p>If you have questions, contact the warehouse manager immediately.</p>
            <p style="margin-top:18px">
                <a href="{manifest_url}" style="background:#0b57d0;color:#ffffff;text-decoration:none;padding:10px 14px;border-radius:6px;display:inline-block;font-weight:600">Open Manifest</a>
            </p>
            </div></div>
        """).format(
            company_name=company_name,
            driver_name=driver_name,
            manifest=self.name,
            reason=self.recall_reason,
            source=self.source_warehouse,
            dest=self.destination_warehouse,
            manifest_url=manifest_url,
        )

        if driver_user:
            # In-app realtime notification
            frappe.publish_realtime(
                event="notification",
                message={
                    "subject": subject,
                    "message": _("Transfer {0} recalled. Return all items to {1}.").format(
                        self.name, self.source_warehouse
                    ),
                    "type": "error",
                    "from_user": frappe.session.user,
                },
                user=driver_user,
            )
            # Email
            frappe.sendmail(
                recipients=[driver_user],
                subject=subject,
                message=message,
                reference_doctype=self.doctype,
                reference_name=self.name,
                delayed=False,
            )

    def _notify_recall_stores(self):
        """Send recall notice emails to source and destination store contacts."""
        stores = []
        if self.source_store:
            stores.append((self.source_store, "Source"))
        if self.destination_store:
            stores.append((self.destination_store, "Destination"))

        for store_name, label in stores:
            try:
                contact_email = frappe.db.get_value("CH Store", store_name, "email")
            except Exception:
                contact_email = None
            store_user = None
            try:
                from ch_erp15.ch_erp15.store_request_api import _get_store_managers
                managers = _get_store_managers(store_name) or []
                store_user = managers[0] if managers else None
            except Exception:
                store_user = None
            if not contact_email and not store_user:
                continue

            subject = _("Transfer Recall Notice — {0}").format(self.name)
            manifest_url = frappe.utils.get_url_to_form(self.doctype, self.name)
            company_name = self.company or "Congruence Holdings"
            message = _("""
                <div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
                <div style="background:#0f172a;color:#ffffff;padding:12px 16px;font-weight:600">{company_name} — Transfer Recall Notice</div>
                <div style="padding:16px">
                <p>This is to notify you that transfer manifest <strong>{manifest}</strong> has been recalled.</p>
                <table style="border-collapse:collapse;width:100%;font-size:14px">
                    <tr><td style="padding:6px;font-weight:bold">Your Store ({label})</td><td style="padding:6px">{store}</td></tr>
                    <tr><td style="padding:6px;font-weight:bold">Recall Reason</td><td style="padding:6px">{reason}</td></tr>
                    <tr><td style="padding:6px;font-weight:bold">Recall Initiated By</td><td style="padding:6px">{by}</td></tr>
                    <tr><td style="padding:6px;font-weight:bold">Items in Transit</td><td style="padding:6px">{items} item lines / {qty} units</td></tr>
                </table>
                <br/>
                <p>The delivery person has been instructed to return all items to the source warehouse.
                Please do not accept any delivery for this manifest.</p>
                <p style="margin-top:18px">
                    <a href="{manifest_url}" style="background:#0b57d0;color:#ffffff;text-decoration:none;padding:10px 14px;border-radius:6px;display:inline-block;font-weight:600">Open Manifest</a>
                </p>
                </div></div>
            """).format(
                company_name=company_name,
                manifest=self.name,
                label=label,
                store=store_name,
                reason=self.recall_reason,
                by=self.recall_initiated_by,
                items=self.total_items,
                qty=self.total_qty,
                manifest_url=manifest_url,
            )

            recipients = []
            if contact_email:
                recipients.append(contact_email)
            if store_user and store_user != contact_email:
                recipients.append(store_user)

            if recipients:
                frappe.sendmail(
                    recipients=recipients,
                    subject=subject,
                    message=message,
                    reference_doctype=self.doctype,
                    reference_name=self.name,
                    delayed=False,
                )


# ────────────────────────────────────────────────────────────────────────
# Whitelisted helpers (e-Way Bill orchestration from the manifest form)
# ────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def refresh_ewaybill_summary(manifest):
    """Pull the latest EWB numbers / validity off each linked Stock Entry,
    update the cached summary on the manifest, and return the structured list.

    Called by the manifest form's "Refresh e-Way Bills" button and by the
    background poller after enqueueing generations.
    """
    if not manifest:
        frappe.throw(_("Manifest name is required."))
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("read")
    return doc.refresh_ewaybill_summary()


@frappe.whitelist()
def resync_ewaybills(manifest):
    """Manually re-run EWB sync for a manifest (e.g. after addresses are
    corrected, or a failed job is retried). Restricted to users who can
    write to the manifest."""
    if not manifest:
        frappe.throw(_("Manifest name is required."))
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    if doc.status not in ("Assigned", "Pickup Started", "In Transit"):
        frappe.throw(_("e-Way Bills can only be (re)synced once the driver is Assigned."))
    if not doc.vehicle_number:
        frappe.throw(_("Vehicle Number is missing — cannot sync e-Way Bills."))
    doc._sync_ewaybills_for_transfers()
    return doc.refresh_ewaybill_summary()
