"""CH Transfer Manifest controller.

Lifecycle:
    Draft → Packed → Assigned → Pickup Started → In Transit → Delivered → Received → Closed
"""

import hashlib
import hmac
import math
import re
import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
    add_to_date,
    cint,
    flt,
    get_datetime,
    getdate,
    now_datetime,
    nowdate,
)
from frappe.utils.password import get_encryption_key

_OTP_DIGEST_PREFIX = "hmac-sha256$"


def delivery_otp_digest(otp: str) -> str:
    """Return a site-keyed digest; a receiver OTP must never be stored raw."""
    key = get_encryption_key()
    if isinstance(key, str):
        key = key.encode("utf-8")
    value = str(otp or "").strip().encode("utf-8")
    return _OTP_DIGEST_PREFIX + hmac.new(key, value, hashlib.sha256).hexdigest()


def verify_delivery_otp(stored: str | None, candidate: str | None) -> bool:
    stored = str(stored or "").strip()
    candidate = str(candidate or "").strip()
    if not stored or not candidate:
        return False
    if stored.startswith(_OTP_DIGEST_PREFIX):
        return hmac.compare_digest(stored, delivery_otp_digest(candidate))
    # Migration-safe legacy verification.  Successful use clears the value;
    # rotation patches digest remaining active legacy codes.
    return hmac.compare_digest(stored, candidate)


class GeofenceError(frappe.ValidationError):
    """Raised when a driver's location fails the geofence.

    A distinct class so the driver app can recognise this specific refusal and
    offer the reasoned override, rather than string-matching a message that
    changes with translation.
    """


