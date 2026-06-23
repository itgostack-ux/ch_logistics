"""CH Transfer Manifest controller.

Lifecycle:
    Draft → Packed → Assigned → Pickup Started → In Transit → Delivered → Received → Closed
"""

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, cint, flt


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

            self.vehicle_number = vehicle_number or self.vehicle_number
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
            self.pickup_photo = pickup_photo
            self.pickup_datetime = now_datetime()
            self.pickup_lat = flt(lat)
            self.pickup_lng = flt(lng)
            self.pickup_notes = notes
            self.status = "In Transit"
            self.flags.ignore_validate_update_after_submit = True
            self.save()
            self._sync_logistics_status_to_entries("In Transit")
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

    def reject_manifest(self, rejection_reason, rejection_photo, rejection_notes=None):
        """Driver rejects a pickup that cannot be completed (FR-022 → FR-027).

        Reason + proof photo are mandatory; the manifest moves to Rejected and
        the dispatcher is notified. Stock is untouched — nothing was picked."""
        lock_key = f"manifest_status_{frappe.scrub(self.name)}"
        if not frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]:
            frappe.throw(_("Manifest {0} is being updated by another user. Please refresh and try again.").format(self.name))
        try:
            current_status = frappe.db.get_value("CH Transfer Manifest", self.name, "status")
            if current_status != self.status:
                self.status = current_status
            if self.status not in ("Assigned", "Pickup Started"):
                frappe.throw(_("Can only reject a manifest before pickup is completed (current: {0}).").format(self.status),
                             title=_("Ch Transfer Manifest Error"))
            valid_reasons = {"Material Not Ready", "Wrong Package", "Store Closed",
                             "Damaged Package", "Other"}
            if rejection_reason not in valid_reasons:
                frappe.throw(_("Select a valid rejection reason."), title=_("Ch Transfer Manifest Error"))
            if not rejection_photo:
                frappe.throw(_("Rejection proof photo is mandatory."), title=_("Ch Transfer Manifest Error"))

            self.rejection_reason = rejection_reason
            self.rejection_photo = rejection_photo
            self.rejection_notes = rejection_notes
            self.rejected_by = frappe.session.user
            self.rejected_at = now_datetime()
            self.status = "Rejected"
            self.flags.ignore_validate_update_after_submit = True
            self.save()
            # Goods never left the source — return child entries to Pending Pickup.
            self._sync_logistics_status_to_entries("Pending Pickup")
            self._notify_dispatcher_rejection()
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

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

    def complete_delivery(self, delivery_photo, receiver_name, otp=None,
                          lat=None, lng=None):
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
            if not delivery_photo:
                frappe.throw(_("Delivery photo is mandatory."), title=_("Ch Transfer Manifest Error"))
            if not receiver_name:
                frappe.throw(_("Receiver name is mandatory."), title=_("Ch Transfer Manifest Error"))
            # OTP verification
            if self.delivery_otp:
                if not otp:
                    frappe.throw(_("Delivery OTP is required."), title=_("Ch Transfer Manifest Error"))
                if str(otp).strip() != str(self.delivery_otp).strip():
                    frappe.throw(_("Invalid OTP. Please check and try again."), title=_("Ch Transfer Manifest Error"))
                self.delivery_otp_verified = 1

            self.delivery_photo = delivery_photo
            self.delivery_datetime = now_datetime()
            self.delivery_lat = flt(lat)
            self.delivery_lng = flt(lng)
            self.receiver_name = receiver_name
            self.status = "Delivered"
            self.flags.ignore_validate_update_after_submit = True
            self.save()
            self._sync_logistics_status_to_entries("Delivered")
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
        """Push logistics status change to all child Stock Entries."""
        for row in self.transfers:
            frappe.db.set_value(
                "Stock Entry", row.stock_entry,
                "custom_logistics_status", logistics_status,
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
