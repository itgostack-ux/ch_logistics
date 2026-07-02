"""
Transfer Manifest API — Whitelisted endpoints.

Used by both the CH Transfer Manifest form buttons and the Delivery App page.
"""

import hashlib
import hmac
import json as _json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime, cint, flt


# ── Role gating (Phase B — Outward / Inward governance) ─────────────────────
# Maps each transition to the roles allowed to perform it. System Manager
# always bypasses. Keep in sync with the role list in ch_transfer_manifest.json.
_STAGE_ROLES = {
    # Outward stages — source-store dispatch lane
    "assign_driver":     {"Delivery Manager", "Stock Manager", "Store Manager"},
    "start_pickup":      {"Delivery Manager", "Delivery User", "Stock Manager"},
    "mark_reached_destination": {"Delivery Manager", "Delivery User", "Stock Manager"},
    "reject_manifest":   {"Delivery Manager", "Delivery User", "Stock Manager"},
    "complete_delivery": {"Delivery User", "Delivery Manager"},
    "driver_close_manifest": {"Delivery User", "Delivery Manager"},
    # Inward stages — destination-store receipt lane
    "accept_delivery":   {"Store Manager", "Stock Manager"},
    "close_manifest":    {"Store Manager", "Stock Manager"},
    # Reversal — manager-only
    "initiate_recall":   {"Stock Manager", "Delivery Manager", "Store Manager"},
    "confirm_return":    {"Stock Manager", "Delivery Manager"},
}


def _require_stage_role(stage: str) -> None:
    """Raise PermissionError if current user lacks any role required for `stage`.

    System Manager and Administrator always bypass.
    """
    user = frappe.session.user
    if user == "Administrator":
        return
    user_roles = set(frappe.get_roles(user))
    if "System Manager" in user_roles:
        return
    needed = _STAGE_ROLES.get(stage, set())
    if not needed:
        return
    if user_roles & needed:
        return
    frappe.throw(
        _("You do not have the required role to <b>{0}</b>. Required: {1}").format(
            stage.replace("_", " ").title(),
            ", ".join(sorted(needed)),
        ),
        frappe.PermissionError,
        title=_("Transfer Manifest — Role Required"),
    )


# ── Manifest CRUD ────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_manifest(stock_entries, source_warehouse=None, destination_warehouse=None,
                    source_store=None, destination_store=None) -> dict:
    """Create a manifest from a list of Stock Entry names (comma-separated or list)."""
    if isinstance(stock_entries, str):
        stock_entries = [s.strip() for s in stock_entries.split(",") if s.strip()]

    if not stock_entries:
        frappe.throw(_("No Stock Entries provided."), title=_("API Error"))

    # Infer warehouses from first Stock Entry if not supplied
    if not source_warehouse or not destination_warehouse:
        se = frappe.get_doc("Stock Entry", stock_entries[0])
        source_warehouse = source_warehouse or se.from_warehouse
        destination_warehouse = destination_warehouse or se.to_warehouse

    doc = frappe.new_doc("CH Transfer Manifest")
    doc.source_warehouse = source_warehouse
    doc.destination_warehouse = destination_warehouse
    doc.source_store = source_store
    doc.destination_store = destination_store

    for se_name in stock_entries:
        doc.append("transfers", {"stock_entry": se_name})

    doc.insert()
    return doc.name


@frappe.whitelist()
def get_manifest(manifest) -> dict:
    """Return full manifest doc as dict."""
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("read")
    return doc.as_dict()


@frappe.whitelist()
def pack_box(manifest, packed_qty, weight_kg=None, dimensions_cm=None,
             seal_number=None, packing_photo=None, notes=None) -> dict:
    """Add one carton (LPN) to a Draft manifest's packing slip.

    Used by the Logistics Command Center "Packing" hub so a packer can
    mint cartons without opening each manifest form.  The LPN label and
    packed_by / packed_at audit fields are auto-stamped by the manifest
    controller via _auto_label_packages() on save.
    """
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    if doc.docstatus != 0:
        frappe.throw(frappe._("Packing can only be added while the manifest is Draft."))
    try:
        packed_qty_int = int(packed_qty)
    except (TypeError, ValueError):
        frappe.throw(frappe._("Packed quantity is required."))
    if packed_qty_int <= 0:
        frappe.throw(frappe._("Packed quantity must be greater than zero."))

    # Pre-flight overpack guard so the user sees a clean, contextual message
    # BEFORE save() runs. The manifest controller's _validate_packing() is the
    # source-of-truth guard (covers direct form saves + this API path); this
    # check only exists to give the pack-station a nicer error string.
    total_qty = float(doc.total_qty or 0)
    if total_qty > 0:
        packed_so_far = sum(float(p.packed_qty or 0) for p in (doc.packages or []))
        remaining = total_qty - packed_so_far
        if packed_qty_int > remaining:
            frappe.throw(
                frappe._(
                    "Cannot pack {0} units — only {1} remaining on manifest {2}"
                    " (total {3}, already packed {4})."
                ).format(
                    packed_qty_int,
                    max(remaining, 0),
                    doc.name,
                    int(total_qty) if total_qty.is_integer() else total_qty,
                    int(packed_so_far) if packed_so_far.is_integer() else packed_so_far,
                ),
                title=frappe._("Overpack Blocked"),
            )

    doc.append("packages", {
        "packed_qty": packed_qty_int,
        "weight_kg": weight_kg or None,
        "dimensions_cm": dimensions_cm or None,
        "seal_number": seal_number or None,
        "packing_photo": packing_photo or None,
        "notes": notes or None,
    })
    doc.save()
    last = doc.packages[-1] if doc.packages else None
    return {
        "name": doc.name,
        "box_count": doc.get("box_count"),
        "package_label": last.package_label if last else None,
        "packed_qty": last.packed_qty if last else 0,
    }