class CHTransferManifest(Document):

    _SERVER_MANAGED_FIELDS = frozenset({
        "status", "trip", "driver", "driver_name", "driver_phone",
        "vehicle_number", "custom_vehicle", "courier_partner",
        "tracking_number", "custom_external_booking_id",
        "pickup_datetime", "pickup_photo", "pickup_lat", "pickup_lng",
        "arrival_datetime", "arrival_lat", "arrival_lng",
        "delivery_datetime", "delivery_photo", "delivery_lat", "delivery_lng",
        "receiver_name", "delivery_otp", "delivery_otp_verified",
        "delivery_otp_log", "delivery_otp_generated_at", "delivery_otp_expires_at",
        "delivery_otp_sent_at", "delivery_otp_attempts", "delivery_otp_verified_at",
        "delivery_otp_verified_by", "qr_payload",
        "tracking_token", "received_by", "received_at", "closed_by", "closed_at",
        "rejected_by", "rejected_at", "rejected_during", "rejection_reason",
        "rejection_photo", "rejection_notes", "recall_initiated_by",
        "recall_initiated_at", "return_confirmed_by", "return_confirmed_at",
        "cancellation_reason", "cancelled_by", "cancelled_on",
    })

    def validate(self):
        self._validate_server_managed_fields()
        if not self.status:
            self.status = "Draft"
        self._populate_transfer_details()
        self._seed_manifest_stops()
        self._compute_totals()
        self._validate_packing()
        self._validate_transfers()
        # Must run AFTER _validate_server_managed_fields(): qr_payload is a
        # server-managed field, so seeding it before that check would trip the
        # forgery guard on the very save that mints it.
        self._ensure_qr_payload()
        self._refuse_delivered_with_draft_stock_entries()

    def before_update_after_submit(self):
        # A submitted manifest's saves run before_update_after_submit, NOT
        # validate() (frappe Document.run_before_save_methods), so the
        # Delivered gate must be wired here too or every real lifecycle
        # transition (manifests are submitted long before delivery) would
        # bypass it.
        self._refuse_delivered_with_draft_stock_entries()

    def _refuse_delivered_with_draft_stock_entries(self):
        """Refuse the transition INTO Delivered while a linked Stock Entry
        is still Draft.

        "Delivered" asserts the goods physically changed hands, so every
        transfer voucher behind the manifest must already be a posted
        (submitted) document — a Draft Stock Entry at that point means the
        ledger never recorded the movement the driver just handed over.
        The Sep-01 go-live audit found exactly this wreck in production
        data (TM-2026-00004: Delivered with a draft Stock Entry and
        stranded ledger rows); this gate makes that state impossible to
        (re)create through any save path. Only the transition into
        Delivered is gated — re-saving an already-Delivered legacy row or
        moving one forward to Received must not brick on old data.
        """
        if self.status != "Delivered":
            return
        before = self.get_doc_before_save()
        if before and before.get("status") == "Delivered":
            return
        self._assert_no_draft_stock_entries()

    def _assert_no_draft_stock_entries(self):
        """Throw if any transfers row points at a docstatus=0 Stock Entry.

        Deleted / missing Stock Entries are deliberately tolerated here:
        legacy Received flows purge the source SE, and _populate_transfer_
        details already polices missing rows for pre-dispatch manifests.
        """
        names = [row.stock_entry for row in (self.transfers or []) if row.stock_entry]
        if not names:
            return
        drafts = frappe.get_all(
            "Stock Entry",
            filters={"name": ["in", names], "docstatus": 0},
            pluck="name",
        )
        if drafts:
            frappe.throw(
                _(
                    "Cannot mark manifest {0} as Delivered: Stock Entry {1} "
                    "is still Draft, so the stock ledger has not recorded this "
                    "movement. Submit the Stock Entry (or remove the row) and retry."
                ).format(self.name, ", ".join(sorted(drafts))),
                title=_("Draft Stock Entry Blocks Delivery"),
            )

    def on_update(self):
        self._sync_package_items()
        # Previously only ran on_submit — but packing (and now, manifest
        # linking) is a Draft-only activity, so a Stock Entry added to a
        # still-Draft manifest's transfers never saw custom_transfer_manifest
        # set until submission, well after Pack Box needed it. Runs on every
        # save now so a manually created/edited Draft manifest links back
        # immediately.
        self._update_stock_entries_manifest()

    def _sync_package_items(self):
        """Persist a newly-packed box's item-level breakdown.

        A Table field nested inside a child row ("grandchild" table, here
        CH Transfer Package.items) is NOT something doc.save() walks
        recursively — only the manifest's own direct child tables
        (packages, transfers) get auto-persisted. doc.append("packages",
        {"items": [...]}) (used by both the pack_box API and the manifest
        form's Pack Box dialog) happily holds the item rows in memory
        through validate(), but they were silently dropped on save with
        no error — confirmed by testing pack_box end-to-end and finding
        the box's packed_qty total saved correctly while its item rows
        vanished. This writes them out explicitly now that each package
        has a stable name to parent them against.

        Only inserts for a package with NO existing item rows in the DB
        yet — never rewrites/deletes an already-packed box's items, since
        neither UI currently edits an existing box's contents after the
        fact (both only ever add a brand new box).
        """
        for p in self.packages or []:
            if not p.name:
                continue
            in_memory = [
                it for it in (p.get("items") or [])
                if (it.get("item_code") if isinstance(it, dict) else it.item_code)
            ]
            if not in_memory:
                continue
            if frappe.db.exists("CH Transfer Package Item", {"parent": p.name}):
                continue
            for idx, it in enumerate(in_memory, start=1):
                item_code = it.get("item_code") if isinstance(it, dict) else it.item_code
                qty = it.get("qty") if isinstance(it, dict) else it.qty
                frappe.get_doc({
                    "doctype": "CH Transfer Package Item",
                    "parent": p.name,
                    "parenttype": "CH Transfer Package",
                    "parentfield": "items",
                    "idx": idx,
                    "item_code": item_code,
                    "item_name": frappe.db.get_value("Item", item_code, "item_name"),
                    "qty": flt(qty),
                }).insert(ignore_permissions=True)

    def _ensure_qr_payload(self):
        """Mint the pickup-scan token at save time, not at driver assignment.

        Box labels are printed at pack time — before a driver is assigned — so
        a token minted only at assignment leaves the printed QR encoding the
        manifest name. ``start_pickup`` then mints a fresh token and
        auto-authorises whatever the driver scanned, which silently turns the
        scan gate into a no-op. Minting here keeps one stable token from the
        first save through pickup, so the printed label stays valid and the
        gate stays real.

        Draft-only by design: ``qr_payload`` is not ``allow_on_submit``, so
        assigning it here on a submitted manifest would trip Frappe's
        "not allowed to change after submit" guard. Submitted rows that predate
        this are covered by ``ensure_secure_qr_token()``, which uses ``db_set``
        and is safe post-submit.
        """
        if self.docstatus != 0:
            return
        token = (self.qr_payload or "").strip()
        if len(token) < 22 or token == self.name:
            self.qr_payload = frappe.generate_hash(length=32)

    def _validate_server_managed_fields(self):
        """Prevent ordinary CRUD from forging logistics lifecycle evidence."""
        from ch_logistics import roles as role_registry

        if role_registry.is_privileged() or self.flags.ignore_validate_update_after_submit:
            return
        if self.is_new():
            if self.get("status") not in (None, "", "Draft"):
                frappe.throw(_("New manifests must begin in Draft status."), frappe.PermissionError)
            forged = [
                fieldname
                for fieldname in self._SERVER_MANAGED_FIELDS - {"status"}
                if self.meta.has_field(fieldname) and self.get(fieldname) not in (None, "", 0)
            ]
        else:
            before = self.get_doc_before_save()
            if not before:
                return
            forged = [
                fieldname
                for fieldname in self._SERVER_MANAGED_FIELDS
                if self.meta.has_field(fieldname)
                and self.get(fieldname) != before.get(fieldname)
            ]
        if forged:
            frappe.throw(
                _("Lifecycle fields can only be changed through an authorized logistics action: {0}.")
                .format(", ".join(sorted(forged))),
                frappe.PermissionError,
            )

    def before_submit(self):
        if not self.transfers:
            frappe.throw(_("Add at least one Stock Entry to the manifest."), title=_("Ch Transfer Manifest Error"))
        # Oracle WMS gate: a manifest cannot transition Draft → Packed
        # until at least one carton/box has been recorded on the packing
        # slip.  This guarantees ``box_count`` is meaningful by the time
        # the driver picks up and lines up box labels with physical reality.
        if (not self.packages) and self._packing_required():
            frappe.throw(
                _("Add at least one packed box (use 'Pack Box' on the Packages tab) before submitting. " 
                  "Box count must be > 0 before dispatch."),
                title=_("Packing Slip Required"),
            )
        # SAP EWM / Oracle SIM parity — optional visual evidence of
        # packing quality (per-pack photo).  When the setting is on,
        # at least one packing photo must be attached before Packed.
        # Photos live either in the ``packing_photos`` child table (if
        # the site has adopted it) or as generic File attachments on
        # the manifest itself.
        if self._packing_photo_required() and not self._has_packing_photo():
            frappe.throw(
                _("Attach at least one packing photo before submitting. "
                  "See CH Logistics Settings → Require Packing Photo Before Dispatch."),
                title=_("Packing Photo Required"),
            )
        # Trip linking is optional at submit time — manifests can be submitted
        # standalone and attached to a trip via the Logistics Control Tower.
        # NOTE: linked Stock Entries are NOT pushed to custom_status="Packed"
        # here anymore — packing now lives entirely on the Stock Entry, and
        # every entry grouped into a manifest is already past that stage (at
        # "Ready For Pickup") by the time it gets here. Doing so used to
        # downgrade an already-correct status.
        if self.status in ("Draft", None, ""):
            self.status = "Packed"

    def _packing_required(self) -> bool:
        """Honour the global CH Logistics Settings → require_packing_slip flag.

        Defaults to ``False`` so existing manifests keep submitting; flip
        the setting on once warehouse staff have been trained to pack at
        a pack station, after which submission without a packing slip is
        rejected (matching Oracle WMS / Manhattan WMS behaviour).
        """
        try:
            return bool(frappe.db.get_single_value(
                "CH Logistics Settings", "require_packing_slip"
            ))
        except Exception:
            return False

    def _packing_photo_required(self) -> bool:
        """Honour the CH Logistics Settings → require_packing_photo flag."""
        try:
            return bool(frappe.db.get_single_value(
                "CH Logistics Settings", "require_packing_photo"
            ))
        except Exception:
            return False

    def _has_packing_photo(self) -> bool:
        """Return True when at least one packing photo is attached.

        Accepts any of:
          * A ``packing_photo`` on one of the manifest's cartons
            (``CH Transfer Package``), which is where the packing hub
            actually stores them and where the field is mandatory, OR
          * A ``packing_photos`` child table field on the manifest, OR
          * A generic image ``File`` attachment on the manifest doc.

        The carton branch is the one that matters and was missing: the check
        looked only at a ``packing_photos`` table the docstring itself called
        "upcoming" (it does not exist) and at File attachments. Turning
        require_packing_photo on would therefore have rejected manifests whose
        every carton carried a photo — the flag was unusable, not merely off.
        """
        for pkg in (self.packages or []):
            if (pkg.get("packing_photo") or "").strip():
                return True
        photos = getattr(self, "packing_photos", None)
        if photos:
            return True
        try:
            files = frappe.get_all(
                "File",
                filters={
                    "attached_to_doctype": self.doctype,
                    "attached_to_name": self.name,
                },
                fields=["file_name", "file_url", "is_private"],
                limit=25,
            )
        except Exception:
            return False
        image_exts = (".jpg", ".jpeg", ".png", ".webp", ".heic")
        for f in files:
            fname = (f.get("file_name") or "").lower()
            furl = (f.get("file_url") or "").lower()
            if any(fname.endswith(ext) or furl.endswith(ext) for ext in image_exts):
                return True
        return False

    def on_cancel(self):
        if not self.flags.get("stock_reversal_completed"):
            frappe.throw(
                _(
                    "Use the 'Cancel & Return Stock' action. A manifest cannot be "
                    "cancelled until every linked Stock Entry has been reversed."
                ),
                title=_("Controlled Cancellation Required"),
            )
        self.db_set("status", "Cancelled")
        self._maybe_auto_close_parent_trip()

    # ── Helpers ─────────────────────────────────────────────────────────

    def _populate_transfer_details(self):
        """Auto-fill warehouse, item count, MR link from each Stock Entry."""
        # Once a manifest has moved beyond Draft, the linked Stock Entries
        # may have been cancelled or even deleted (e.g. by a recall +
        # reverse-SE workflow that purges the original).  Tolerate that
        # case so legacy/submitted manifests can still be re-saved and
        # re-submitted for repairs (matches behaviour of SAP TM where
        # shipment lines stay queryable after their underlying GR is
        # cancelled).
        post_draft = (self.status or "Draft") not in ("Draft", "")
        # Collect every bad row instead of frappe.throw-ing mid-loop: a
        # multi-SE manifest used to abort at the FIRST invalid Stock Entry,
        # so later rows were never even looked at and the user discovered
        # problems one save at a time (and a partially-processed pass left
        # earlier rows populated while later ones stayed stale). One
        # aggregated refusal reports the whole picture in a single save.
        row_errors = []
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
                if post_draft:
                    # Legacy / post-recall row — keep whatever was stamped
                    # on the manifest line and move on.
                    continue
                row_errors.append(_("Row {0}: Stock Entry {1} not found.").format(row.idx, row.stock_entry))
                continue
            if se.docstatus == 2:
                if post_draft:
                    continue
                row_errors.append(_("Row {0}: Stock Entry {1} is cancelled.").format(row.idx, row.stock_entry))
                continue
            if se.stock_entry_type != "Material Transfer":
                row_errors.append(
                    _("Row {0}: Stock Entry {1} is not a Material Transfer (type: {2}).").format(
                        row.idx, row.stock_entry, se.stock_entry_type
                    )
                )
                continue
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

        if row_errors:
            frappe.throw(
                "<br>".join(row_errors),
                title=_("Ch Transfer Manifest Error"),
            )

    def _seed_manifest_stops(self):
        """Ensure a CH Transfer Manifest Stop exists for every from/to
        warehouse used by this manifest's transfer rows, and stamp each
        row's ``manifest_stop`` with the stop (by idx) its drop leg
        belongs to.

        Lets one manifest carry a multi-leg route instead of a single
        source/destination pair — e.g. Annanagar->Ashok Nagar and Ashok
        Nagar->Chennai Hub in the same manifest naturally produce 3 stops
        (Pickup@Annanagar, Pickup+Drop@Ashok Nagar — it's the drop for the
        first leg and the pickup for the second, so both roles merge into
        one stop — Drop@Chennai Hub), reusing a stop already at a given
        warehouse instead of creating a duplicate.

        Mirrors the exact hub-augmentation pattern already built for
        CH Logistics Trip (_seed_stops_from_manifests /
        auto_plan_trips's hub-stop insertion in
        ch_logistics.api.logistics_api) — only the doctype differs.

        Runs in validate(), not on_update(): unlike CH Transfer Package's
        grandchild items table, `stops` and `transfers` are both direct
        child tables of this manifest, so ordinary doc.save() persists
        them correctly — no post-save grandchild-sync workaround needed
        here. child rows have no `.name` until saved, so stops are
        referenced by `idx` (stable, since stops are only ever appended,
        never reordered or removed) rather than by name.
        """
        if not self.transfers:
            return

        by_warehouse = {}
        for s in self.stops or []:
            if s.warehouse:
                by_warehouse[s.warehouse] = s

        def _ensure_stop(warehouse, role):
            if not warehouse:
                return None
            existing = by_warehouse.get(warehouse)
            if existing:
                if existing.stop_type != role and existing.stop_type != "Pickup+Drop":
                    existing.stop_type = "Pickup+Drop"
                return existing
            new_stop = self.append("stops", {
                "sequence": len(self.stops or []) + 1,
                "warehouse": warehouse,
                "stop_type": role,
                "status": "Pending",
            })
            by_warehouse[warehouse] = new_stop
            return new_stop

        for row in self.transfers:
            if not row.from_warehouse and not row.to_warehouse:
                continue
            _ensure_stop(row.from_warehouse, "Pickup")
            drop_stop = _ensure_stop(row.to_warehouse, "Drop")
            if drop_stop:
                row.manifest_stop = drop_stop.idx

    def _compute_totals(self):
        self.total_stock_entries = len(self.transfers)
        self.total_items = sum(cint(r.item_count) for r in self.transfers)
        self.total_qty = sum(flt(r.total_qty) for r in self.transfers)
        # Oracle WMS pattern: box_count is auto-derived from the packing
        # slips (CH Transfer Package rows) — one row per physical carton.
        self._auto_label_packages()
        self.box_count = len(self.packages or [])
        self._compute_freight()

    def _validate_packing(self):
        """Reject packing slips whose combined ``packed_qty`` exceeds the
        manifest's ``total_qty``.

        This is the server-side source-of-truth guard: the pack-station
        dialog (both on the manifest form and in the Logistics Control
        Tower) does a client-side pre-check, but a determined user can
        bypass client validation by editing the packages child table
        directly on the form or by calling ``pack_box`` via the API.

        A packer physically cannot put more units into cartons than were
        picked into the manifest, so over-packing signals a data-entry
        mistake (typo, wrong manifest, missed decrement) that would
        otherwise silently corrupt the box-count / weight totals used
        downstream for freight billing and receiving reconciliation.

        Fires only when ``packages`` has rows so unpacked drafts still
        validate cleanly.
        """
        if not self.packages:
            return
        total_qty = flt(self.total_qty)
        if total_qty <= 0:
            return
        packed_sum = sum(flt(p.packed_qty) for p in self.packages)
        if packed_sum > total_qty:
            over = packed_sum - total_qty
            frappe.throw(
                _(
                    "Packed quantity ({0}) exceeds manifest total quantity ({1}) by {2}."
                    " Remove or reduce the overfilled box(es) on the Packages tab."
                ).format(packed_sum, total_qty, over),
                title=_("Overpacked Manifest"),
            )
        self._validate_package_items()

    def _validate_package_items(self):
        """Per-item mirror of _validate_packing's total-qty guard.

        A box's own ``packed_qty`` can be within the manifest total while
        still being wrong at the item level (e.g. packing 5 units of an
        item the manifest only carries 3 of, offset by under-packing some
        other item) — the aggregate check alone can't catch that. Reuses
        _get_manifest_pack_items()'s own packed/total per item rather than
        re-deriving it, so the two can never disagree.
        """
        for row in self._get_manifest_pack_items():
            if row["packed_qty"] > row["total_qty"]:
                over = row["packed_qty"] - row["total_qty"]
                frappe.throw(
                    _(
                        "Packed quantity of {0} ({1}) across all boxes exceeds the {2}"
                        " units of it on this manifest by {3}."
                    ).format(row["item_code"], row["packed_qty"], row["total_qty"], over),
                    title=_("Overpacked Item"),
                )

    def _get_manifest_pack_items(self):
        """Aggregate item_code -> {item_name, total_qty, packed_qty,
        remaining_qty} across every Stock Entry linked to this manifest,
        net of what's already recorded in existing package rows.

        Single source of truth for both the pack-station item picker
        (fetched via the whitelisted API wrapper) and this doctype's own
        _validate_package_items() guard — the two can never disagree about
        how much of an item is actually available to pack.

        CH Transfer Package.items is a "grandchild" table (a Table field
        nested inside a child row) — frappe.get_doc does NOT auto-load
        this onto self.packages[i].items the way it loads self.packages
        itself, so an already-saved package's item rows have to be
        queried explicitly by package name. Only a package still being
        built in THIS save (no name yet, or no DB rows for it yet) is
        read from the in-memory dict/row instead.
        """
        # Queried and folded into `totals` one Stock Entry at a time, in the
        # manifest's own transfers order (not an SE-name sort) — this is
        # the order the auto-split in pack_box() consumes items in when the
        # packer enters a bare Packed Qty total instead of picking items.
        se_names = [row.stock_entry for row in (self.transfers or []) if row.stock_entry]
        totals = {}
        for se_name in se_names:
            for row in frappe.get_all(
                "Stock Entry Detail",
                filters={"parent": se_name},
                fields=["item_code", "item_name", "qty"],
                order_by="idx",
            ):
                if not row.item_code:
                    continue
                bucket = totals.setdefault(row.item_code, {"item_name": row.item_name, "total_qty": 0})
                bucket["total_qty"] += flt(row.qty)

        package_names = [p.name for p in (self.packages or []) if p.name]
        db_rows_by_package = {}
        if package_names:
            for row in frappe.get_all(
                "CH Transfer Package Item",
                filters={"parent": ["in", package_names]},
                fields=["parent", "item_code", "qty"],
            ):
                db_rows_by_package.setdefault(row.parent, []).append(row)

        packed = {}
        for p in self.packages or []:
            rows = db_rows_by_package.get(p.name) if p.name else None
            if rows is None:
                rows = p.get("items") or []
            for it in rows:
                item_code = it.get("item_code") if isinstance(it, dict) else it.item_code
                qty = it.get("qty") if isinstance(it, dict) else it.qty
                if not item_code:
                    continue
                packed[item_code] = packed.get(item_code, 0) + flt(qty)

        result = []
        for item_code, info in totals.items():
            packed_qty = flt(packed.get(item_code, 0))
            result.append({
                "item_code": item_code,
                "item_name": info["item_name"],
                "total_qty": info["total_qty"],
                "packed_qty": packed_qty,
                "remaining_qty": max(info["total_qty"] - packed_qty, 0),
            })
        return result

    def _auto_label_packages(self):
        """Assign sequential LPN labels and stamp packer audit fields.

        Mirrors Oracle WMS's License Plate Number scheme: every physical
        box gets a unique label of the form ``{manifest}-B{NN}`` so that
        scanning the label at any downstream stop (loading, hand-off,
        receiving) unambiguously identifies the carton.
        """
        used = {
            (p.package_label or "").strip().upper()
            for p in (self.packages or [])
            if (p.package_label or "").strip()
        }
        next_seq = 1
        for p in (self.packages or []):
            if not (p.package_label or "").strip():
                while True:
                    candidate = f"{self.name or 'TM-NEW'}-B{next_seq:02d}"
                    if candidate.upper() not in used:
                        p.package_label = candidate
                        used.add(candidate.upper())
                        next_seq += 1
                        break
                    next_seq += 1
            if not p.packed_by:
                p.packed_by = frappe.session.user
            if not p.packed_at:
                p.packed_at = now_datetime()

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
            self._sync_custom_status_only("Assigned")
            if self._delivery_otp_required():
                self._generate_delivery_otp(request_source="Driver Assignment")
            # Seed the pickup-scan token so QR enforcement has something to
            # match against (older manifests are backfilled lazily here).
            if not self.qr_payload or self.qr_payload == self.name:
                self.qr_payload = frappe.generate_hash(length=32)
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
            if se.docstatus == 2:
                skipped += 1
                skipped_reasons.append(f"{se_name}: cancelled")
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
                # e-Waybill Log carries validity only — it has no status column,
                # and asking for one made the whole EWB summary raise.
                ewb_doc = frappe.db.get_value(
                    "e-Waybill Log",
                    {"name": ewb_no},
                    ["valid_upto"],
                    as_dict=True,
                ) or {}
                validity = ewb_doc.get("valid_upto")
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

    def start_pickup(self, pickup_photo, lat=None, lng=None, notes=None, scanned_qr=None,
                     gps_accuracy_m=None, geofence_override_reason=None):
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
            lat_f, lng_f = self._validate_geo(lat, lng, kind="pickup",
                                              accuracy_m=gps_accuracy_m,
                                              override_reason=geofence_override_reason)
            self._apply_pickup_in_transit_transition(pickup_photo, lat_f, lng_f, notes)
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def _apply_pickup_in_transit_transition(self, pickup_photo, lat_f, lng_f, notes=None):
        """Flip this manifest to In Transit and fire everything that follows
        from a completed pickup — the shared tail of ``start_pickup`` (whole-
        manifest QR/GPS/photo capture) AND of the per-leg Accept Shipment
        rollup (``driver_accept_manifest_row`` in ``logistics_api.py``, once
        every ``transfers`` row has individually captured its own pickup
        evidence). Both callers are expected to already hold this manifest's
        ``manifest_status_<name>`` lock and to have validated QR/photo/geo
        themselves — this method only applies the transition, it does not
        re-validate anything.
        """
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
        # Cascade to parent trip's pickup-type stop (Pending → Completed)
        self._cascade_stop_status_to_trip()
        # Proactive "out for delivery" to the destination store + track link.
        from ch_logistics.api.customer_tracking import notify_destination
        notify_destination(self.name, "out_for_delivery")

    def _scanned_matches_own_shipment(self, scanned):
        """True if ``scanned`` identifies a Stock Entry actually linked to
        THIS manifest — either its bare name, or a box label of the form
        ``{stock_entry}-B{NN}`` (see CH Stock Entry Box Label / pack_box_stock_entry).

        This is the fallback proof-of-possession for drivers who have the
        physical box in hand but not a separate printed manifest QR sheet —
        weaker than the random qr_payload token (a Stock Entry name is
        guessable if you already know the shipment), but still requires
        knowing which specific Stock Entries are on THIS exact manifest, not
        just any string, so it isn't a free pass around the scan requirement.
        """
        scanned = (scanned or "").strip()
        if not scanned:
            return False
        stock_entries = {row.stock_entry for row in (self.transfers or []) if row.stock_entry}
        if scanned in stock_entries:
            return True
        # Box label form: strip a trailing "-B<digits>" and compare what's left.
        base = re.sub(r"-B\d+$", "", scanned)
        return base in stock_entries

    def _validate_pickup_qr(self, scanned_qr):
        """Enforce the mandatory pickup scan (Ekart/Delhivery: every shipment is
        scanned at handover). The scanned payload must match this manifest's
        ``qr_payload`` token (or, for legacy rows, the manifest name), OR
        identify one of this manifest's own Stock Entries / box labels."""
        enforce = frappe.db.get_single_value("CH Logistics Settings", "enforce_pickup_qr")
        if enforce is not None and not int(enforce):
            return
        expected = (self.qr_payload or "").strip()
        scanned = (scanned_qr or "").strip()
        if not scanned:
            frappe.throw(_("QR scan is mandatory. Scan the manifest/order QR to start pickup."),
                         title=_("Scan Required"))
        if self._scanned_matches_own_shipment(scanned):
            return
        if len(expected) < 22 or expected == self.name:
            frappe.throw(_("This manifest is missing a secure QR token. Reassign it before pickup."),
                         title=_("QR Token Missing"))
        if not hmac.compare_digest(scanned, expected):
            frappe.throw(_("Scanned QR does not match this manifest."),
                         title=_("Wrong QR"))

    def _validate_pickup_qr_multi(self, stock_entry, scanned_list):
        """Pickup-scan enforcement for one Stock Entry leg that may have
        MULTIPLE physical boxes (CH Stock Entry Package) — every box's own
        label must appear somewhere in ``scanned_list``, not just any one of
        them, so a driver can't load 1 of 3 boxes and still mark the whole
        shipment picked up. A Stock Entry with 0 or 1 box falls back to the
        existing single-scan _validate_pickup_qr unchanged (its own
        qr_payload/shipment-name check already covers that case).
        """
        scanned_list = [(s or "").strip() for s in (scanned_list or []) if (s or "").strip()]
        box_labels = [
            b for b in frappe.get_all(
                "CH Stock Entry Package", filters={"parent": stock_entry}, pluck="package_label"
            ) if b
        ]
        if len(box_labels) <= 1:
            self._validate_pickup_qr(scanned_list[0] if scanned_list else "")
            return
        if not scanned_list:
            frappe.throw(
                _("Scan all {0} box QR codes for {1} before accepting.").format(
                    len(box_labels), stock_entry
                ),
                title=_("Scan Required"),
            )
        missing = [b for b in box_labels if b not in scanned_list]
        if missing:
            frappe.throw(
                _("{0} box(es) still need to be scanned for {1}: {2}").format(
                    len(missing), stock_entry, ", ".join(missing)
                ),
                title=_("Scan All Boxes"),
            )

    def _validate_delivery_qr_multi(self, stock_entry, scanned_list):
        """Delivery-side mirror of _validate_pickup_qr_multi — a Stock Entry
        leg with MULTIPLE physical boxes (CH Stock Entry Package) must have
        every box's own label scanned before delivery can be confirmed, not
        just any one of them (or the bare manifest QR). Without this, a
        driver carrying e.g. 3 boxes for one shipment could scan just 1 and
        still mark the whole thing Delivered while 2 boxes stayed on the
        truck — accept and deliver now enforce the same completeness
        guarantee. A Stock Entry with 0 or 1 box falls back to the existing
        single-scan _validate_delivery_qr unchanged.
        """
        scanned_list = [(s or "").strip() for s in (scanned_list or []) if (s or "").strip()]
        box_labels = [
            b for b in frappe.get_all(
                "CH Stock Entry Package", filters={"parent": stock_entry}, pluck="package_label"
            ) if b
        ]
        if len(box_labels) <= 1:
            self._validate_delivery_qr(scanned_list[0] if scanned_list else "")
            return

        enforce = frappe.db.get_single_value("CH Logistics Settings", "enforce_delivery_qr")
        if enforce is not None and not int(enforce):
            return

        if not scanned_list:
            frappe.throw(
                _("Scan all {0} box QR codes for {1} before delivering.").format(
                    len(box_labels), stock_entry
                ),
                title=_("Scan Required"),
            )
        missing = [b for b in box_labels if b not in scanned_list]
        if missing:
            frappe.throw(
                _("{0} box(es) still need to be scanned for {1}: {2}").format(
                    len(missing), stock_entry, ", ".join(missing)
                ),
                title=_("Scan All Boxes"),
            )

    def _validate_delivery_qr(self, scanned_qr):
        """Enforce the mandatory delivery scan (same handover ritual as pickup,
        on the receiver side). Gated by ``enforce_delivery_qr`` so it can be
        relaxed for last-mile B2C lanes that don't carry a returnable QR."""
        enforce = frappe.db.get_single_value("CH Logistics Settings", "enforce_delivery_qr")
        # Default ON when the flag has never been set (matches JSON default=1).
        if enforce is not None and not int(enforce):
            return
        expected = (self.qr_payload or "").strip()
        scanned = (scanned_qr or "").strip()
        if not scanned:
            frappe.throw(_("QR scan is mandatory. Scan the manifest/order QR to complete delivery."),
                         title=_("Scan Required"))
        if self._scanned_matches_own_shipment(scanned):
            return
        if len(expected) < 22 or expected == self.name:
            frappe.throw(_("This manifest is missing a secure QR token. Reassign it before delivery."),
                         title=_("QR Token Missing"))
        if not hmac.compare_digest(scanned, expected):
            frappe.throw(_("Scanned QR does not match this manifest."),
                         title=_("Wrong QR"))

    def ensure_secure_qr_token(self):
        """Mint a secure ``qr_payload`` when the stored one is missing, too
        short, or a legacy name-equals-token — i.e. exactly the cases the
        pickup/delivery QR validators reject (``len < 22`` or ``== name``).

        Returns ``(token, minted)``. Safe on submitted manifests (uses
        ``db_set`` and never bumps ``modified``). Centralises the lazy-mint
        that used to live only in the consolidated stop endpoints, so EVERY
        pickup/delivery path (bundle, per-manifest, desk) self-heals instead of
        dead-ending on "This manifest is missing a secure QR token."
        """
        token = (self.qr_payload or "").strip()
        if len(token) < 22 or token == self.name:
            token = frappe.generate_hash(length=32)
            self.db_set("qr_payload", token, update_modified=False)
            return token, True
        return token, False

    def _validate_geo(self, lat, lng, kind: str, accuracy_m=None, override_reason=None):
        """Mandatory driver-location proof for pickup/delivery, checked against
        the manifest header's own source/destination warehouse.

        Thin wrapper over :meth:`_validate_geo_for` — see there for the shared
        sanity checks and the per-location geofence delegate.
        """
        target_wh = self.source_warehouse if kind == "pickup" else self.destination_warehouse
        place = (self.source_store or self.source_warehouse) if kind == "pickup" \
            else (self.destination_store or self.destination_warehouse)
        return self._validate_geo_for(target_wh, place, lat, lng, kind,
                                      accuracy_m=accuracy_m, override_reason=override_reason)

    def _validate_geo_for(self, target_wh, place, lat, lng, kind: str, accuracy_m=None, override_reason=None):
        """Mandatory driver-location proof for pickup/delivery at an EXPLICIT
        location — lets a caller check a single Stock Entry leg's own
        from/to warehouse instead of always the manifest header's one
        source_warehouse/destination_warehouse (which is wrong for any leg
        whose own route differs, now that one manifest can carry several).

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
        # Geofence: the driver must physically be at the correct location — so a
        # parcel can't be picked up at the wrong source or delivered to the wrong
        # store even if the right QR is scanned.
        self._validate_geofence_for(target_wh, place, lat_f, lng_f, kind, accuracy_m=accuracy_m,
                                    override_reason=override_reason)
        return lat_f, lng_f

    def _validate_geofence(self, lat_f, lng_f, kind: str, accuracy_m=None,
                           override_reason=None):
        """Reject a pickup/arrival/delivery tap that is too far from the
        manifest header's own source/destination warehouse.

        Thin wrapper over :meth:`_validate_geofence_for` — see there for the
        actual distance/accuracy/override logic.
        """
        target_wh = self.source_warehouse if kind == "pickup" else self.destination_warehouse
        place = (self.source_store or self.source_warehouse) if kind == "pickup" \
            else (self.destination_store or self.destination_warehouse)
        self._validate_geofence_for(target_wh, place, lat_f, lng_f, kind,
                                    accuracy_m=accuracy_m, override_reason=override_reason)

    def _validate_geofence_for(self, target_wh, place, lat_f, lng_f, kind: str, accuracy_m=None,
                               override_reason=None):
        """Reject a pickup/arrival/delivery tap that is too far from an
        EXPLICIT expected warehouse/place (see :meth:`_validate_geo_for`).

        Three things this has to get right, none of which it did before:

        * **Fix quality.** A phone inside a warehouse routinely falls back to a
          network fix accurate to kilometres. Comparing that against a 300 m
          fence produces a confident "you are 1,840 m away" while the driver
          stands on the dock. The reported accuracy is now part of the
          comparison: the fence is widened by it, and a fix too coarse to
          decide anything is reported as such instead of being read as a
          location error.
        * **A way out.** There was none — the message said "report an issue if
          this is wrong" and no such path existed, so a bad fix stranded the
          driver mid-route. An explicit reason now overrides the distance check
          and is recorded against the manifest and its trip.
        * **Honest coverage.** A location with no coordinates still cannot be
          checked, but that now leaves an audit note rather than passing
          silently, so "geofencing is on" cannot be mistaken for
          "geofencing is enforced everywhere".
        """
        enforce = frappe.db.get_single_value("CH Logistics Settings", "enforce_geofence")
        if enforce is not None and not int(enforce):
            return
        if not target_wh:
            return
        try:
            from ch_logistics.api.optimizer import _warehouse_coords, haversine_km
            coords = _warehouse_coords(target_wh)
        except Exception:
            return
        if not coords:
            # Cannot compare — but say so, so an ungeocoded hub is visible as a
            # gap instead of looking like a location that passed the check.
            self._note_geofence_event(
                _("Geofence not applied at {0}: {1} has no coordinates.").format(kind, target_wh)
            )
            return

        radius_m = cint(frappe.db.get_single_value("CH Logistics Settings", "geofence_radius_m")) or 300

        try:
            acc_m = float(accuracy_m) if accuracy_m not in (None, "") else None
        except (TypeError, ValueError):
            acc_m = None
        if acc_m is not None and acc_m < 0:
            acc_m = None

        # A fix coarser than this cannot distinguish "at the dock" from "in the
        # next suburb", so treat it as a failed capture rather than a failed
        # location. Derived from the radius so tuning the fence tunes this too.
        max_usable_accuracy_m = radius_m * 3
        if acc_m is not None and acc_m > max_usable_accuracy_m and not override_reason:
            frappe.throw(
                _("Your device reported a location accurate only to {0} m, which is too "
                  "imprecise to confirm you are at {1}. Step outside for a clearer GPS "
                  "signal and retry.").format(int(acc_m), place),
                exc=GeofenceError,
                title=_("Location Too Imprecise"),
            )

        dist_m = haversine_km(lat_f, lng_f, coords[0], coords[1]) * 1000.0
        # Widen the fence by the fix's own margin of error: a reading 400 m out
        # with +/-300 m accuracy is not evidence the driver is elsewhere.
        allowance_m = radius_m + min(acc_m or 0.0, max_usable_accuracy_m)
        if dist_m <= allowance_m:
            return

        if override_reason and str(override_reason).strip():
            self._note_geofence_event(
                _("Geofence overridden at {0}: {1} m from {2} (limit {3} m). Reason: {4}")
                .format(kind, int(dist_m), place, int(allowance_m), str(override_reason).strip())
            )
            return

        action = _("pick up here") if kind == "pickup" else _("deliver here")
        frappe.throw(
            _("You are {0} m from {1} (must be within {2} m to {3}). "
              "Go to the correct location, or confirm with a reason if this is wrong.")
            .format(int(dist_m), place, int(allowance_m), action),
            exc=GeofenceError,
            title=_("Wrong Location"),
        )

    def _note_geofence_event(self, message: str):
        """Record a geofence override / gap against the manifest and its trip.

        Deliberately a comment rather than a CH Logistics Exception row: open
        exceptions block trip completion, and an override that silently stops
        the driver closing the trip an hour later is its own trap. The comment
        is attributed and timestamped, and mirroring it onto the trip is what
        puts it in front of ops.
        """
        try:
            self.add_comment("Comment", message)
            if self.get("trip"):
                frappe.get_doc("CH Logistics Trip", self.trip).add_comment("Comment", message)
        except Exception:
            # Audit must never be the reason a handover fails.
            frappe.log_error(frappe.get_traceback(), "geofence audit note failed")

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
            # Every linked Stock Entry moved into the transit ledger back at
            # Pending With Goods (custom stock-ledger management starts
            # there, well before a driver physically touches anything — see
            # TRANSIT_LEDGER_MANAGED_STATUSES in ch_erp15), so a pickup-time
            # rejection still has real stock sitting in the transit
            # warehouse that needs reversing, exactly like an in-transit
            # rejection does. reverse_transfer_to_source does that reversal
            # AND sets custom_status to "Rejected" in one call — replacing
            # the old logistics-status-only sync, which left the Stock
            # Entry's stock position wrong (nothing was ever moved back).
            from ch_erp15.ch_erp15.custom.stock_entry import reverse_transfer_to_source

            for row in self.transfers:
                reverse_transfer_to_source(
                    row.stock_entry,
                    reason=rejection_reason,
                    reference=self.name,
                    target_status="Rejected",
                )
            if during == "In Transit":
                # Goods were physically with the driver — still worth a trip
                # exception so the control tower sees the failed delivery
                # attempt the same way Delhivery / BlueDart / FedEx surface
                # "Delivery Exception", even though the stock is already
                # back on the ledger.
                self._raise_trip_exception_for_rejection(rejection_reason, rejection_notes)
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
            from ch_logistics.roles import (
                filter_notification_users,
                get_notification_role_users,
            )

            recipients.update(get_notification_role_users("rejection_dispatcher_notify"))
            recipients = filter_notification_users(recipients)
            if self.company:
                try:
                    from ch_erp15.ch_erp15.notification_router import (
                        filter_users_by_company,
                    )

                    recipients = filter_users_by_company(recipients, self.company)
                except Exception:
                    recipients = []
            subject = _("Manifest {0} rejected: {1}").format(self.name, self.rejection_reason)
            body = _("Driver {0} rejected manifest {1}. Reason: {2}.").format(
                self.driver_name or self.driver or "", self.name, self.rejection_reason)
            for user in recipients:
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

    def mark_reached_destination(self, lat, lng, gps_accuracy_m=None,
                                 geofence_override_reason=None):
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
            lat_f, lng_f = self._validate_geo(lat, lng, kind="arrival",
                                              accuracy_m=gps_accuracy_m,
                                              override_reason=geofence_override_reason)
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

    def _packed_seals(self) -> set[str]:
        """Seal / tamper-tag numbers recorded on this manifest's cartons."""
        return {
            str(p.seal_number).strip().upper()
            for p in (self.packages or [])
            if (p.seal_number or "").strip()
        }

    def _validate_seals(self, observed):
        """Chain-of-custody check: the seals that arrive must be the ones that left.

        A seal number was captured at packing and then never read again, so a
        swapped or cut tamper tag was invisible to the system — the one thing a
        seal exists to prove. GS1/CTPAT chain-of-custody and every carrier
        high-value flow verify the tag at each custody change, not just at
        origin.

        Self-activating by design: manifests whose cartons carry no seal are
        unaffected, so this needs no rollout flag and cannot block sites that
        do not seal. Where seals ARE recorded, delivery must account for every
        one of them.

        A mismatch is deliberately a hard stop rather than a warning. The
        driver's route out is the damage/tamper channel (``damage_reported``),
        which preserves the evidence instead of quietly accepting the box.
        """
        packed = self._packed_seals()
        if not packed:
            return
        if isinstance(observed, str):
            observed = re.split(r"[,\n;]+", observed)
        seen = {
            str(x).strip().upper()
            for x in (observed or [])
            if str(x or "").strip()
        }
        if not seen:
            frappe.throw(
                _("Seal verification is required: confirm tamper tag(s) {0} before "
                  "completing delivery.").format(", ".join(sorted(packed))),
                title=_("Seal Check Required"),
            )
        missing = packed - seen
        unexpected = seen - packed
        if missing or unexpected:
            details = []
            if missing:
                details.append(_("not presented: {0}").format(", ".join(sorted(missing))))
            if unexpected:
                details.append(_("not on the packing list: {0}").format(", ".join(sorted(unexpected))))
            frappe.throw(
                _("Seal mismatch at handover ({0}). Do not accept the consignment — "
                  "report it as damaged/tampered so the evidence is preserved.").format(
                      "; ".join(details)),
                title=_("Seal Mismatch"),
            )

    def complete_delivery(self, delivery_photo, receiver_name, otp=None,
                          lat=None, lng=None, scanned_qr=None,
                          otp_preverified=False, seal_numbers=None,
                          gps_accuracy_m=None, geofence_override_reason=None):
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
            # Chain of custody: the tamper tags that left must be the ones
            # presented here. No-op for manifests packed without seals.
            self._validate_seals(seal_numbers)
            # Mandatory driver GPS at the receiver's doorstep (proof of presence).
            lat_f, lng_f = self._validate_geo(lat, lng, kind="delivery",
                                              accuracy_m=gps_accuracy_m,
                                              override_reason=geofence_override_reason)
            # OTP verification is auditable. The active digest remains hashed
            # at rest; the linked log retains generation, dispatch, attempts,
            # expiry and verifier evidence after this one-time digest is cleared.
            if self.delivery_otp or self._delivery_otp_required():
                from ch_logistics.logistics.doctype.ch_logistics_otp_log.ch_logistics_otp_log import (
                    DeliveryOTPError,
                    verify_manifest_otp,
                )

                if not otp_preverified:
                    verification = verify_manifest_otp(self, otp)
                    if not verification.get("valid"):
                        raise DeliveryOTPError(verification.get("message") or _("Invalid delivery OTP."))
                self.delivery_otp_verified = 1
                self.delivery_otp = None

            self._apply_delivery_complete_transition(delivery_photo, lat_f, lng_f, receiver_name)
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def _apply_delivery_complete_transition(self, delivery_photo, lat_f, lng_f, receiver_name):
        """Flip this manifest to Delivered and fire everything that follows
        from a completed delivery — the shared tail of ``complete_delivery``
        (whole-manifest QR/OTP/GPS/photo capture) AND of the per-leg Deliver
        rollup (``driver_complete_delivery_row`` in ``transfer_manifest_api.py``,
        once every ``transfers`` row has individually captured its own
        delivery evidence). Both callers are expected to already hold this
        manifest's ``manifest_status_<name>`` lock and to have validated
        QR/OTP/photo/geo themselves — this method only applies the
        transition, it does not re-validate anything.
        """
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
        # Cascade to parent trip's drop stop (Pending → Completed)
        self._cascade_stop_status_to_trip()
        self._maybe_auto_close_parent_trip()
        from ch_logistics.api.customer_tracking import notify_destination
        notify_destination(self.name, "delivered")

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
            # Cascade to parent trip's drop stop (Pending → Completed), then let
            # the parent trip auto-close now that this shipment is settled
            # (Received). When it's the last outstanding shipment the trip
            # closes cleanly — the driver was already freed at delivery.
            self._cascade_stop_status_to_trip()
            self._maybe_auto_close_parent_trip()
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def close_manifest(self):
        if self.status != "Received":
            frappe.throw(_("Can only close when status is Received."), title=_("Ch Transfer Manifest Error"))
        self.status = "Closed"
        self.flags.ignore_validate_update_after_submit = True
        self.save()
        # Cascade to parent trip's drop stop (Pending → Completed) BEFORE
        # trip auto-close so the Trip Performance report sees the final
        # stop state even if trip closes in the same request.
        self._cascade_stop_status_to_trip()
        self._maybe_auto_close_parent_trip()
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

        Each Stock Entry submit runs inside its OWN database savepoint.
        Stock Entry submission posts Stock Ledger Entries partway through
        its submit chain, so a crash later in that chain used to strand
        already-posted SLE rows against a still-Draft Stock Entry once the
        surrounding request eventually committed — the Sep-01 go-live audit
        found 9 such wrecks (ledger rows created seconds into failed
        submits, voucher never advanced past docstatus=0). Rolling back to
        the savepoint on ANY exception guarantees a failed submit leaves
        zero trace, while successful sibling submits in the same pass are
        preserved.

        Failures surface on the manifest itself (timeline comment + red
        msgprint) instead of only via frappe.log_error: an Error Log row is
        invisible to the receiving team, and the old end-of-loop
        frappe.throw rolled the whole acceptance back anyway — taking the
        error evidence with it.
        """
        submitted = []
        errors = []
        for row in self.transfers:
            savepoint = f"ch_se_submit_{frappe.generate_hash(length=10)}"
            frappe.db.savepoint(savepoint)
            try:
                se_doc = frappe.get_doc("Stock Entry", row.stock_entry)
                if se_doc.docstatus != 0:
                    frappe.db.release_savepoint(savepoint)
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
                        detail = f"{se_doc.name}: {_si.item_code} needs {_si.qty}, available {_avail} in {_si.s_warehouse}"
                        frappe.log_error(detail, "Manifest SE Insufficient Stock")
                        errors.append(detail)
                        _ok = False
                        break
                if not _ok:
                    frappe.db.release_savepoint(savepoint)
                    continue  # skip this SE

                se_doc.flags.ignore_permissions = True
                # Same rationale as _create_reverse_se: this is a controlled,
                # system-generated submit fired only after the manifest's own
                # transit lifecycle completed (packed → picked → delivered →
                # accepted), which is precisely the visibility the ch_erp15
                # direct-Material-Transfer guardrail exists to protect. Without
                # this flag the guardrail refuses EVERY delivery-acceptance
                # auto-submit and GAP-2 can never post the receipt.
                se_doc.flags.ignore_procurement_guardrails = True
                se_doc.submit()
                frappe.db.release_savepoint(savepoint)
                submitted.append(row.stock_entry)
            except Exception as exc:
                # ATOMIC: throw away everything this submit attempt wrote
                # (SLEs, GL rows, serial moves, the docstatus flip itself)
                # so a half-submitted Stock Entry can never reach the
                # database. log_error AFTER the rollback so the log row
                # survives it.
                frappe.db.rollback(save_point=savepoint)
                if isinstance(exc, frappe.ValidationError):
                    # Swallowing a frappe.throw does NOT clear its
                    # message_log entry — without this, every caught
                    # validation failure would still pop up on the client
                    # alongside our consolidated msgprint below.
                    frappe.clear_last_message()
                errors.append(
                    f"{row.stock_entry}: {frappe.utils.escape_html(str(exc) or type(exc).__name__)}"
                )
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
            detail = "; ".join(errors)
            # Persist the failure on the manifest timeline AND tell the user.
            # Deliberately no frappe.throw here: throwing would roll back the
            # acceptance, the successful sibling submits and this very
            # comment, leaving the failure visible only in Error Log — the
            # exact silent wreck this method previously produced.
            self.add_comment(
                "Comment",
                _("Auto-submit FAILED for {0} Stock Entry row(s) on delivery acceptance: {1}. "
                  "These entries remain Draft and the stock ledger does not yet reflect "
                  "their receipt.").format(len(errors), detail),
            )
            frappe.msgprint(
                _(
                    "{0} linked Stock Entry row(s) could not be submitted and remain Draft: {1}. "
                    "The stock ledger does not reflect this receipt for those rows — fix the "
                    "underlying issue and submit them from the Stock Entry list."
                ).format(len(errors), detail),
                title=_("Stock Ledger Not Updated"),
                indicator="red",
            )

    def _create_landed_cost_voucher(self) -> None:
        """GAP-8: Distribute manifest freight cost across items via Landed Cost Voucher.

        Creates and submits an LCV linked to all submitted Stock Entries in this manifest.
        The LCV proportionally adjusts item valuation rates so the total landed
        cost (transfer price + freight) is correctly reflected in the stock ledger.
        """
        existing_lcv = frappe.db.get_value("CH Transfer Manifest", self.name, "custom_landed_cost_voucher")
        if existing_lcv:
            lcv_status = frappe.db.get_value("Landed Cost Voucher", existing_lcv, "docstatus")
            if lcv_status == 1:
                return
            if lcv_status == 0:
                lcv_doc = frappe.get_doc("Landed Cost Voucher", existing_lcv)
                lcv_doc.flags.ignore_permissions = True
                lcv_doc.submit()
                return
            frappe.throw(
                _("Linked Landed Cost Voucher {0} is cancelled. Clear it or create a new LCV before closing.").format(existing_lcv),
                title=_("Landed Cost Voucher Required"),
            )

        submitted_ses = [
            row.stock_entry for row in self.transfers
            if frappe.db.get_value("Stock Entry", row.stock_entry, "docstatus") == 1
        ]
        if not submitted_ses:
            frappe.throw(
                _("Freight is present, but no submitted Stock Entries are linked to this manifest. Receive stock first."),
                title=_("Landed Cost Voucher Required"),
            )

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
            if not freight_account:
                frappe.throw(
                    _("Set a freight account on the manifest or Default Expense Account on Company {0}.").format(self.company),
                    title=_("Freight Account Required"),
                )
            lcv.append("taxes", {
                "expense_account": freight_account,
                "description": _("Freight — manifest {0}").format(self.name),
                "amount": flt(self.freight_amount),
            })

            lcv.flags.ignore_permissions = True
            lcv.insert(ignore_permissions=True)
            lcv.submit()
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
            raise

    def _insert_system_delivery_claim(self, claim) -> None:
        previous_manifest = frappe.flags.get("ch_system_generated_delivery_claim")
        claim.flags.ch_system_generated_delivery_claim = self.name
        frappe.flags.ch_system_generated_delivery_claim = self.name
        try:
            claim.insert(ignore_permissions=True)
        finally:
            if previous_manifest is None:
                frappe.flags.pop("ch_system_generated_delivery_claim", None)
            else:
                frappe.flags.ch_system_generated_delivery_claim = previous_manifest

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
            self._insert_system_delivery_claim(claim)
            self.add_comment(
                "Comment",
                _("Damage claim {0} auto-created on delivery acceptance.").format(claim.name),
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Auto damage claim failed: {self.name}")
            raise

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
            self._insert_system_delivery_claim(claim)
            self.add_comment(
                "Comment",
                _("Shortage claim {0} auto-created — total {1} units short.").format(
                    claim.name, total_shortage
                ),
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Auto shortage claim failed: {self.name}")
            raise

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

    def _delivery_otp_required(self) -> bool:
        try:
            configured = frappe.db.get_single_value(
                "CH Logistics Settings", "enforce_delivery_otp"
            )
        except Exception:
            configured = None
        return configured is None or bool(cint(configured))

    def _generate_delivery_otp(self, request_source="Driver App", stop_sequence=None,
                               plaintext_otp=None):
        plaintext = str(
            plaintext_otp or (secrets.randbelow(900000) + 100000)
        ).strip()
        from ch_logistics.logistics.doctype.ch_logistics_otp_log.ch_logistics_otp_log import (
            issue_manifest_otp,
        )

        issue_manifest_otp(
            self,
            plaintext,
            request_source=request_source,
            stop_sequence=stop_sequence,
        )
        return plaintext

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

    def _sync_custom_status_only(self, target_status):
        """Push a manifest-status-driven value onto linked Stock Entries'
        ``custom_status`` only — unlike ``_sync_logistics_status_to_entries``,
        this does NOT touch ``custom_logistics_status`` (a separate, narrower
        courier-leg enum — Pending Pickup / Picked Up / In Transit /
        Delivered / Revert Requested / Reverted — that has no "Packed" or
        "Assigned" option and would reject either as an invalid Select value).

        Used for the two manifest states before any courier leg exists yet:
        Packed (submitted, not yet assigned to a driver) and Assigned
        (driver assigned, pickup not yet started). Before this, a Stock
        Entry just sat at whatever status it had when created through
        either of those states, with nothing reflecting that the manifest
        had actually progressed.
        """
        meta = frappe.get_meta("Stock Entry")
        if not meta.has_field("custom_status"):
            return
        for row in self.transfers:
            if not row.stock_entry:
                continue
            frappe.db.set_value(
                "Stock Entry", row.stock_entry, "custom_status", target_status,
                update_modified=False,
            )

    def _maybe_auto_close_parent_trip(self):
        """Advance the parent trip as its manifests settle.

        Two INDEPENDENT gates, using the trip's own canonical status sets so
        the two controllers can never disagree:

        * Completed — no manifest is still pre-delivery (every shipment has at
          least been Delivered — goods handed over).
        * Closed    — no manifest blocks closing. Per policy a Delivered
          shipment is enough: the driver's job ends at handover, so the trip
          closes right away. The destination store's Scan & Receive settles the
          manifest (posts stock) later, on its own time, decoupled from the
          trip. Only pre-delivery or Rejected shipments block closing.
        """
        trip_name = self.get("trip")
        if not trip_name:
            return
        from ch_logistics.logistics.doctype.ch_logistics_trip.ch_logistics_trip import (
            _MANIFEST_BLOCKS_TRIP_CLOSE,
            _MANIFEST_PREDELIVERY,
        )
        try:
            trip = frappe.get_doc("CH Logistics Trip", trip_name)
            if trip.status in ("Closed", "Cancelled"):
                return

            rows = frappe.get_all(
                "CH Transfer Manifest",
                filters={"trip": trip_name, "docstatus": ["<", 2]},
                fields=["name", "status"],
            )
            if not rows:
                return

            statuses = [(r.status or "Draft") for r in rows]
            # Completion gate — every shipment is at least Delivered.
            all_delivered = not any(s in _MANIFEST_PREDELIVERY for s in statuses)
            # Close gate — per policy the trip closes the moment goods are all
            # delivered; the store's Scan & Receive settles the manifest later
            # and must NOT hold the trip (or the driver) open. Only pre-delivery
            # or Rejected shipments block closing.
            blocks_close = any(s in _MANIFEST_BLOCKS_TRIP_CLOSE for s in statuses)

            if trip.status == "Started" and all_delivered:
                trip.mark_completed()
                trip.save(ignore_permissions=True)
                trip.reload()

            if not blocks_close and trip.status == "Completed":
                trip.mark_closed()
                trip.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"trip auto-close check failed from manifest {self.name}",
            )

    # ── Stop-status cascade (SAP TM parity) ────────────────────────────
    # SAP TM cascades Freight Order status down to Freight Unit stops
    # automatically; Oracle OTM cascades Trip status to Segments the same
    # way.  Without this, closing a manifest leaves the trip's Pickup /
    # Drop stop rows stuck on "Pending" (they only get flipped to
    # "Completed" when the driver taps the mobile app's stop_complete
    # button — a step field ops routinely forget).  This cascade runs on
    # every manifest state change that could advance a stop, so the
    # Logistics Command Center + Trip Performance report always show the
    # true operational state.
    _MANIFEST_PICKUP_DONE = {
        "In Transit", "Delivered", "Received", "Partially Received", "Closed",
    }
    _MANIFEST_DROP_DONE = {
        "Delivered", "Received", "Partially Received", "Closed",
    }
    _STOP_TERMINAL = {"Completed", "Skipped", "Exception"}

    def _cascade_stop_status_to_trip(self):
        """Advance the parent trip's stop statuses based on peer manifests.

        Two stops can serve one manifest: the PICKUP stop at its source and
        the DROP stop at its destination. ``stop_sequence`` records only the
        delivery side (forward trips), so the pickup stop must be resolved by
        matching the manifest's source store/warehouse against the trip's
        stops — otherwise a manifest-level ``start_pickup`` never completes
        the pickup stop, and the driver is shown a bare "Arrive" that skips
        straight to the scan-compliance prompt.

        - Pickup / Pickup+Drop stop at the source → Completed once every
          peer manifest picking up there reached ``_MANIFEST_PICKUP_DONE``.
        - Drop / Pickup+Drop stop at the destination → Completed once every
          peer manifest delivering there reached ``_MANIFEST_DROP_DONE``.

        Stops already in a terminal state (Completed / Skipped / Exception)
        are left alone so an explicit driver action wins over the cascade.
        """
        trip_name = self.get("trip")
        if not trip_name:
            return
        try:
            trip = frappe.get_doc("CH Logistics Trip", trip_name)
            if not trip.stops:
                return

            peers = frappe.get_all(
                "CH Transfer Manifest",
                filters={"trip": trip_name, "docstatus": ["<", 2]},
                fields=[
                    "name", "status", "stop_sequence",
                    "source_store", "source_warehouse",
                    "destination_store", "destination_warehouse",
                ],
            )
            if not peers:
                return

            wh_by_store = self._store_warehouse_map(trip.stops, peers)

            changed = False
            for stop in trip.stops:
                if (stop.status or "Pending") in self._STOP_TERMINAL:
                    continue
                stype = (stop.stop_type or "Drop").strip().lower()
                if stype == "pickup":
                    sides = [("source", self._MANIFEST_PICKUP_DONE)]
                elif stype == "pickup+drop":
                    sides = [
                        ("source", self._MANIFEST_PICKUP_DONE),
                        ("destination", self._MANIFEST_DROP_DONE),
                    ]
                else:
                    sides = [("destination", self._MANIFEST_DROP_DONE)]

                stop_peers = []
                required_by_name = {}
                for side, required in sides:
                    for p in peers:
                        if self._stop_serves_manifest(stop, p, side, wh_by_store):
                            stop_peers.append(p)
                            # Pickup+Drop: the stricter (drop) requirement wins
                            # when the same manifest matches both sides.
                            prev = required_by_name.get(p.name)
                            if prev is None or required is self._MANIFEST_DROP_DONE:
                                required_by_name[p.name] = required
                if not stop_peers:
                    continue
                all_done = all(
                    (p.status or "Draft") in required_by_name[p.name]
                    for p in stop_peers
                )
                if not all_done:
                    continue

                stop.status = "Completed"
                if not stop.ata:
                    stop.ata = now_datetime()
                changed = True

            if changed:
                trip.flags.ignore_validate_update_after_submit = True
                trip.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"trip stop cascade failed from manifest {self.name}",
            )

    @staticmethod
    def _store_warehouse_map(stops, manifests) -> dict:
        """CH Store → Warehouse for every store referenced by stops/manifests,
        so store-only and warehouse-only rows still match each other."""
        names = {s.store for s in stops if s.get("store")}
        for m in manifests:
            for key in ("source_store", "destination_store"):
                if m.get(key):
                    names.add(m[key])
        if not (names and frappe.db.exists("DocType", "CH Store")):
            return {}
        return {
            r.name: r.warehouse
            for r in frappe.get_all(
                "CH Store",
                filters={"name": ["in", list(names)]},
                fields=["name", "warehouse"],
            )
            if r.warehouse
        }

    @staticmethod
    def _stop_serves_manifest(stop, manifest, side, wh_by_store) -> bool:
        """True when `stop` is the pickup (side='source') or delivery
        (side='destination') location of `manifest`."""
        store = manifest.get(f"{side}_store")
        warehouse = manifest.get(f"{side}_warehouse")
        if side == "destination" and manifest.get("stop_sequence") \
                and cint(manifest.get("stop_sequence")) == cint(stop.sequence):
            return True
        if store and stop.get("store") and store == stop.get("store"):
            return True
        if warehouse and stop.get("warehouse") and warehouse == stop.get("warehouse"):
            return True
        if warehouse and stop.get("store") and wh_by_store.get(stop.get("store")) == warehouse:
            return True
        if store and stop.get("warehouse") and wh_by_store.get(store) == stop.get("warehouse"):
            return True
        return False

    # ── Recall / Reversal ───────────────────────────────────────────────

    def cancel_before_departure(self, reason):
        """Cancel a pre-departure manifest and atomically restore its stock.

        Once physical pickup has begun the accounting document must not be
        represented as if dispatch never happened; that path is a recall.
        """
        reason = str(reason or "").strip()
        if not reason:
            frappe.throw(_("Cancellation reason is mandatory."))

        lock_key = f"manifest_cancel_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(
                _("Manifest {0} is being cancelled by another user.").format(self.name)
            )
        try:
            current_status = frappe.db.get_value(
                "CH Transfer Manifest", self.name, "status"
            ) or "Draft"
            self.status = current_status
            allowed = ("Draft", "Packed", "Assigned")
            if self.status not in allowed or self.pickup_datetime:
                frappe.throw(
                    _(
                        "Manifest {0} has already entered physical movement "
                        "(status: {1}). Use Initiate Recall instead."
                    ).format(self.name, self.status),
                    title=_("Recall Required"),
                )

            reversed_ses = self._reverse_stock_entries(
                reason=reason,
                reference=f"manifest {self.name} pre-departure cancellation",
                allow_original_cancellation=True,
            )
            self.reversed_stock_entries = ", ".join(reversed_ses) if reversed_ses else "—"
            if cint(self.get("ewaybill_count")) or self.get("ewaybill_status") in (
                "Generated",
                "Partial",
            ):
                self.ewaybill_status = "Cancelled"
            self.cancellation_reason = reason
            self.cancelled_by = frappe.session.user
            self.cancelled_on = now_datetime()
            self.status = "Cancelled"
            self.flags.ignore_validate_update_after_submit = True

            if self.docstatus == 1:
                self.save(ignore_permissions=True)
                self.flags.stock_reversal_completed = True
                self.cancel()
            else:
                self.save(ignore_permissions=True)

            self.add_comment(
                "Comment",
                _(
                    "Manifest cancelled before departure by {0}. Reason: {1}. "
                    "Stock reversal: {2}"
                ).format(
                    self.cancelled_by,
                    reason,
                    self.reversed_stock_entries,
                ),
            )
            return reversed_ses
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def initiate_recall(self, reason, notes=None):
        """Store/warehouse manager initiates a transfer recall.

        Allowed from: Packed, Assigned, Pickup Started, In Transit, Delivered.
        Sends email + in-app notification to driver and store contacts.
        """
        lock_key = f"manifest_recall_{frappe.scrub(self.name)}"
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (lock_key,))[0][0]
        if not lock_result:
            frappe.throw(frappe._("Manifest {0} is being recalled by another user. Please refresh and try again.").format(self.name))
        try:
            allowed = ("Packed", "Assigned", "Pickup Started", "In Transit", "Delivered")
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

            # Freeze every linked custom transit Stock Entry before notifying
            # the driver. If any entry is already received, the whole request
            # rolls back and a new reverse transfer is required instead.
            from ch_erp15.ch_erp15.custom.stock_entry import request_transfer_return

            for row in self.transfers:
                request_transfer_return(
                    row.stock_entry,
                    reason=reason,
                    reference=f"manifest {self.name}",
                )

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

    def get_return_requirements(self):
        """Return ledger-derived quantities/IMEIs required at source."""
        from ch_erp15.ch_erp15.custom.stock_entry import get_transfer_return_inventory

        inventory = []
        for row in self.transfers:
            inventory.extend(get_transfer_return_inventory(row.stock_entry))
        serials = list(dict.fromkeys(
            serial_no
            for item in inventory
            for serial_no in item.get("serials", [])
        ))
        return {
            "items": inventory,
            "serials": serials,
            "serial_count": len(serials),
            "total_qty": sum(flt(item.get("qty")) for item in inventory),
        }

    @staticmethod
    def _parse_returned_serials(returned_serials):
        if returned_serials is None:
            return []
        if isinstance(returned_serials, str):
            value = returned_serials.strip()
            if value.startswith("["):
                returned_serials = frappe.parse_json(value)
            else:
                returned_serials = value.replace(",", "\n").splitlines()
        if not isinstance(returned_serials, (list, tuple)):
            frappe.throw(_("Returned IMEIs must be a list or one value per line."))
        serials = [str(value).strip() for value in returned_serials if str(value).strip()]
        if len(serials) != len(set(serials)):
            frappe.throw(
                _("The same returned IMEI was scanned more than once."),
                title=_("Duplicate Return Scan"),
            )
        return serials

    @staticmethod
    def _parse_returned_quantities(returned_quantities):
        if returned_quantities is None:
            return {}
        if isinstance(returned_quantities, str):
            returned_quantities = frappe.parse_json(returned_quantities)
        if not isinstance(returned_quantities, (list, tuple)):
            frappe.throw(_("Returned quantities must be a list of counted rows."))

        counts = {}
        for row in returned_quantities:
            row = frappe._dict(row or {})
            key = (str(row.stock_entry or "").strip(), str(row.row_name or "").strip())
            if not all(key):
                frappe.throw(_("Every returned quantity row requires its Stock Entry and row ID."))
            if key in counts:
                frappe.throw(
                    _("The same non-serialized return row was counted more than once."),
                    title=_("Duplicate Return Count"),
                )
            try:
                qty = float(row.qty)
            except (TypeError, ValueError):
                qty = -1
            if not math.isfinite(qty) or qty < 0:
                frappe.throw(_("Returned quantity must be a non-negative number."))
            counts[key] = qty
        return counts

    def confirm_return(
        self,
        return_photo,
        returned_serials=None,
        returned_quantities=None,
    ):
        """Delivery person confirms all items have been physically returned to source.

        Exact IMEI reconciliation is mandatory for serialized inventory. The
        manifest becomes Returned only after every stock reversal succeeds.
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

            requirements = self.get_return_requirements()
            expected = set(requirements["serials"])
            scanned_values = self._parse_returned_serials(returned_serials)
            scanned = set(scanned_values)
            if expected != scanned:
                missing = sorted(expected - scanned)
                unexpected = sorted(scanned - expected)
                details = []
                if missing:
                    details.append(_("missing: {0}").format(", ".join(missing)))
                if unexpected:
                    details.append(_("not on manifest: {0}").format(", ".join(unexpected)))
                frappe.throw(
                    _(
                        "Returned IMEI reconciliation failed ({0}). Scan every "
                        "serialized device physically received at the source."
                    ).format("; ".join(details) or _("scan mismatch")),
                    title=_("Return Scan Mismatch"),
                )

            expected_counts = {
                (str(row.get("stock_entry") or ""), str(row.get("row_name") or "")): flt(
                    row.get("qty")
                )
                for row in requirements["items"]
                if not row.get("serials") and flt(row.get("qty")) > 0
            }
            counted = self._parse_returned_quantities(returned_quantities)
            quantity_errors = []
            for key, expected_qty in expected_counts.items():
                if key not in counted:
                    quantity_errors.append(
                        _("{0}/{1}: count not entered").format(key[0], key[1])
                    )
                elif abs(counted[key] - expected_qty) > 0.000001:
                    quantity_errors.append(
                        _("{0}/{1}: expected {2}, counted {3}").format(
                            key[0], key[1], expected_qty, counted[key]
                        )
                    )
            unexpected_counts = sorted(set(counted) - set(expected_counts))
            quantity_errors.extend(
                _("{0}/{1}: row is not expected on this return").format(*key)
                for key in unexpected_counts
            )
            if quantity_errors:
                frappe.throw(
                    _(
                        "Non-serialized quantity reconciliation failed:<br>{0}"
                    ).format("<br>".join(quantity_errors)),
                    title=_("Return Count Mismatch"),
                )

            self.return_photo = return_photo
            self.return_confirmed_by = frappe.session.user
            self.return_confirmed_at = now_datetime()

            reversed_ses = self._reverse_stock_entries(
                reason=self.recall_reason or _("Manifest recall"),
                reference=f"manifest {self.name} confirmed return",
                allow_original_cancellation=False,
            )
            self.reversed_stock_entries = ", ".join(reversed_ses) if reversed_ses else "—"

            self.status = "Returned"
            self.flags.ignore_validate_update_after_submit = True
            # _reverse_stock_entries may have CANCELLED the linked SEs (the
            # clean-cancel path) — link validation would now refuse to save a
            # manifest referencing a cancelled document. That link is the
            # intended audit trail of the recall, so skip link validation for
            # this save only. Without this, confirm_return crashed whenever
            # the SE cancel succeeded (it only worked via the reverse-SE
            # fallback path).
            self.flags.ignore_links = True
            self.save()

            self.add_comment(
                "Comment",
                _(
                    "Return confirmed by {0} at {1}. IMEIs reconciled: {2}; "
                    "non-serialized rows counted: {3}. Stock reversed: {4}"
                ).format(
                    self.return_confirmed_by,
                    self.return_confirmed_at,
                    len(scanned),
                    len(counted),
                    self.reversed_stock_entries,
                ),
            )

            self._maybe_finalize_recalled_trip()
            self._maybe_auto_close_parent_trip()
            return reversed_ses
        finally:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))

    def _reverse_stock_entries(
        self,
        reason,
        reference=None,
        allow_original_cancellation=False,
    ):
        """Reverse every linked Stock Entry or fail the whole transaction.

        Custom CH transit entries use their own in-transit ledger and must be
        reversed through that service. Before departure, plain submitted
        ERPNext Material Transfers may use immutable-ledger cancellation.
        After departure, the original is never cancelled: a current-date
        compensating Stock Entry preserves the legal and accounting history.
        """
        from ch_erp15.ch_erp15.custom.stock_entry import reverse_transfer_to_source

        if allow_original_cancellation:
            # Do the statutory preflight for every document before touching
            # stock, so one non-cancellable e-Way Bill cannot leave a
            # multi-document manifest partially reversed.
            for row in self.transfers:
                se = frappe.get_doc("Stock Entry", row.stock_entry)
                custom_status = (se.get("custom_status") or "").strip()
                if se.docstatus == 1 and not custom_status:
                    self._validate_ewaybill_cancellation(se)

        results = []
        for row in self.transfers:
            se_name = row.stock_entry
            se = frappe.get_doc("Stock Entry", se_name)

            custom_action = reverse_transfer_to_source(
                se,
                reason=reason,
                reference=reference or f"manifest {self.name}",
            )
            if custom_action:
                results.append(custom_action)
                continue

            if se.docstatus == 2:
                results.append(f"{se_name} (already cancelled)")
                continue
            if se.docstatus == 0:
                results.append(f"{se_name} (no posted stock movement)")
                continue

            if not allow_original_cancellation:
                try:
                    reverse_name = self._create_reverse_se(se_name, reason=reason)
                    results.append(f"{se_name} → reverse {reverse_name}")
                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"Recall: could not reverse SE {se_name} for manifest {self.name}",
                    )
                    frappe.throw(
                        _(
                            "Compensating Stock Entry failed for {0}. Manifest "
                            "{1} remains in Recall Initiated; correct the stock "
                            "dependency and retry."
                        ).format(se_name, self.name),
                        title=_("Atomic Stock Reversal Failed"),
                    )
                continue

            ewaybill = (se.get("ewaybill") or "").strip()
            savepoint = f"manifest_reverse_{frappe.generate_hash(length=10)}"
            frappe.db.savepoint(savepoint)
            try:
                se.cancel()
                if ewaybill and frappe.db.get_value("Stock Entry", se_name, "ewaybill"):
                    frappe.throw(
                        _(
                            "e-Way Bill {0} is still active after cancellation "
                            "of Stock Entry {1}."
                        ).format(ewaybill, se_name),
                        title=_("e-Way Bill Cancellation Failed"),
                    )
                results.append(f"{se_name} (cancelled)")
                frappe.db.release_savepoint(savepoint)
            except Exception:
                frappe.db.rollback(save_point=savepoint)
                if ewaybill:
                    frappe.throw(
                        _(
                            "Stock Entry {0} was not cancelled because e-Way "
                            "Bill {1} could not be cancelled safely. Resolve it "
                            "in India Compliance and retry."
                        ).format(se_name, ewaybill),
                        title=_("Statutory Cancellation Blocked"),
                    )
                try:
                    reverse_name = self._create_reverse_se(se_name, reason=reason)
                    results.append(f"{se_name} → reverse {reverse_name}")
                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"Recall: could not reverse SE {se_name} for manifest {self.name}",
                    )
                    frappe.throw(
                        _(
                            "Stock reversal failed for {0}. Manifest {1} remains "
                            "unchanged; resolve the Stock Entry dependency and retry."
                        ).format(se_name, self.name),
                        title=_("Atomic Stock Reversal Failed"),
                    )
        return results

    @staticmethod
    def _validate_ewaybill_cancellation(stock_entry):
        """Block pre-departure cancellation unless its live EWB can be retired.

        India Compliance auto-cancels only within the statutory 24-hour
        window. Its default hook otherwise permits the accounting document to
        cancel while the portal EWB remains live, which is not acceptable for
        this controlled logistics action.
        """
        ewaybill = (stock_entry.get("ewaybill") or "").strip()
        if not ewaybill:
            return

        log = frappe.db.get_value(
            "e-Waybill Log",
            ewaybill,
            ["is_cancelled", "created_on"],
            as_dict=True,
        )
        if not log:
            frappe.throw(
                _(
                    "e-Way Bill {0} on Stock Entry {1} has no India Compliance "
                    "log. Synchronize the EWB before cancelling the manifest."
                ).format(ewaybill, stock_entry.name),
                title=_("e-Way Bill Audit Missing"),
            )
        if cint(log.is_cancelled):
            frappe.throw(
                _(
                    "e-Way Bill {0} is already cancelled on the portal but is "
                    "still linked to Stock Entry {1}. Refresh/synchronize the "
                    "Stock Entry before retrying."
                ).format(ewaybill, stock_entry.name),
                title=_("e-Way Bill Sync Required"),
            )

        try:
            from india_compliance.gst_india.utils import is_api_enabled

            settings = frappe.get_cached_doc("GST Settings")
        except (ImportError, frappe.DoesNotExistError):
            settings = None

        if not (
            settings
            and settings.enable_e_waybill
            and settings.auto_cancel_e_waybill
            and settings.reason_for_e_waybill_cancellation
            and is_api_enabled(settings)
        ):
            frappe.throw(
                _(
                    "Stock Entry {0} has active e-Way Bill {1}. Enable API "
                    "auto-cancellation and its cancellation reason in GST "
                    "Settings, or cancel the EWB manually before cancelling "
                    "this manifest."
                ).format(stock_entry.name, ewaybill),
                title=_("e-Way Bill Cancellation Required"),
            )

        if not log.created_on or add_to_date(
            get_datetime(log.created_on), days=1
        ) < now_datetime():
            frappe.throw(
                _(
                    "e-Way Bill {0} is outside the 24-hour cancellation window. "
                    "The manifest cannot be represented as cancelled; use the "
                    "documented statutory exception/return process."
                ).format(ewaybill),
                title=_("Statutory Cancellation Window Closed"),
            )

    def _create_reverse_se(self, se_name, reason=None):
        """Create a current-date compensating transfer (destination → source)."""
        from ch_erp15.ch_erp15.custom.stock_entry import _transfer_item_serials

        original = frappe.get_doc("Stock Entry", se_name)
        reverse = frappe.new_doc("Stock Entry")
        reverse.stock_entry_type = "Material Transfer"
        reverse.from_warehouse = original.to_warehouse
        reverse.to_warehouse = original.from_warehouse
        reverse.company = original.company
        if reverse.meta.has_field("custom_transfer_type"):
            reverse.custom_transfer_type = (
                original.get("custom_transfer_type") or "Warehouse Transfer"
            )
        reverse.remarks = _(
            "Compensating transfer for manifest {0}; original Stock Entry {1}. "
            "Reason: {2}"
        ).format(
            self.name, se_name, reason or self.recall_reason or _("Manifest reversal")
        )
        for item in original.items:
            source = item.t_warehouse or original.to_warehouse
            target = item.s_warehouse or original.from_warehouse
            values = {
                "item_code": item.item_code,
                "qty": item.qty,
                "uom": item.uom,
                "batch_no": item.batch_no,
                "s_warehouse": source,
                "t_warehouse": target,
            }
            serials = _transfer_item_serials(item)
            if serials:
                values["serial_no"] = "\n".join(serials)
            reverse.append("items", values)
        reverse.insert(ignore_permissions=True)
        # This is the controlled system-generated compensating document. The
        # normal UI guard correctly blocks ad-hoc direct Material Transfers.
        reverse.flags.ignore_procurement_guardrails = True
        reverse.submit()
        return reverse.name

    def _maybe_finalize_recalled_trip(self):
        """Cancel an aborted trip only after all manifests are reconciled."""
        trip_name = self.get("trip")
        if not trip_name:
            return
        trip = frappe.get_doc("CH Logistics Trip", trip_name)
        if not trip.get("cancellation_reason") or trip.status in ("Closed", "Cancelled"):
            return
        rows = frappe.get_all(
            "CH Transfer Manifest",
            filters={"trip": trip_name, "docstatus": ["<", 2]},
            fields=["name", "status"],
        )
        blocking = [
            row.name
            for row in rows
            if (row.status or "Draft") not in ("Returned", "Cancelled")
        ]
        if blocking:
            return
        trip.mark_cancelled_after_recall()
        trip.save(ignore_permissions=True)
        if trip.driver:
            from ch_logistics.api.logistics_api import _set_driver_availability
            _set_driver_availability(trip.driver, "Available", None)

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
            # CH Store holds no email address; the recall notice is addressed to
            # the store's managers.
            store_user = None
            try:
                from ch_erp15.ch_erp15.store_request_api import _get_store_managers
                managers = _get_store_managers(store_name) or []
                store_user = managers[0] if managers else None
            except Exception:
                store_user = None
            if not store_user:
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

            recipients = [store_user]

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

