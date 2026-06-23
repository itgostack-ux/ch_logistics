"""Rebind migrated logistics DocTypes, Pages, Custom Fields, and Singles
to the ``Logistics`` module (canonical home in app ``ch_logistics``).

Phase 3 of the logistics-app split:
  * 9 DocTypes (data preserved; only `tabDocType.module` flips)
  * 2 Desk Pages (delivery-app, logistics-control-tower)
  * Custom Fields previously tagged with "Ch Erp15" or "Ch Logistics"
  * Module Def — ensure "Logistics" exists in app `ch_logistics`
  * Pre-record previously-applied patches under their new dotted paths in
    ``tabPatch Log`` so ``bench migrate`` does not re-run them.

Idempotent: re-running is a no-op once everything is on "Logistics".
"""
from __future__ import annotations

import frappe


_DOCTYPES = (
	"CH Logistics Trip",
	"CH Logistics Trip Stop",
	"CH Logistics Exception",
	"CH Logistics Settings",
	"CH Transfer Manifest",
	"CH Transfer Manifest Item",
	"CH Transfer Package",
	"CH Driver Device",
	"Stock Entry Logistics History",
)

_PAGES = ("delivery-app", "logistics-control-tower")

_MODULE_OLD = ("Ch Erp15", "Ch Logistics")
_MODULE_NEW = "Logistics"

_PRE_APPLIED_PATCHES = (
	# Old dotted path in ch_erp15  →  New dotted path in ch_logistics
	("ch_erp15.patches.install_driver_app_fields",
	 "ch_logistics.patches.v0_0_3.install_driver_app_fields"),
	("ch_erp15.patches.install_logistics_phase2_fields",
	 "ch_logistics.patches.v0_0_3.install_logistics_phase2_fields"),
)


def execute():
	_ensure_module_def()
	_rebind_doctypes()
	_rebind_pages()
	_rebind_custom_fields()
	_rebind_phase2_module_def_owned_doctypes()
	_record_previously_applied_patches()
	frappe.db.commit()


def _ensure_module_def() -> None:
	if not frappe.db.exists("Module Def", _MODULE_NEW):
		frappe.get_doc({
			"doctype": "Module Def",
			"module_name": _MODULE_NEW,
			"app_name": "ch_logistics",
			"custom": 0,
		}).insert(ignore_permissions=True)
	else:
		# Ensure app_name is correct (in case it was created elsewhere)
		current_app = frappe.db.get_value("Module Def", _MODULE_NEW, "app_name")
		if current_app != "ch_logistics":
			frappe.db.set_value("Module Def", _MODULE_NEW, "app_name", "ch_logistics")


def _rebind_doctypes() -> None:
	for dt in _DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue
		current = frappe.db.get_value("DocType", dt, "module")
		if current in _MODULE_OLD:
			frappe.db.set_value("DocType", dt, "module", _MODULE_NEW, update_modified=False)


def _rebind_pages() -> None:
	for page in _PAGES:
		if not frappe.db.exists("Page", page):
			continue
		current = frappe.db.get_value("Page", page, "module")
		if current in _MODULE_OLD:
			frappe.db.set_value("Page", page, "module", _MODULE_NEW, update_modified=False)


def _rebind_custom_fields() -> None:
	"""Re-tag Custom Fields installed by the moved patches so fixture export
	from ``ch_logistics`` picks them up (filtered by module='Logistics')."""
	frappe.db.sql(
		"""
		UPDATE `tabCustom Field`
		SET module = %s
		WHERE module IN %s
		  AND dt IN %s
		""",
		(_MODULE_NEW, _MODULE_OLD, _DOCTYPES),
	)


def _rebind_phase2_module_def_owned_doctypes() -> None:
	"""Phase 2 created CH Driver Location, CH Manifest Rejection,
	CH Tracking Settings under module 'Ch Logistics' (snake folder
	`ch_logistics`). After the rename to 'Logistics' (folder `logistics/`),
	rebind those rows too."""
	phase2 = ("CH Driver Location", "CH Manifest Rejection", "CH Tracking Settings")
	for dt in phase2:
		if not frappe.db.exists("DocType", dt):
			continue
		if frappe.db.get_value("DocType", dt, "module") == "Ch Logistics":
			frappe.db.set_value("DocType", dt, "module", _MODULE_NEW, update_modified=False)
	for page in ("driver-map", "live-fleet-map"):
		if frappe.db.exists("Page", page) and frappe.db.get_value("Page", page, "module") == "Ch Logistics":
			frappe.db.set_value("Page", page, "module", _MODULE_NEW, update_modified=False)
	frappe.db.sql(
		"""UPDATE `tabCustom Field` SET module=%s WHERE module='Ch Logistics'""",
		(_MODULE_NEW,),
	)
	# Drop the now-empty 'Ch Logistics' Module Def if nothing still claims it
	if frappe.db.exists("Module Def", "Ch Logistics"):
		still_referenced = (
			frappe.db.count("DocType", {"module": "Ch Logistics"})
			+ frappe.db.count("Page", {"module": "Ch Logistics"})
			+ frappe.db.count("Custom Field", {"module": "Ch Logistics"})
		)
		if not still_referenced:
			frappe.delete_doc("Module Def", "Ch Logistics", force=True, ignore_permissions=True)


def _record_previously_applied_patches() -> None:
	"""For each patch already recorded under its old ch_erp15 dotted path,
	insert a row with the new ch_logistics path so ``frappe.modules.patch_handler``
	skips it on this migrate."""
	for old, new in _PRE_APPLIED_PATCHES:
		old_applied = frappe.db.exists("Patch Log", {"patch": old})
		new_applied = frappe.db.exists("Patch Log", {"patch": new})
		if old_applied and not new_applied:
			frappe.get_doc({"doctype": "Patch Log", "patch": new}).insert(
				ignore_permissions=True
			)