# ── Status Transitions ──────────────────────────────────────────────────────

@frappe.whitelist()
def assign_driver(manifest, driver, courier_partner=None, vehicle_number=None,
                  tracking_number=None, estimated_delivery_date=None,
                  vehicle=None, external_booking_id=None) -> dict:
    _require_stage_role("assign_driver")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    doc.assign_driver(
        driver=driver,
        courier_partner=courier_partner,
        vehicle_number=vehicle_number,
        tracking_number=tracking_number,
        estimated_delivery_date=estimated_delivery_date,
        vehicle=vehicle,
        external_booking_id=external_booking_id,
    )
    _send_delivery_otp(doc)
    return {"status": doc.status, "vehicle": doc.get("custom_vehicle"), "delivery_otp": doc.get("delivery_otp")}


@frappe.whitelist()
def start_pickup(manifest, pickup_photo, lat=None, lng=None, notes=None,
                 scanned_qr=None) -> dict:
    _require_stage_role("start_pickup")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    doc.start_pickup(pickup_photo=pickup_photo, lat=lat, lng=lng, notes=notes,
                     scanned_qr=scanned_qr)
    return {"status": doc.status}


@frappe.whitelist()
def reject_manifest(manifest, rejection_reason, rejection_photo,
                    rejection_notes=None) -> dict:
    """Driver rejects a pickup that cannot be completed (FR-022 → FR-027)."""
    _require_stage_role("reject_manifest")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    doc.reject_manifest(
        rejection_reason=rejection_reason,
        rejection_photo=rejection_photo,
        rejection_notes=rejection_notes,
    )
    return {"status": doc.status}


@frappe.whitelist()
def bulk_reject_other_assignments(accepted_manifest, rejection_reason,
                                  rejection_photo, rejection_notes=None) -> dict:
    """Driver accepts one manifest and rejects all the others assigned to them.

    Standard handover-pool pattern used by Swiggy / Zomato / Dunzo / Ekart
    driver apps: a driver is offered a batch of orders, picks one, and
    bounces the rest back to dispatch so they can be re-routed.

    Scope:
      * If the accepted manifest is attached to a trip, only manifests on
        the *same* trip in status ``Assigned`` are rejected.
      * Otherwise, every ``Assigned`` manifest currently sitting on the
        logged-in driver is rejected.

    Each rejection runs through ``reject_manifest`` so the same reason +
    photo + dispatcher notification + stock-state revert path is used. We
    never silently bulk-update behind the controller.

    Returns: {accepted, rejected: [names], skipped: [{name, reason}]}.
    """
    _require_stage_role("reject_manifest")
    if not rejection_reason:
        frappe.throw(_("Rejection reason is required."), title=_("API Error"))
    if not rejection_photo:
        frappe.throw(_("Rejection proof photo is required."), title=_("API Error"))

    accepted_doc = frappe.get_doc("CH Transfer Manifest", accepted_manifest)
    accepted_doc.check_permission("read")
    driver = accepted_doc.driver
    if not driver:
        frappe.throw(_("Accepted manifest has no driver."), title=_("API Error"))

    # Build the sibling pool: same driver, same trip if any, status Assigned.
    filters = {
        "name": ["!=", accepted_manifest],
        "driver": driver,
        "docstatus": 1,
        "status": "Assigned",
    }
    trip = accepted_doc.get("trip")
    if trip:
        filters["trip"] = trip

    siblings = frappe.get_all("CH Transfer Manifest", filters=filters, pluck="name")

    rejected, skipped = [], []
    for name in siblings:
        try:
            sib = frappe.get_doc("CH Transfer Manifest", name)
            sib.check_permission("write")
            sib.reject_manifest(
                rejection_reason=rejection_reason,
                rejection_photo=rejection_photo,
                rejection_notes=rejection_notes,
            )
            rejected.append(name)
        except Exception as exc:
            # Don't let one bad sibling abort the whole batch — surface
            # which ones failed so the driver app can flag them.
            skipped.append({"name": name, "reason": str(exc)})
            frappe.log_error(title=f"bulk_reject skip {name}",
                             message=frappe.get_traceback())

    return {
        "accepted": accepted_manifest,
        "rejected": rejected,
        "skipped": skipped,
        "scope": "trip" if trip else "driver",
    }


