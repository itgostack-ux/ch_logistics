"""CH Manifest Rejection — driver rejects a pickup with two proof photos."""
from __future__ import annotations

import math

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class CHManifestRejection(Document):
	def _authorized_manifest(self):
		from ch_logistics.api.driver_resolver import assert_manifest_driver_access

		locked = frappe.db.sql(
			"SELECT name FROM `tabCH Transfer Manifest` WHERE name = %s FOR UPDATE",
			(self.manifest,),
		)
		if not locked:
			frappe.throw(_("Transfer Manifest {0} does not exist.").format(self.manifest))
		manifest = frappe.get_doc("CH Transfer Manifest", self.manifest)
		assert_manifest_driver_access(manifest, scope_side="source")
		if not manifest.driver:
			frappe.throw(_("The manifest has no assigned driver."), frappe.PermissionError)
		if manifest.status not in ("Assigned", "Pickup Started", "In Transit"):
			frappe.throw(_("Only an active pickup or in-transit delivery can be rejected."))
		return manifest

	def before_insert(self) -> None:
		manifest = self._authorized_manifest()
		if frappe.db.exists(
			"CH Manifest Rejection",
			{
				"manifest": self.manifest,
				"docstatus": ("<", 2),
				"status": ("not in", ("Closed", "Reassigned")),
			},
		):
			frappe.throw(_("An active rejection already exists for this manifest."))
		self.driver = manifest.driver
		self.trip = manifest.trip
		self.rejected_by = frappe.session.user
		self.rejected_on = now_datetime()
		self.status = "Pending Review"
		self.dispatcher_notified = 0

	def validate(self) -> None:
		if not self.proof_image_1 or not self.proof_image_2:
			frappe.throw(_("Both proof photos are required (FR-024, FR-025)."))
		if self.proof_image_1 == self.proof_image_2:
			frappe.throw(_("The two proof photos must be different."))
		if len(self.remarks or "") > 1000:
			frappe.throw(_("Remarks cannot exceed 1,000 characters."))
		for value, minimum, maximum, label in (
			(self.latitude, -90, 90, _("Latitude")),
			(self.longitude, -180, 180, _("Longitude")),
		):
			if value is not None and (not math.isfinite(float(value)) or not minimum <= float(value) <= maximum):
				frappe.throw(_("{0} is outside the valid range.").format(label))
		if not self.is_new():
			original = frappe.db.get_value(
				self.doctype,
				self.name,
				["manifest", "trip", "driver", "rejected_by", "rejected_on",
				 "status", "dispatcher_notified", "resolved_on"],
				as_dict=True,
			)
			if original and any(
				original.get(fieldname) != self.get(fieldname)
				for fieldname in (
					"manifest", "trip", "driver", "rejected_by", "rejected_on",
					"status", "dispatcher_notified", "resolved_on",
				)
			):
				frappe.throw(_("Manifest rejection ownership fields cannot be changed."))

	def before_submit(self) -> None:
		manifest = self._authorized_manifest()
		if self.driver != manifest.driver or self.trip != manifest.trip:
			frappe.throw(_("Manifest assignment changed; create a new rejection."))

	def before_save(self) -> None:
		if self.is_new():
			self.rejected_by = frappe.session.user
		if not self.rejected_on:
			self.rejected_on = now_datetime()
		# Auto-populate trip from manifest if blank
		if self.manifest and not self.trip:
			self.trip = frappe.db.get_value("CH Transfer Manifest",
											self.manifest, "trip")

	def on_submit(self) -> None:
		"""Run the one authoritative manifest rejection lifecycle atomically."""
		manifest = self._authorized_manifest()
		manifest.reject_manifest(
			rejection_reason=self.rejection_reason,
			rejection_photo=self.proof_image_1,
			rejection_notes=self.remarks,
		)
		# The manifest controller performs the scoped dispatcher notification.
		# This flag means that path was invoked, not that a swallowed duplicate
		# notification happened here.
		self.db_set("dispatcher_notified", 1, update_modified=False)

	def _notify_dispatcher(self) -> None:
		"""Best-effort FCM + system notification to the dispatcher."""
		from ch_logistics.roles import get_notification_role_users

		dispatchers = get_notification_role_users("rejection_dispatcher_notify")
		company = frappe.db.get_value("CH Transfer Manifest", self.manifest, "company")
		if company:
			try:
				from ch_erp15.ch_erp15.notification_router import filter_users_by_company

				dispatchers = filter_users_by_company(dispatchers, company)
			except Exception:
				dispatchers = []
		for user in dispatchers:
			try:
				frappe.publish_realtime(
					event="ch_logistics:manifest_rejected",
					message={
						"rejection": self.name,
						"manifest": self.manifest,
						"driver": self.driver,
						"reason": self.rejection_reason,
					},
					user=user,
				)
			except Exception:
				continue
