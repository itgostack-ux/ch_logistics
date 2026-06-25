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
    d = frappe.db.get_value(
        "Driver", driver,
        ["name", "full_name", "cell_number", "availability_status",
         "current_trip", "last_active"],
        as_dict=True,
    ) or {}
    return d


# --------------------------------------------------------------------------
# Session: login / logout / token (FR-001 → FR-008)
# --------------------------------------------------------------------------
@frappe.whitelist()
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
    frappe.db.commit()
    return {
        "driver": driver,
        "device": device,
        "status": ds.AVAILABLE,
        "profile": _profile(driver),
    }


@frappe.whitelist()
def driver_logout(device_id=None):
    """Clear FCM token(s), deactivate the device(s) and go Offline (FR-008)."""
    driver = _current_driver()
    deactivate_devices(driver, device_id=device_id)
    ds.set_status(driver, ds.OFFLINE, force=True, touch_activity=False)
    frappe.db.commit()
    return {"driver": driver, "status": ds.OFFLINE}


@frappe.whitelist()
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
@frappe.whitelist()
def heartbeat(lat=None, lng=None):
    """Lightweight keep-alive. Refreshes activity and pulls an Idle driver back
    to Available (FR-047). The mobile app calls this periodically and on any
    foreground action."""
    driver = _current_driver()
    status = ds.touch_activity(driver)
    return {"driver": driver, "status": status}


@frappe.whitelist()
def set_break():
    """Driver starts a break — Available → Break (FR-042)."""
    driver = _current_driver()
    ds.set_status(driver, ds.BREAK)
    frappe.db.commit()
    return {"driver": driver, "status": ds.BREAK}


@frappe.whitelist()
def end_break():
    """Driver resumes from break — Break → Available (FR-043, FR-044)."""
    driver = _current_driver()
    ds.set_status(driver, ds.AVAILABLE)
    frappe.db.commit()
    return {"driver": driver, "status": ds.AVAILABLE}


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
    )
