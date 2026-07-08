"""Firebase Cloud Messaging transport for the driver / logistics app.

The single place Firebase credentials and the Admin SDK live. ``driver_push``
resolves a driver's active device tokens and hands them here to be delivered.

The transport uses the official Firebase Admin SDK (HTTP v1). It degrades
gracefully: a missing library, missing credentials, or dry-run mode all return
a structured no-op result rather than raising, so a mis-configured or offline
Firebase never breaks the operational flow that triggered the notification.
"""
import json
import os

import frappe

_FIREBASE_APP_NAME = "ch_bench_fcm"


# ---------------------------------------------------------------------------
# Configuration / credentials
# ---------------------------------------------------------------------------
def _settings():
    try:
        return frappe.get_cached_doc("CH Logistics Settings")
    except Exception:
        return None


def _dry_run() -> bool:
    """Simulate sends without contacting Firebase (used by tests/staging)."""
    return bool(frappe.flags.get("fcm_dry_run"))


def _load_service_account() -> dict | None:
    """Resolve the Firebase service-account JSON.

    Order: the ``fcm_service_account_file`` attached on CH Logistics Settings,
    then a ``fcm_service_account_path`` in site config (the guide's
    ``sites/<site>/private/firebase-adminsdk.json`` approach). Returns the parsed
    dict, or ``None`` when nothing is configured.
    """
    raw = None
    settings = _settings()
    file_url = settings.get("fcm_service_account_file") if settings else None
    if file_url:
        try:
            files = frappe.get_all("File", filters={"file_url": file_url}, limit=1, pluck="name")
            if files:
                raw = frappe.get_doc("File", files[0]).get_content()
        except Exception:
            frappe.log_error(title="FCM: cannot read service-account file",
                             message=frappe.get_traceback())

    if not raw:
        path = frappe.conf.get("fcm_service_account_path")
        if path:
            abspath = path if os.path.isabs(path) else frappe.get_site_path(path)
            if os.path.exists(abspath):
                with open(abspath) as fh:
                    raw = fh.read()

    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except Exception:
        frappe.log_error(title="FCM: bad service-account JSON",
                         message=frappe.get_traceback())
        return None


def _get_app():
    """Return the initialised (and cached) Firebase Admin app, or ``None``."""
    try:
        import firebase_admin
        from firebase_admin import credentials
    except Exception:
        return None
    try:
        return firebase_admin.get_app(_FIREBASE_APP_NAME)
    except ValueError:
        pass  # not initialised yet
    service_account = _load_service_account()
    if not service_account:
        return None
    try:
        cert = credentials.Certificate(service_account)
        return firebase_admin.initialize_app(cert, name=_FIREBASE_APP_NAME)
    except Exception:
        frappe.log_error(title="FCM: Admin SDK init failed",
                         message=frappe.get_traceback())
        return None


def is_configured() -> bool:
    """True when a real send could go out (creds present + library installed)."""
    return _get_app() is not None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _clean(tokens) -> list[str]:
    seen, out = set(), []
    for t in tokens or []:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def send_to_tokens(tokens, title, body, data=None) -> dict:
    """Best-effort multicast to raw FCM tokens via the Admin SDK (HTTP v1).

    Returns ``{ok, sent, failed, ...}`` where ``failed`` lists tokens Firebase
    reported as permanently invalid (unregistered / sender-id mismatch) so the
    caller can deactivate them. Never raises.
    """
    tokens = _clean(tokens)
    if not tokens:
        return {"ok": False, "sent": 0, "failed": [], "reason": "no-tokens"}
    if _dry_run():
        return {"ok": True, "sent": len(tokens), "failed": [], "dry_run": True}

    app = _get_app()
    if app is None:
        return {"ok": False, "sent": 0, "failed": [], "reason": "not-configured"}

    from firebase_admin import messaging

    message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={str(k): str(v) for k, v in (data or {}).items()},
        android=messaging.AndroidConfig(priority="high"),
    )
    try:
        resp = messaging.send_each_for_multicast(message, app=app)
    except Exception:
        frappe.log_error(title="FCM: send failed", message=frappe.get_traceback())
        return {"ok": False, "sent": 0, "failed": [], "reason": "send-error"}

    permanent = tuple(
        exc for exc in (
            getattr(messaging, "UnregisteredError", None),
            getattr(messaging, "SenderIdMismatchError", None),
        ) if exc is not None
    )
    failed = []
    for token, r in zip(tokens, resp.responses):
        if not r.success and permanent and isinstance(r.exception, permanent):
            failed.append(token)
    if resp.failure_count:
        frappe.log_error(
            title="FCM: partial delivery failure",
            message=f"success={resp.success_count} failure={resp.failure_count} "
                    f"invalid_tokens={len(failed)}",
        )
    return {"ok": resp.success_count > 0, "sent": resp.success_count, "failed": failed}


def _deactivate_invalid(doctype, tokens) -> None:
    """Deactivate + clear device rows whose token FCM reported as invalid."""
    for t in tokens or []:
        for name in frappe.get_all(doctype, filters={"fcm_token": t}, pluck="name"):
            frappe.db.set_value(doctype, name, {"is_active": 0, "fcm_token": None})
