"""Customer / destination track-and-trace + proactive delivery notifications.

Closes the "no customer visibility" gap: enterprise TMS push a branded tracking
link and proactive "out for delivery / arriving / delivered" messages. Here the
recipient of an inter-store transfer is the destination store, so notifications
go to the destination store's contacts, and anyone with the tokenized link sees
a live status timeline + ETA + driver position.
"""
import frappe
from frappe import _
from frappe.utils import get_url, now_datetime

# Public, recipient-safe status labels (no internal vocabulary leaked).
_PUBLIC_STATUS = {
    "Packed": ("Preparing", False),
    "Assigned": ("Driver assigned", False),
    "Pickup Started": ("Picked up", False),
    "In Transit": ("Out for delivery", False),
    "Delivered": ("Delivered", True),
    "Partially Received": ("Delivered", True),
    "Received": ("Received", True),
    "Closed": ("Completed", True),
    "Rejected": ("Could not be picked up", False),
    "Cancelled": ("Cancelled", False),
    "Returned": ("Returned", False),
}


def ensure_token(doc) -> str:
    if not doc.get("tracking_token"):
        doc.tracking_token = frappe.generate_hash(length=22)
    return doc.tracking_token


def tracking_url(doc) -> str:
    token = doc.get("tracking_token") or ""
    return get_url(f"/track?id={token}")


# ── public page payload ────────────────────────────────────────────────────
@frappe.whitelist(allow_guest=True)
def get_public_tracking(token: str) -> dict:
    """Recipient-safe tracking payload for the public /track page."""
    if not token:
        frappe.throw(_("Invalid tracking link."), frappe.PermissionError)
    name = frappe.db.get_value("CH Transfer Manifest", {"tracking_token": token}, "name")
    if not name:
        frappe.throw(_("Tracking link not found."), frappe.DoesNotExistError)

    m = frappe.db.get_value(
        "CH Transfer Manifest", name,
        ["name", "status", "source_warehouse", "destination_warehouse",
         "source_store", "destination_store", "driver_name", "trip",
         "pickup_datetime", "delivery_datetime", "received_datetime",
         "estimated_delivery_date", "creation"],
        as_dict=True,
    )
    label, done = _PUBLIC_STATUS.get(m.status, (m.status, False))

    # ETA: trip planned_end (driven by predictive ETA) else manifest estimate.
    eta = None
    driver_pos = None
    if m.trip:
        eta = frappe.db.get_value("CH Logistics Trip", m.trip, "planned_end")
        driver = frappe.db.get_value("CH Logistics Trip", m.trip, "driver")
        if driver and m.status == "In Transit":
            pos = frappe.db.get_value(
                "CH Driver Location", {"driver": driver},
                ["latitude", "longitude", "captured_at"],
                order_by="captured_at desc", as_dict=True)
            if pos and pos.latitude and pos.longitude:
                driver_pos = {"lat": pos.latitude, "lng": pos.longitude,
                              "at": str(pos.captured_at)}
    eta = eta or m.estimated_delivery_date

    timeline = [
        {"label": _("Order prepared"), "time": m.creation, "done": True},
        {"label": _("Picked up"), "time": m.pickup_datetime, "done": bool(m.pickup_datetime)},
        {"label": _("Out for delivery"), "time": m.pickup_datetime,
         "done": m.status in ("In Transit", "Delivered", "Partially Received", "Received", "Closed")},
        {"label": _("Delivered"), "time": m.delivery_datetime, "done": bool(m.delivery_datetime)},
    ]

    return {
        "order_id": m.name,
        "status": label,
        "is_delivered": done,
        "from": m.source_store or m.source_warehouse,
        "to": m.destination_store or m.destination_warehouse,
        "driver_name": (m.driver_name or "").split(" ")[0] if m.driver_name else None,
        "eta": str(eta) if eta else None,
        "driver_position": driver_pos,
        "timeline": [{"label": t["label"], "time": str(t["time"]) if t["time"] else None,
                      "done": t["done"]} for t in timeline],
    }


# ── proactive notifications ────────────────────────────────────────────────
def notify_destination(manifest_name: str, event: str) -> dict:
    """Notify the destination store's contacts on a key delivery milestone.

    event ∈ {"out_for_delivery", "delivered"}. Best-effort across in-app +
    email (+ WhatsApp if configured); never raises into the delivery flow.
    """
    try:
        m = frappe.get_doc("CH Transfer Manifest", manifest_name)
        url = tracking_url(m)
        dest = m.destination_store or m.destination_warehouse or _("destination")

        if event == "out_for_delivery":
            subject = _("Transfer {0} is out for delivery").format(m.name)
            body = _("Your transfer {0} to {1} is out for delivery. Track it live: {2}").format(
                m.name, dest, url)
        elif event == "delivered":
            subject = _("Transfer {0} delivered").format(m.name)
            body = _("Your transfer {0} to {1} has been delivered.").format(m.name, dest)
        else:
            return {"ok": False, "reason": "unknown-event"}

        users, emails, phones = _destination_contacts(m.destination_store)

        # 1. In-app notifications to destination store users.
        for user in users:
            if user and user not in ("Administrator", "Guest"):
                frappe.get_doc({
                    "doctype": "Notification Log", "for_user": user, "type": "Alert",
                    "subject": subject, "email_content": body,
                    "document_type": "CH Transfer Manifest", "document_name": m.name,
                }).insert(ignore_permissions=True)

        # 2. Email (only sends if a working outgoing account is configured).
        if emails:
            frappe.sendmail(recipients=emails, subject=subject,
                            message=body.replace(url, f'<a href="{url}">{url}</a>'),
                            reference_doctype="CH Transfer Manifest", reference_name=m.name,
                            now=False)

        # 3. WhatsApp (best-effort, optional helper).
        _maybe_whatsapp(phones, m, event, url)

        return {"ok": True, "users": len(users), "emails": len(emails), "phones": len(phones)}
    except Exception:
        frappe.log_error(title=f"notify_destination[{event}] failed for {manifest_name}",
                         message=frappe.get_traceback())
        return {"ok": False}


def _destination_contacts(destination_store):
    """Resolve (users, emails, phones) for the destination store, reusing the
    transfer-manifest contact resolver."""
    try:
        from ch_logistics.api.transfer_manifest_api import _collect_store_manager_contacts
        return _collect_store_manager_contacts(destination_store)
    except Exception:
        return ([], [], [])


def _maybe_whatsapp(phones, m, event, url):
    if not phones:
        return
    try:
        from ch_item_master.ch_core.whatsapp import send_template_message
    except Exception:
        return
    for phone in phones:
        try:
            send_template_message(
                phone=phone,
                template_name="transfer_status",
                body_values={"1": m.name, "2": m.destination_store or "", "3": url},
                ref_doctype="CH Transfer Manifest", ref_name=m.name, enqueue=True,
            )
        except Exception:
            # Template may not exist on every site — stay silent.
            return
