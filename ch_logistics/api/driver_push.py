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
    """Push to all of the driver's active device tokens via the shared FCM
    transport (Firebase Admin SDK, HTTP v1). Returns the number of tokens
    successfully delivered to; 0 (no-op) until Firebase credentials are set.
    Tokens FCM reports as permanently invalid are deactivated."""
    tokens = active_tokens(driver)
    if not tokens:
        return 0

    from ch_logistics.api.fcm import send_to_tokens, _deactivate_invalid

    res = send_to_tokens(tokens, title, body, data)
    if res.get("failed"):
        _deactivate_invalid("CH Driver Device", res["failed"])
    return res.get("sent", 0)
