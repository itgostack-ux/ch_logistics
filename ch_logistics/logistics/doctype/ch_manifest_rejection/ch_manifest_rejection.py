"""CH Manifest Rejection — driver rejects a pickup with two proof photos."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class CHManifestRejection(Document):
	def validate(self) -> None:
		if not self.proof_image_1 or not self.proof_image_2:
			frappe.throw(_("Both proof photos are required (FR-024, FR-025)."))
		if self.proof_image_1 == self.proof_image_2:
			frappe.throw(_("The two proof photos must be different."))

	def before_save(self) -> None:
		if not self.rejected_by:
			self.rejected_by = frappe.session.user
		if not self.rejected_on:
			self.rejected_on = now_datetime()
		# Auto-populate trip from manifest if blank
		if self.manifest and not self.trip:
			self.trip = frappe.db.get_value("CH Transfer Manifest",
											self.manifest, "trip")

	def on_submit(self) -> None:
		"""Mark the manifest as Rejected and notify dispatcher."""
		try:
			frappe.db.set_value("CH Transfer Manifest", self.manifest,
								{"status": "Rejected"})
		except Exception:
			frappe.log_error(frappe.get_traceback(),
							 "CHManifestRejection.on_submit set status")

		try:
			self._notify_dispatcher()
			self.db_set("dispatcher_notified", 1, update_modified=False)
		except Exception:
			frappe.log_error(frappe.get_traceback(),
							 "CHManifestRejection._notify_dispatcher")

	def _notify_dispatcher(self) -> None:
		"""Best-effort FCM + system notification to the dispatcher."""
		# Reuse existing push helper while it still lives in ch_erp15
		try:
			from ch_logistics.api.driver_push import notify_driver  # noqa
		except Exception:
			notify_driver = None

		# Build dispatcher list — central registry (rejection_dispatcher_notify)
		from ch_logistics.roles import get_roles_for

		dispatchers = frappe.get_all(
			"Has Role",
			filters={"role": ["in", sorted(get_roles_for("rejection_dispatcher_notify"))], "parenttype": "User"},
			pluck="parent",
		)
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