@frappe.whitelist()
def mark_reached_destination(manifest, lat, lng) -> dict:
    """Driver \"Reached Location\" ping at the destination.

    Captures arrival GPS + timestamp on the manifest while keeping status
    at 'In Transit'. ``complete_delivery`` is gated on this ping, so the
    Complete Delivery dialog cannot open until the driver has confirmed
    arrival at the receiver.
    """
    _require_stage_role("mark_reached_destination")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    info = doc.mark_reached_destination(lat=lat, lng=lng)
    return {
        "status": doc.status,
        "arrival_datetime": info.get("arrival_datetime"),
        "message": _("Arrival recorded. You can now Complete Delivery."),
    }


@frappe.whitelist()
def complete_delivery(manifest, delivery_photo, receiver_name, otp=None,
                      lat=None, lng=None, scanned_qr=None) -> dict:
    _require_stage_role("complete_delivery")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    doc.complete_delivery(
        delivery_photo=delivery_photo,
        receiver_name=receiver_name,
        otp=otp, lat=lat, lng=lng,
        scanned_qr=scanned_qr,
    )
    return {"status": doc.status}


@frappe.whitelist()
def accept_delivery(manifest, damage_reported=0, damage_notes=None,
                    damage_photo=None, received_lines=None) -> dict:
    _require_stage_role("accept_delivery")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    doc.accept_delivery(
        damage_reported=cint(damage_reported),
        damage_notes=damage_notes,
        damage_photo=damage_photo,
        received_lines=received_lines,
    )
    return {
        "status": doc.status,
        "shortage_total": sum(
            (r.get("custom_shortage_qty") or 0) for r in doc.transfers
        ),
    }


@frappe.whitelist()
def close_manifest(manifest) -> dict:
    _require_stage_role("close_manifest")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    doc.close_manifest()
    return {"status": doc.status}


@frappe.whitelist()
def driver_close_manifest(manifest, close_note=None) -> dict:
    """Driver-friendly manifest closure.

    Allows the assigned driver to close a manifest from Delivered state to
    keep the mobile workflow concise. When closing from Delivered we preserve
    the side-effects of ``close_manifest`` (trip auto-close, freight posting,
    LCV creation) without forcing destination receipt actions on the driver.
    """
    _require_stage_role("driver_close_manifest")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")

    from ch_logistics.api.driver_resolver import resolve_current_driver
    current_driver = resolve_current_driver(throw=False, auto_provision_admin=False)
    if current_driver and doc.driver and doc.driver != current_driver:
        frappe.throw(_("You can only close manifests assigned to your driver profile."))

    if doc.status == "Closed":
        return {"status": doc.status, "trip": doc.trip}

    if doc.status in ("Received", "Partially Received"):
        doc.close_manifest()
        return {"status": doc.status, "trip": doc.trip}

    if doc.status != "Delivered":
        frappe.throw(
            _("Manifest can be closed from Delivered/Received states only (current: {0}).").format(doc.status)
        )

    doc.status = "Closed"
    doc.flags.ignore_validate_update_after_submit = True
    doc.save()
    if close_note:
        doc.add_comment("Comment", _("Driver close note: {0}").format(close_note))
    # Cascade to parent trip stop (Pending → Completed) before auto-close.
    doc._cascade_stop_status_to_trip()
    doc._maybe_auto_close_parent_trip()
    if flt(doc.freight_amount) > 0 and not doc.freight_journal_entry:
        doc._post_freight_gl()
    if flt(doc.freight_amount) > 0:
        doc._create_landed_cost_voucher()
    return {"status": doc.status, "trip": doc.trip}


# ── Recall / Reversal ────────────────────────────────────────────────────────

@frappe.whitelist()
def initiate_recall(manifest, reason, notes=None) -> dict:
    """Initiate a transfer recall. Notifies driver and stores via email + in-app.

    Allowed statuses: Packed, Assigned, In Transit, Delivered.
    """
    if not reason:
        frappe.throw(_("Recall reason is required."), title=_("API Error"))
    _require_stage_role("initiate_recall")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    doc.initiate_recall(reason=reason, notes=notes)
    return {
        "status": doc.status,
        "recall_initiated_at": str(doc.recall_initiated_at),
        "message": _("Transfer recall initiated. Driver and stores have been notified."),
    }


@frappe.whitelist()
def confirm_return(manifest, return_photo, confirmed_by=None) -> dict:
    """Delivery person confirms all items returned to source warehouse.

    Cancels/reverses the underlying Stock Entries to reinstate stock.
    Status → Returned.
    """
    if not return_photo:
        frappe.throw(_("Return photo is required."), title=_("API Error"))
    _require_stage_role("confirm_return")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    reversed_ses = doc.confirm_return(
        return_photo=return_photo,
        confirmed_by=confirmed_by,
    )
    return {
        "status": doc.status,
        "return_confirmed_at": str(doc.return_confirmed_at),
        "reversed_stock_entries": reversed_ses,
        "message": _("Return confirmed. Stock has been reversed to source warehouse."),
    }


@frappe.whitelist()
def resend_otp(manifest) -> dict:
    """Regenerate and send OTP to destination store manager."""
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    if doc.status not in ("Assigned", "In Transit"):
        frappe.throw(_("OTP can only be resent in Assigned/In Transit status."), title=_("API Error"))
    doc._generate_delivery_otp()
    doc.flags.ignore_validate_update_after_submit = True
    doc.save()
    frappe.db.commit()

    _send_delivery_otp(doc)
    return {"message": _("OTP sent to destination store contact.")}


