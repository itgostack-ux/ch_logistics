"""Driver mobile/app API — the device-facing surface of the logistics module.

Implements the BRD's driver-app contract (FR-001 → FR-009, FR-042 → FR-047):
login + device registration + FCM token capture, the operational status
machine, break management and the activity heartbeat that drives idle
detection. Trip- and manifest-level actions live in ``logistics_api`` and
``transfer_manifest_api``; this module owns the *driver session* itself.

Every endpoint resolves the Driver from the authenticated Frappe user, so the
caller authenticates with normal Frappe credentials/token (FR-001) and never
passes a driver id from the client.
"""
import frappe
from frappe import _

from ch_logistics.api import driver_status as ds
from ch_logistics.api.driver_resolver import resolve_current_driver
from ch_logistics import roles as role_registry
from ch_logistics.logistics.doctype.ch_driver_device.ch_driver_device import (
    deactivate_devices,
    register_device,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _current_driver(throw=True) -> str | None:
    """Thin shim over the shared resolver (kept for backward compatibility
    with existing call sites in this module). See
    :func:`ch_logistics.api.driver_resolver.resolve_current_driver`."""
    return resolve_current_driver(throw=throw)


def _single_device_enforced() -> bool:
    val = frappe.db.get_single_value("CH Logistics Settings", "enforce_single_device")
    return val is None or int(val) == 1


def _profile(driver: str) -> dict:
    # Some environments may not have logistics custom fields yet; keep the
    # profile API resilient and return sane defaults instead of SQL errors.
    meta = frappe.get_meta("Driver")
    fields = ["name", "full_name", "cell_number"]
    optional = ["availability_status", "current_trip", "last_active"]
    fields.extend([f for f in optional if meta.has_field(f)])

    d = frappe.db.get_value("Driver", driver, fields, as_dict=True) or {}
    if "availability_status" not in d:
        d["availability_status"] = ds.get_status(driver) or ds.OFFLINE
    if "current_trip" not in d:
        d["current_trip"] = None
    if "last_active" not in d:
        d["last_active"] = None
    return d


# --------------------------------------------------------------------------
# Session: login / logout / token (FR-001 → FR-008)
# --------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def driver_login(device_id, platform="Android", fcm_token=None, app_version=None):
    """Register the handset against the logged-in driver and bring them online.

    Captures Device ID (FR-002) and FCM token (FR-003), stores both against the
    driver (FR-006), and sets status → Available (FR-004)."""
    driver = _current_driver()
    if not device_id:
        frappe.throw(_("Device ID is required."))

    device = register_device(
        driver=driver,
        device_id=device_id,
        platform=platform,
        fcm_token=fcm_token,
        app_version=app_version,
        enforce_single_device=_single_device_enforced(),
    )
    # Offline → Available. force=True because a re-login from any online state
    # (e.g. app reinstall mid-shift) should still settle to Available.
    ds.set_status(driver, ds.AVAILABLE, force=True)
    return {
        "driver": driver,
        "device": device,
        "status": ds.AVAILABLE,
        "profile": _profile(driver),
    }


@frappe.whitelist(methods=["POST"])
def driver_logout(device_id=None):
    """Clear FCM token(s), deactivate the device(s) and go Offline (FR-008)."""
    driver = _current_driver()
    deactivate_devices(driver, device_id=device_id)
    ds.set_status(driver, ds.OFFLINE, force=True, touch_activity=False)
    return {"driver": driver, "status": ds.OFFLINE}


@frappe.whitelist(methods=["POST"])
def update_fcm_token(device_id, fcm_token):
    """Refresh the FCM token for an existing device (FR-007)."""
    driver = _current_driver()
    register_device(
        driver=driver,
        device_id=device_id,
        fcm_token=fcm_token,
        enforce_single_device=_single_device_enforced(),
    )
    ds.touch_activity(driver)
    return {"ok": True}


# --------------------------------------------------------------------------
# Status / activity (FR-004, FR-042 → FR-047)
# --------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def heartbeat(lat=None, lng=None):
    """Lightweight keep-alive. Refreshes activity and pulls an Idle driver back
    to Available (FR-047). The mobile app calls this periodically and on any
    foreground action."""
    driver = _current_driver()
    status = ds.touch_activity(driver)
    return {"driver": driver, "status": status}


@frappe.whitelist(methods=["POST"])
def set_break(break_type: str = "Rest", reason: str = None, lat=None, lng=None, trip: str = None):
    """Driver starts a break — Available → Break (FR-042).

    Also writes a CH Driver Break Log row so the Monthly Driver KPI
    report has an audit trail of break-time.  The doctype's
    ``validate`` prevents duplicate open rows for the same driver, and
    ``start_break_for_driver`` is idempotent for concurrent taps.
    """
    from ch_logistics.logistics.doctype.ch_driver_break_log.ch_driver_break_log import (
        start_break_for_driver,
    )

    driver = _current_driver()
    if trip and frappe.db.get_value("CH Logistics Trip", trip, "driver") != driver:
        frappe.throw(
            _("You can only start a break against your assigned trip."),
            frappe.PermissionError,
        )
    ds.set_status(driver, ds.BREAK)
    log_name = None
    try:
        log_name = start_break_for_driver(
            driver,
            break_type=(break_type or "Rest"),
            reason=reason,
            trip=trip,
            latitude=float(lat) if lat not in (None, "") else None,
            longitude=float(lng) if lng not in (None, "") else None,
        )
    except Exception:
        # Break-log persistence is best-effort — never let it block the
        # underlying status change (mirrors the same defensive pattern
        # used in ``_cascade_stop_status_to_trip``).
        frappe.log_error(
            frappe.get_traceback(),
            f"CH Driver Break Log start failed for {driver}",
        )
    return {"driver": driver, "status": ds.BREAK, "break_log": log_name}


@frappe.whitelist(methods=["POST"])
def end_break(lat=None, lng=None):
    """Driver resumes from break — Break → Available (FR-043, FR-044).

    Closes the newest open CH Driver Break Log row for the driver
    and stamps end_ts + duration_min via the doctype's ``before_save``.
    Returns the closed log name for the mobile app to display.
    """
    from ch_logistics.logistics.doctype.ch_driver_break_log.ch_driver_break_log import (
        end_break_for_driver,
    )

    driver = _current_driver()
    ds.set_status(driver, ds.AVAILABLE)
    log_name = None
    try:
        log_name = end_break_for_driver(
            driver,
            latitude=float(lat) if lat not in (None, "") else None,
            longitude=float(lng) if lng not in (None, "") else None,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"CH Driver Break Log end failed for {driver}",
        )
    return {"driver": driver, "status": ds.AVAILABLE, "break_log": log_name}


@frappe.whitelist()
def get_status():
    """Current driver profile + operational status (app home screen)."""
    driver = _current_driver()
    return _profile(driver)


@frappe.whitelist()
def get_active_devices():
    """Devices currently bound to the driver (settings / security screen)."""
    driver = _current_driver()
    return frappe.get_all(
        "CH Driver Device",
        filters={"driver": driver},
        fields=["name", "device_id", "platform", "app_version", "is_active",
                "last_seen", "registered_on"],
        order_by="is_active desc, last_seen desc",
        limit_page_length=role_registry.get_int_setting("driver_device_row_limit", 20),
    )
