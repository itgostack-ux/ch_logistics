import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class CHDriverDevice(Document):
    def before_insert(self):
        if not self.registered_on:
            self.registered_on = now_datetime()
        if not self.last_seen:
            self.last_seen = self.registered_on

    def validate(self):
        if self.driver and not self.user:
            self.user = frappe.db.get_value("Driver", self.driver, "user")


def register_device(driver, device_id, platform="Android", fcm_token=None,
                    app_version=None, enforce_single_device=True):
    """Upsert the device row for ``driver`` + ``device_id`` and return its name.

    Single-device enforcement (Ekart/Delhivery security default): registering a
    handset deactivates any other active device for the same driver so push and
    sessions only ever target the current phone.
    """
    existing = frappe.db.get_value(
        "CH Driver Device", {"driver": driver, "device_id": device_id}, "name"
    )
    now = now_datetime()
    if existing:
        doc = frappe.get_doc("CH Driver Device", existing)
        doc.platform = platform or doc.platform
        if fcm_token:
            doc.fcm_token = fcm_token
        if app_version:
            doc.app_version = app_version
        doc.is_active = 1
        doc.last_seen = now
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({
            "doctype": "CH Driver Device",
            "driver": driver,
            "device_id": device_id,
            "platform": platform or "Android",
            "fcm_token": fcm_token,
            "app_version": app_version,
            "is_active": 1,
            "registered_on": now,
            "last_seen": now,
        }).insert(ignore_permissions=True)

    if enforce_single_device:
        others = frappe.get_all(
            "CH Driver Device",
            filters={"driver": driver, "is_active": 1, "name": ["!=", doc.name]},
            pluck="name",
        )
        for other in others:
            frappe.db.set_value("CH Driver Device", other, "is_active", 0)
    return doc.name


def deactivate_devices(driver, device_id=None):
    """Deactivate and clear push tokens on logout. Scoped to one device when
    ``device_id`` is given, otherwise all devices for the driver (FR-008)."""
    filters = {"driver": driver, "is_active": 1}
    if device_id:
        filters["device_id"] = device_id
    for name in frappe.get_all("CH Driver Device", filters=filters, pluck="name"):
        frappe.db.set_value("CH Driver Device", name, {"is_active": 0, "fcm_token": None})


def active_tokens(driver) -> list[str]:
    """Return live FCM tokens for a driver — only active devices (FR-009)."""
    rows = frappe.get_all(
        "CH Driver Device",
        filters={"driver": driver, "is_active": 1, "fcm_token": ["is", "set"]},
        pluck="fcm_token",
    )
    return [t for t in rows if t]