def _split_contact_values(raw_value: str | None) -> list[str]:
    """Split comma/semicolon/newline-separated contact values into clean tokens."""
    if not raw_value:
        return []
    normalized = str(raw_value).replace(";", ",").replace("\n", ",")
    return [v.strip() for v in normalized.split(",") if v and v.strip()]


def _uniq_keep_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for v in values or []:
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _resolve_destination_pos_profile(destination_store: str | None) -> str | None:
    """Resolve destination POS Profile from CH Store first, then known mappings."""
    if not destination_store:
        return None

    # Canonical mapping: CH Store.pos_profile
    pos_profile = frappe.db.get_value("CH Store", destination_store, "pos_profile")
    if pos_profile:
        return pos_profile

    # Backward-compat mapping via POS Profile Extension
    if frappe.db.exists("DocType", "POS Profile Extension"):
        pos_profile = frappe.db.get_value(
            "POS Profile Extension", {"store": destination_store, "disabled": 0}, "pos_profile"
        )
        if pos_profile:
            return pos_profile

    # Legacy custom field fallback: POS Profile.custom_store
    pos_meta = frappe.get_meta("POS Profile")
    if pos_meta.has_field("custom_store"):
        return frappe.db.get_value(
            "POS Profile", {"custom_store": destination_store, "disabled": 0}, "name"
        )

    return None


def _collect_pos_profile_contacts(pos_profile: str | None) -> tuple[list[str], list[str]]:
    """Collect POS Profile contact email/mobile values (site-specific custom fields)."""
    if not pos_profile:
        return [], []

    meta = frappe.get_meta("POS Profile")

    email_fields = [
        "custom_store_email",
        "custom_contact_email",
        "custom_manager_email",
        "custom_delivery_otp_email",
        "contact_email",
        "email_id",
    ]
    mobile_fields = [
        "custom_cug_mobile",
        "custom_store_mobile",
        "custom_contact_mobile",
        "custom_manager_mobile",
        "custom_delivery_otp_mobile",
        "custom_store_phone",
        "mobile_no",
        "phone",
        "contact_phone",
    ]

    # Some deployments store store manager as User Link/email in this field.
    manager_field = "custom_store_manager"

    emails = []
    mobiles = []

    for fieldname in email_fields:
        if not meta.has_field(fieldname):
            continue
        raw = frappe.db.get_value("POS Profile", pos_profile, fieldname)
        for val in _split_contact_values(raw):
            if "@" in val:
                emails.append(val)

    for fieldname in mobile_fields:
        if not meta.has_field(fieldname):
            continue
        raw = frappe.db.get_value("POS Profile", pos_profile, fieldname)
        for val in _split_contact_values(raw):
            mobiles.append(val)

    if meta.has_field(manager_field):
        mgr = frappe.db.get_value("POS Profile", pos_profile, manager_field)
        if mgr:
            if "@" in str(mgr):
                emails.append(str(mgr).strip())
            elif frappe.db.exists("User", mgr):
                mgr_email = frappe.db.get_value("User", mgr, "email")
                mgr_mobile = frappe.db.get_value("User", mgr, "mobile_no")
                if mgr_email:
                    emails.append(mgr_email)
                if mgr_mobile:
                    mobiles.append(mgr_mobile)

    return _uniq_keep_order(emails), _uniq_keep_order(mobiles)


def _collect_store_manager_contacts(destination_store: str | None) -> tuple[list[str], list[str], list[str]]:
    """Collect manager users + email/mobile from CH Store user mappings."""
    from ch_erp15.ch_erp15.store_request_api import _get_store_managers

    users = _uniq_keep_order(_get_store_managers(destination_store) if destination_store else [])
    emails = []
    mobiles = []
    for user in users:
        email = frappe.db.get_value("User", user, "email")
        mobile = frappe.db.get_value("User", user, "mobile_no")
        if email:
            emails.append(email)
        if mobile:
            mobiles.append(mobile)
    return users, _uniq_keep_order(emails), _uniq_keep_order(mobiles)


