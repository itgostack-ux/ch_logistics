"""Provider-agnostic driver push notifications.

A single ``notify_driver`` entry point fans a message out across whatever
channels are available, the way a mature last-mile stack decouples the event
("trip assigned") from the transport (FCM / APNs / web push):

  * Frappe Notification Log + realtime  → lands on the desk delivery app today.
  * FCM HTTP v1 (legacy key)            → targets the native driver app once
                                          ``CH Logistics Settings`` is configured.

Every channel is best-effort and isolated — a failing transport never breaks
the operational flow that triggered the notification.
"""
import json

import frappe
from frappe.utils import now_datetime

from ch_logistics.logistics.doctype.ch_driver_device.ch_driver_device import active_tokens


def _provider() -> str:
    return frappe.db.get_single_value("CH Logistics Settings", "push_provider") or "Frappe Web Push"


def _driver_user(driver: str) -> str | None:
    return frappe.db.get_value("Driver", driver, "user")


def notify_driver(driver, title, body, data=None, reference=None):
    """Notify a driver. ``reference`` is an optional (doctype, name) tuple that
    deep-links the in-app notification. Returns a per-channel result dict."""
    if not driver:
        return {"ok": False, "reason": "no-driver"}

    result = {"driver": driver, "channels": []}
    provider = _provider()
    if provider == "Disabled":
        return {"ok": False, "reason": "disabled"}

    # 1) In-app / desk: Notification Log + realtime event ------------------
    user = _driver_user(driver)
    if user:
        try:
            _notify_inapp(user, title, body, reference)
            frappe.publish_realtime(
                event="driver_push",
                message={"title": title, "body": body, "data": data or {},
                         "ts": str(now_datetime())},
                user=user,
            )
            result["channels"].append("inapp")
        except Exception:
            frappe.log_error(title="notify_driver inapp failed",
                             message=frappe.get_traceback())

    # 2) FCM to the native app (only when explicitly configured) -----------
    if provider == "FCM":
        try:
            sent = _send_fcm(driver, title, body, data)
            if sent:
                result["channels"].append("fcm")
        except Exception:
            frappe.log_error(title="notify_driver fcm failed",
                             message=frappe.get_traceback())

    result["ok"] = bool(result["channels"])
    return result


def _notify_inapp(user, title, body, reference):
    notif = {
        "doctype": "Notification Log",
        "for_user": user,
        "type": "Alert",
        "subject": title,
        "email_content": body,
    }
    if reference and len(reference) == 2 and reference[0] and reference[1]:
        notif["document_type"] = reference[0]
        notif["document_name"] = reference[1]
    frappe.get_doc(notif).insert(ignore_permissions=True)


def _send_fcm(driver, title, body, data):
    """Best-effort FCM legacy-key send to all active device tokens. Returns the
    number of tokens targeted. No-op (returns 0) until a server key is set."""
    settings = frappe.get_cached_doc("CH Logistics Settings")
    server_key = settings.get_password("fcm_server_key", raise_exception=False) if settings else None
    if not server_key:
        return 0
    tokens = active_tokens(driver)
    if not tokens:
        return 0

    import requests  # lazy import — only needed on the FCM path

    payload = {
        "registration_ids": tokens,
        "notification": {"title": title, "body": body},
        "data": data or {},
        "priority": "high",
    }
    resp = requests.post(
        "https://fcm.googleapis.com/fcm/send",
        data=json.dumps(payload),
        headers={"Authorization": f"key={server_key}",
                 "Content-Type": "application/json"},
        timeout=10,
    )
    if resp.status_code >= 400:
        frappe.log_error(title="FCM send non-200",
                         message=f"{resp.status_code}: {resp.text[:500]}")
        return 0
    return len(tokens)