@frappe.whitelist(methods=["POST"])
def refresh_ewaybill_summary(manifest):
    """Pull the latest EWB numbers / validity off each linked Stock Entry,
    update the cached summary on the manifest, and return the structured list.

    Called by the manifest form's "Refresh e-Way Bills" button and by the
    background poller after enqueueing generations.
    """
    if not manifest:
        frappe.throw(_("Manifest name is required."))
    from ch_logistics import roles as role_registry
    from ch_logistics import scope_guard

    role_registry.require("ewaybill_sync", _("refresh e-Way Bills"))
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    scope_guard.assert_manifest_scope(doc.as_dict(), side="source")
    return doc.refresh_ewaybill_summary()


@frappe.whitelist(methods=["POST"])
def resync_ewaybills(manifest):
    """Manually re-run EWB sync for a manifest (e.g. after addresses are
    corrected, or a failed job is retried). Restricted to users who can
    write to the manifest."""
    if not manifest:
        frappe.throw(_("Manifest name is required."))
    from ch_logistics import roles as role_registry
    from ch_logistics import scope_guard

    role_registry.require("ewaybill_sync", _("resync e-Way Bills"))
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    scope_guard.assert_manifest_scope(doc.as_dict(), side="source")
    if doc.status not in ("Assigned", "Pickup Started", "In Transit"):
        frappe.throw(_("e-Way Bills can only be (re)synced once the driver is Assigned."))
    if not doc.vehicle_number:
        frappe.throw(_("Vehicle Number is missing — cannot sync e-Way Bills."))
    doc._sync_ewaybills_for_transfers()
    return doc.refresh_ewaybill_summary()