def _collect_warehouse_contacts(warehouse: str | None) -> tuple[list[str], list[str]]:
    """Return (emails, mobiles) configured on the destination Warehouse.

    Honours two ERPNext contact-binding patterns so a warehouse can be
    notified independently of any POS profile / store manager mapping:

      1. Direct Warehouse fields — ``email_id``, ``phone_no``, ``mobile_no``.
      2. Linked Contact docs (the standard "Address and Contact" panel
         on the Warehouse form). Contacts are bound to Warehouse via
         Dynamic Link, exactly the same pattern ERPNext uses for
         Customer / Supplier / Warehouse. Each Contact may carry
         multiple Contact Email / Contact Phone rows — we honour every
         entry and de-dupe at the end.
    """
    if not warehouse:
        return [], []

    emails: list[str] = []
    mobiles: list[str] = []

    # 1) Direct Warehouse fields (legacy / single-recipient setups)
    row = frappe.db.get_value(
        "Warehouse", warehouse,
        ["email_id", "phone_no", "mobile_no"],
        as_dict=True,
    ) or {}
    emails.extend(e for e in _split_contact_values(row.get("email_id")) if "@" in e)
    mobiles.extend(_split_contact_values(row.get("mobile_no")))
    mobiles.extend(_split_contact_values(row.get("phone_no")))

    # 2) Linked Contact docs (the "Address and Contact" panel pattern).
    #    Standard ERPNext binding: Dynamic Link rows on Contact pointing
    #    at link_doctype="Warehouse", link_name=<warehouse>.
    try:
        contact_names = frappe.get_all(
            "Dynamic Link",
            filters={
                "link_doctype": "Warehouse",
                "link_name": warehouse,
                "parenttype": "Contact",
            },
            pluck="parent",
        )
        for cname in contact_names:
            # Use cached doc — Contact is a small doctype and we want every
            # email_ids / phone_nos row.
            try:
                contact = frappe.get_cached_doc("Contact", cname)
            except Exception:
                continue
            for e in (contact.email_ids or []):
                addr = (e.email_id or "").strip()
                if addr and "@" in addr:
                    emails.append(addr)
            for p in (contact.phone_nos or []):
                num = (p.phone or "").strip()
                if num:
                    mobiles.append(num)
    except Exception:
        # Never let a contact-lookup failure block the OTP path — direct
        # warehouse fields above are still honoured.
        frappe.log_error(
            frappe.get_traceback(),
            f"Manifest OTP — linked Contact lookup failed for warehouse {warehouse!r}",
        )

    return _uniq_keep_order(emails), _uniq_keep_order(mobiles)


