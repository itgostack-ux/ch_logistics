import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class CHDriverDevice(Document):
    def before_insert(self):
        self._validate_owner()
        if not self.registered_on:
            self.registered_on = now_datetime()
        if not self.last_seen:
            self.last_seen = self.registered_on

    def validate(self):
        self._validate_owner()
        if self.driver:
            self.user = frappe.db.get_value("Driver", self.driver, "user")

    def _validate_owner(self):
        from ch_logistics import roles as role_registry
        from ch_logistics.api.driver_resolver import resolve_current_driver

        if role_registry.is_privileged():
            return
        driver = resolve_current_driver(throw=True)
        if self.driver != driver:
            frappe.throw("Devices can only be registered for your Driver profile.", frappe.PermissionError)
        if not self.is_new():
            stored_driver = frappe.db.get_value("CH Driver Device", self.name, "driver")
            if stored_driver and stored_driver != self.driver:
                frappe.throw("A registered device cannot be moved to another Driver.", frappe.PermissionError)


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
    if not existing:
        from ch_logistics import roles as role_registry

        device_limit = role_registry.get_int_setting("driver_device_row_limit", 20)
        if frappe.db.count("CH Driver Device", {"driver": driver}) >= device_limit:
            frappe.throw(
                "Registered device limit reached. Remove an old device before adding another.",
                frappe.ValidationError,
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
        doc.save()
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
        }).insert()

    if enforce_single_device:
        frappe.db.sql(
            """UPDATE `tabCH Driver Device`
                  SET is_active = 0, modified = %s, modified_by = %s
                WHERE driver = %s AND is_active = 1 AND name != %s""",
            (now, frappe.session.user, driver, doc.name),
        )
    return doc.name


def deactivate_devices(driver, device_id=None):
    """Deactivate and clear push tokens on logout. Scoped to one device when
    ``device_id`` is given, otherwise all devices for the driver (FR-008)."""
    conditions = ["driver = %(driver)s", "is_active = 1"]
    params = {"driver": driver, "user": frappe.session.user, "now": now_datetime()}
    if device_id:
        conditions.append("device_id = %(device_id)s")
        params["device_id"] = device_id
    frappe.db.sql(
        f"""UPDATE `tabCH Driver Device`
               SET is_active = 0, fcm_token = NULL,
                   modified = %(now)s, modified_by = %(user)s
             WHERE {' AND '.join(conditions)}""",
        params,
    )


def active_tokens(driver) -> list[str]:
    """Return live FCM tokens for a driver — only active devices (FR-009)."""
    from ch_logistics import roles as role_registry

    rows = frappe.get_all(
        "CH Driver Device",
        filters={"driver": driver, "is_active": 1, "fcm_token": ["is", "set"]},
        pluck="fcm_token",
        limit_page_length=role_registry.get_int_setting("driver_device_row_limit", 20),
    )
    return [t for t in rows if t]