def _send_delivery_otp(doc) -> dict:
    """Send delivery OTP to the connected destination warehouse + store contacts.

    Recipient order (highest priority first):
      1. Destination Warehouse contacts (email_id / phone_no / mobile_no)
         — the canonical \"connected warehouse\" address.
      2. CH Store manager User mappings.
      3. Destination POS Profile contacts (including site CUG fields).
      4. CH Store.contact_phone (SMS fallback).

    Returns the recipient summary so callers can echo it back to the
    driver app (\"OTP sent to ops@warehouse.com\").
    """
    manager_users = []
    manager_emails = []
    manager_mobiles = []

    if doc.destination_store:
        try:
            manager_users, manager_emails, manager_mobiles = _collect_store_manager_contacts(
                doc.destination_store
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Manifest OTP — store managers lookup failed")

    pos_profile = _resolve_destination_pos_profile(doc.destination_store)
    profile_emails, profile_mobiles = _collect_pos_profile_contacts(pos_profile)

    warehouse_emails, warehouse_mobiles = _collect_warehouse_contacts(doc.destination_warehouse)

    store_phone = frappe.db.get_value("CH Store", doc.destination_store, "contact_phone") if doc.destination_store else None

    # Warehouse contacts go first — they're the canonical \"connected warehouse\" address
    # for this manifest and the user's explicit choice for delivery handoff.
    email_recipients = _uniq_keep_order(warehouse_emails + manager_emails + profile_emails)
    sms_recipients = _uniq_keep_order(
        warehouse_mobiles + manager_mobiles + profile_mobiles + ([store_phone] if store_phone else [])
    )

    if not manager_users and not email_recipients and not sms_recipients:
        frappe.log_error(
            f"OTP for manifest {doc.name} could not be sent — no destination store contact.",
            "Manifest OTP Delivery",
        )
        return {"emails": [], "mobiles": [], "manager_users": []}

    subject = _("Delivery OTP for Manifest {0}").format(doc.name)
    manifest_url = frappe.utils.get_url_to_form("CH Transfer Manifest", doc.name)
    company_name = doc.company or "Congruence Holdings"
    message = _(
        "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:680px;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden'>"
        "<div style='background:#0f172a;color:#ffffff;padding:12px 16px;font-weight:600'>{company_name} - Transfer Delivery OTP</div>"
        "<div style='padding:16px'><p>A delivery is on its way to your store.</p>"
        "<table style='border-collapse:collapse;font-size:14px'>"
        "<tr><td style='padding:6px;font-weight:bold'>Manifest</td>"
        "<td style='padding:6px'>{manifest}</td></tr>"
        "<tr><td style='padding:6px;font-weight:bold'>Driver</td>"
        "<td style='padding:6px'>{driver}</td></tr>"
        "<tr><td style='padding:6px;font-weight:bold'>Items</td>"
        "<td style='padding:6px'>{items} lines / {qty} units</td></tr>"
        "</table>"
        "<p style='font-size:20px;font-weight:bold;letter-spacing:4px'>"
        "OTP: {otp}</p>"
        "<p>Share this OTP with the driver only after physically verifying all items.</p>"
        "<p style='margin-top:16px'><a href='{manifest_url}' style='background:#0b57d0;color:#ffffff;text-decoration:none;padding:10px 14px;border-radius:6px;display:inline-block;font-weight:600'>Open Manifest</a></p>"
        "</div></div>"
    ).format(
        company_name=company_name,
        manifest=doc.name,
        driver=doc.driver_name or doc.driver or "—",
        items=doc.total_items or 0,
        qty=doc.total_qty or 0,
        otp=doc.delivery_otp,
        manifest_url=manifest_url,
    )

    for user in manager_users:
        try:
            frappe.publish_realtime(
                event="notification",
                message={
                    "subject": subject,
                    "message": _("OTP {0} for manifest {1}. Share only after item verification.").format(
                        doc.delivery_otp, doc.name
                    ),
                    "type": "info",
                    "from_user": frappe.session.user,
                },
                user=user,
            )
        except Exception:
            pass

    if sms_recipients:
        try:
            from frappe.core.doctype.sms_settings.sms_settings import send_sms
            sms_message = _(
                "CH Logistics: Delivery OTP {0} for Manifest {1}. Share only after item verification."
            ).format(doc.delivery_otp, doc.name)
            send_sms(sms_recipients, sms_message)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Manifest OTP SMS failed: {doc.name}")

    try:
        default_outgoing = frappe.db.get_value(
            "Email Account", {"default_outgoing": 1, "enable_outgoing": 1}, "name"
        )
        if default_outgoing and email_recipients:
            frappe.sendmail(
                recipients=email_recipients,
                subject=subject,
                message=message,
                reference_doctype="CH Transfer Manifest",
                reference_name=doc.name,
                delayed=False,
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Manifest OTP email failed: {doc.name}")

    return {
        "emails": email_recipients,
        "mobiles": sms_recipients,
        "manager_users": manager_users,
    }


@frappe.whitelist()
def request_delivery_otp(manifest) -> dict:
    """Driver-side trigger: 'I'm at the destination, send me the OTP'.

    Wired to the **Complete Delivery** button on the driver app: tapping it
    regenerates a fresh OTP, emails / SMSes it to the connected destination
    warehouse (plus store manager + POS profile contacts), then returns the
    masked recipient list so the driver UI can confirm where the code went.

    This is operationally critical: the OTP generated at assignment time can
    be hours stale and the warehouse staff who actually open the door are
    not always copied on the initial dispatch. Carrier apps (Delhivery,
    BlueDart, Ekart, FedEx) all generate the receiver code on driver
    arrival rather than dispatch.
    """
    _require_stage_role("complete_delivery")
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("write")
    if doc.status != "In Transit":
        frappe.throw(
            _("OTP can only be requested while the manifest is In Transit (current: {0}).")
            .format(doc.status),
            title=_("API Error"),
        )
    doc._generate_delivery_otp()
    doc.flags.ignore_validate_update_after_submit = True
    doc.save()
    frappe.db.commit()

    recipients = _send_delivery_otp(doc) or {}

    # Mask emails so the UI can show "o***@warehouse.com" without leaking
    # full addresses to whoever happens to look over the driver's shoulder.
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
        "message": _("OTP sent to the destination warehouse."),
        "masked_emails": [_mask_email(e) for e in recipients.get("emails", [])],
        "masked_mobiles": [_mask_mobile(m) for m in recipients.get("mobiles", [])],
        "email_count": len(recipients.get("emails", [])),
        "sms_count": len(recipients.get("mobiles", [])),
    }


def _extract_tracking_status(payload):
    """Extract a human-readable shipment status from common courier API payload shapes."""
    if isinstance(payload, dict):
        for key in ("status", "current_status", "shipment_status", "tracking_status", "latest_status", "state"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("data", "result", "shipment", "tracking", "response", "latest_event"):
            value = payload.get(key)
            if isinstance(value, dict):
                status = _extract_tracking_status(value)
                if status:
                    return status
            elif isinstance(value, list):
                for row in reversed(value):
                    status = _extract_tracking_status(row)
                    if status:
                        return status
    return None


def _map_courier_status(status_text):
    text = (status_text or "").strip().lower()
    if not text:
        return None
    if any(token in text for token in ("delivered", "delivery completed", "pod", "received by consignee")):
        return "Delivered"
    if any(token in text for token in ("in transit", "out for delivery", "shipped", "dispatched", "picked up", "pickup completed")):
        return "In Transit"
    if any(token in text for token in ("assigned", "booked", "manifested", "scheduled")):
        return "Assigned"
    return None


def _fetch_partner_tracking_payload(courier_doc, tracking_number):
    import requests

    if not courier_doc.api_base_url:
        return {}

    url = courier_doc.api_base_url.strip()
    params = {}
    if "{tracking_number}" in url:
        url = url.replace("{tracking_number}", tracking_number)
    else:
        params["tracking_number"] = tracking_number

    headers = {"Accept": "application/json"}
    if courier_doc.api_key:
        headers["Authorization"] = f"Bearer {courier_doc.api_key}"
        headers["X-API-Key"] = courier_doc.api_key

    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    try:
        return response.json()
    except Exception:
        return {"status": response.text[:140]}


@frappe.whitelist()
def poll_courier_statuses(dry_run=0) -> dict:
    """Poll enabled courier partners and sync the latest manifest delivery status."""
    dry_run = cint(dry_run)
    manifests = frappe.get_all(
        "CH Transfer Manifest",
        filters={
            "docstatus": 1,
            "status": ["in", ["Assigned", "In Transit", "Delivered"]],
            "courier_partner": ["is", "set"],
            "tracking_number": ["is", "set"],
        },
        fields=["name", "status", "courier_partner", "tracking_number", "modified"],
        order_by="modified asc",
        limit=200,
    )

    result = {
        "checked": len(manifests),
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": dry_run,
        "details": [],
    }

    for row in manifests:
        detail = {
            "manifest": row.name,
            "courier_partner": row.courier_partner,
            "tracking_number": row.tracking_number,
            "current_status": row.status,
        }
        try:
            courier_doc = frappe.get_cached_doc("Courier Partner", row.courier_partner)
            if not cint(courier_doc.api_enabled) or not courier_doc.api_base_url:
                result["skipped"] += 1
                detail["reason"] = "API not configured"
                result["details"].append(detail)
                continue

            if dry_run:
                detail["reason"] = "eligible"
                result["details"].append(detail)
                continue

            payload = _fetch_partner_tracking_payload(courier_doc, row.tracking_number)
            external_status = _extract_tracking_status(payload)
            mapped_status = _map_courier_status(external_status)
            detail["external_status"] = external_status

            if mapped_status and mapped_status != row.status:
                doc = frappe.get_doc("CH Transfer Manifest", row.name)
                doc.flags.ignore_validate_update_after_submit = True
                doc.db_set("status", mapped_status, update_modified=True)
                doc._sync_logistics_status_to_entries(mapped_status)
                try:
                    doc.add_comment(
                        "Comment",
                        _("Courier update from {0}: {1}").format(row.courier_partner, external_status or mapped_status),
                    )
                except Exception:
                    pass
                detail["updated_to"] = mapped_status
                result["updated"] += 1
            else:
                result["skipped"] += 1
                detail["reason"] = "No status change"
        except Exception as e:
            result["errors"] += 1
            detail["error"] = str(e)
            frappe.log_error(frappe.get_traceback(), f"Courier polling failed for manifest {row.name}")

        result["details"].append(detail)

    if result["updated"] and not dry_run:
        frappe.db.commit()

    return result


# ── Delivery App Endpoints ───────────────────────────────────────────────────

@frappe.whitelist()
def get_driver_assignments() -> list:
    """Return active manifests for the logged-in driver.

    Includes every status the driver still owns work on:
      Assigned        — pending pickup
      Pickup Started  — in handover (defensive, transient state)
      In Transit      — on the way to receiver
      Delivered       — dropped off but receiver hasn't accepted yet,
                        driver may still need to follow up / collect POD

    Each row carries a ``bucket`` field so the driver app can group them
    under \"To Pick Up\", \"In Transit\", and \"Awaiting Receipt\".
    """
    user = frappe.session.user
    user_roles = frappe.get_roles(user)
    is_ops = bool({"System Manager", "Delivery Manager", "Delivery User"} & set(user_roles))

    # Resolve current user → Driver via the shared chain (User.user, then
    # Employee.user_id → Driver.employee, then Administrator auto-provision).
    from ch_logistics.api.driver_resolver import resolve_current_driver
    driver = resolve_current_driver(throw=False)

    filters = {"docstatus": 1}
    if driver:
        filters["driver"] = driver
    elif is_ops:
        pass  # ops roles see all manifests
    else:
        return []

    active_statuses = ["Assigned", "Pickup Started", "In Transit", "Delivered"]
    filters["status"] = ["in", active_statuses]

    fields = [
        "name", "status", "source_warehouse", "destination_warehouse",
        "source_store", "destination_store",
        "driver_name", "driver_phone",
        "total_stock_entries", "total_items", "total_qty",
        "estimated_delivery_date", "creation",
        "trip",
    ]
    if frappe.db.has_column("CH Transfer Manifest", "arrival_datetime"):
        fields.append("arrival_datetime")

    manifests = frappe.get_all(
        "CH Transfer Manifest",
        filters=filters,
        fields=fields,
        order_by="creation desc",
        limit=100,
    )

    bucket_by_status = {
        "Assigned": "to_pickup",
        "Pickup Started": "to_pickup",
        "In Transit": "in_transit",
        "Delivered": "awaiting_receipt",
    }
    for m in manifests:
        m["bucket"] = bucket_by_status.get(m.get("status"), "to_pickup")
        if m.get("source_store"):
            m["source_address"] = frappe.db.get_value(
                "CH Store", m["source_store"], "address") or ""
        if m.get("destination_store"):
            m["destination_address"] = frappe.db.get_value(
                "CH Store", m["destination_store"], "address") or ""

    return manifests


@frappe.whitelist()
def get_delivery_history() -> list:
    """Return recently delivered/received manifests for current driver."""
    user = frappe.session.user
    user_roles = frappe.get_roles(user)
    is_ops = bool({"System Manager", "Delivery Manager", "Delivery User"} & set(user_roles))

    from ch_logistics.api.driver_resolver import resolve_current_driver
    driver = resolve_current_driver(throw=False)

    filters = {
        "docstatus": 1,
        "status": ["in", ["Delivered", "Received", "Closed"]],
    }
    if driver:
        filters["driver"] = driver
    elif is_ops:
        pass
    else:
        return []

    return frappe.get_all(
        "CH Transfer Manifest",
        filters=filters,
        fields=[
            "name", "status", "source_warehouse", "destination_warehouse",
            "source_store", "destination_store",
            "total_stock_entries", "total_items", "total_qty",
            "delivery_datetime", "received_datetime",
        ],
        order_by="modified desc",
        limit=20,
    )


@frappe.whitelist()
def get_manifest_detail(manifest) -> dict:
    """Return manifest detail for the delivery app."""
    doc = frappe.get_doc("CH Transfer Manifest", manifest)
    doc.check_permission("read")

    result = doc.as_dict()
    # Add Stock Entry item details
    items = []
    for row in doc.transfers:
        se_items = frappe.get_all(
            "Stock Entry Detail",
            filters={"parent": row.stock_entry},
            fields=["item_code", "item_name", "qty", "serial_no", "batch_no"],
        )
        items.append({
            "stock_entry": row.stock_entry,
            "from_warehouse": row.from_warehouse,
            "to_warehouse": row.to_warehouse,
            "items": se_items,
        })
    result["transfer_items_detail"] = items

    # Store addresses
    if doc.source_store:
        result["source_address"] = frappe.db.get_value("CH Store", doc.source_store, "address") or ""
    if doc.destination_store:
        result["destination_address"] = frappe.db.get_value("CH Store", doc.destination_store, "address") or ""

    return result


# ── Operations Hub Integration ───────────────────────────────────────────────

@frappe.whitelist()
def get_manifest_queue(tab="active", warehouse="") -> list:
    """Return manifest list for Ops Hub integration."""
    filters = {"docstatus": 1}

    if tab == "active":
        filters["status"] = ["in", ["Packed", "Assigned", "In Transit"]]
    elif tab == "delivered":
        filters["status"] = ["in", ["Delivered", "Received"]]
    elif tab == "closed":
        filters["status"] = "Closed"
    elif tab == "all":
        pass

    if warehouse:
        filters["source_warehouse"] = warehouse

    return frappe.get_all(
        "CH Transfer Manifest",
        filters=filters,
        fields=[
            "name", "status", "source_warehouse", "destination_warehouse",
            "source_store", "destination_store",
            "driver_name", "courier_partner",
            "total_stock_entries", "total_items", "total_qty",
            "estimated_delivery_date", "creation", "modified",
        ],
        order_by="creation desc",
        limit=100,
    )


# ── Courier Push Webhook Receiver ────────────────────────────────────────────

@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=300, seconds=60, methods=["POST"], ip_based=True)
def receive_courier_webhook(courier_partner: str, payload: str = None) -> dict:
    """Receive push delivery status updates from courier partners.

    Courier partners must POST to:
      /api/method/ch_erp15.ch_erp15.transfer_manifest_api.receive_courier_webhook
      ?courier_partner=<name>

        HMAC-SHA256 signature:
      Header: X-Signature: sha256=<hex_digest>
      Body: raw JSON payload

    Payload must contain a tracking_number and a status field.
    """
    # --- HMAC verification ---
    courier_doc = frappe.get_cached_doc("Courier Partner", courier_partner)
    if not courier_doc.api_key:
        frappe.response["http_status_code"] = 401
        frappe.log_error(
            title="Courier Webhook",
            message=f"Courier partner {courier_partner!r} has no API key configured.",
        )
        return {"error": "Webhook signature is not configured"}

    sig_header = frappe.request.headers.get("X-Signature") or ""
    raw_body = frappe.request.get_data(as_text=True) or ""
    expected = "sha256=" + hmac.new(
        courier_doc.api_key.encode("utf-8"),
        raw_body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig_header, expected):
        frappe.response["http_status_code"] = 401
        return {"error": "Invalid signature"}

    # --- Parse payload ---
    if payload is None:
        raw = frappe.request.get_data(as_text=True) or "{}"
        try:
            data = _json.loads(raw)
        except Exception:
            frappe.response["http_status_code"] = 400
            return {"error": "Invalid JSON"}
    else:
        data = _json.loads(payload) if isinstance(payload, str) else payload

    tracking_number = (
        data.get("tracking_number")
        or data.get("awb")
        or data.get("waybill")
        or data.get("tracking_id")
        or ""
    )
    if not tracking_number:
        frappe.response["http_status_code"] = 400
        return {"error": "tracking_number missing from payload"}

    external_status = _extract_tracking_status(data)
    mapped_status = _map_courier_status(external_status)

    # Find manifest by tracking number
    manifest_name = frappe.db.get_value(
        "CH Transfer Manifest",
        {"tracking_number": tracking_number, "docstatus": 1},
        "name",
    )
    if not manifest_name:
        # Unknown tracking number — log and return 200 (don't let courier retry forever)
        frappe.log_error(
            f"Webhook from {courier_partner}: tracking {tracking_number!r} not found.",
            "Courier Webhook",
        )
        return {"received": True, "matched": False}

    if mapped_status:
        doc = frappe.get_doc("CH Transfer Manifest", manifest_name)
        if mapped_status != doc.status:
            doc.flags.ignore_validate_update_after_submit = True
            doc.db_set("status", mapped_status, update_modified=True)
            doc._sync_logistics_status_to_entries(mapped_status)
            try:
                doc.add_comment(
                    "Comment",
                    _("Courier webhook from {0}: {1}").format(
                        courier_partner, external_status or mapped_status
                    ),
                )
            except Exception:
                pass
            frappe.db.commit()

    return {"received": True, "matched": True, "manifest": manifest_name, "status": mapped_status}
