"""Setup hooks — after_install / after_migrate."""

import frappe
from frappe.utils import add_to_date, cint, now_datetime


def after_install():
	"""Run patches that install custom fields on upstream Driver."""
	from ch_logistics.patches.v0_0_1 import install_driver_geo_fields
	from ch_logistics.patches.v0_0_6 import add_arrival_location_fields
	install_driver_geo_fields.execute()
	add_arrival_location_fields.execute()


def after_migrate():
	"""Idempotent — re-run field installer to recover from manual drift."""
	# Re-run every custom-field installer (all idempotent) — recovers from
	# manual drift AND from prod-dump restores that lost Custom Fields while
	# the patch log still says "executed".
	from ch_logistics.patches.v0_0_1 import install_driver_geo_fields
	from ch_logistics.patches.v0_0_3 import (
		install_driver_app_fields,
		install_logistics_phase2_fields,
	)
	from ch_logistics.patches.v0_0_4 import install_geo_optimization_fields
	from ch_logistics.patches.v0_0_5 import (
		extend_rejection_reasons,
		install_tracking_token,
	)
	from ch_logistics.patches.v0_0_6 import add_arrival_location_fields
	from ch_logistics.patches.v0_0_7 import install_store_geo_fields
	from ch_logistics.patches.v0_0_9 import install_driver_status_fields
	for installer in (
		install_driver_geo_fields,
		install_driver_app_fields,
		install_logistics_phase2_fields,
		install_geo_optimization_fields,
		install_tracking_token,
		extend_rejection_reasons,
		add_arrival_location_fields,
		install_store_geo_fields,
		install_driver_status_fields,
	):
		try:
			installer.execute()
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"ch_logistics.after_migrate {installer.__name__}",
			)
	try:
		_provision_access_control()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ch_logistics.after_migrate access_control")
	try:
		_ensure_otp_settings()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ch_logistics.after_migrate otp_settings")
	try:
		_backfill_logistics_otp_audit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ch_logistics.after_migrate otp_audit")
	try:
		seed_business_users()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ch_logistics.after_migrate seed_users")


def _provision_access_control():
	"""Ensure logistics roles exist + seed the editable role matrix once.

	Seeds CH Logistics Settings → Role Matrix from roles.DEFAULT_ROLE_MATRIX
	only for function keys that have NO rows yet, so admin edits/deletions
	are never overwritten on subsequent migrates.
	"""
	from ch_logistics.roles import DEFAULT_ROLE_MATRIX, ensure_roles

	if not frappe.db.exists("DocType", "CH Logistics Role Rule"):
		return
	ensure_roles()
	from ch_erp15.ch_erp15.default_permissions import seed_default_docperms
	seed_default_docperms({
		"Warehouse": {
			"Delivery Manager": {"read", "write"},
			"Operations Manager": {"read", "write"},
			"Logistics Head": {"read", "write"},
			"Logistic Head": {"read", "write"},
		},
	})

	settings = frappe.get_doc("CH Logistics Settings")
	seeded_keys = {row.function_key for row in (settings.get("role_matrix") or [])}
	changed = False
	for key, roles in DEFAULT_ROLE_MATRIX.items():
		if key in seeded_keys:
			continue
		for role in sorted(roles):
			if not frappe.db.exists("Role", role):
				continue  # legacy alias roles ("Logistic Head") are optional
			settings.append("role_matrix", {"function_key": key, "role": role})
			changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()


def _backfill_logistics_otp_audit():
	"""Create immutable audit rows for legacy manifest OTP evidence.

	Older successful deliveries cleared the one-time digest and retained only
	``delivery_otp_verified``. The original code cannot be reconstructed; the
	backfill records that limitation explicitly while preserving the receiver,
	delivery timestamp and modifying user already present on the manifest.
	"""
	if not frappe.db.exists("DocType", "CH Logistics OTP Log"):
		return
	manifest_meta = frappe.get_meta("CH Transfer Manifest")
	required_fields = {
		"delivery_otp", "delivery_otp_verified", "delivery_otp_log",
		"delivery_otp_generated_at", "delivery_otp_expires_at",
		"delivery_otp_verified_at", "delivery_otp_verified_by",
	}
	if any(not manifest_meta.has_field(fieldname) for fieldname in required_fields):
		return

	expiry_minutes = max(cint(
		frappe.db.get_single_value("CH Logistics Settings", "delivery_otp_expiry_minutes") or 10
	), 1)
	while True:
		rows = frappe.db.sql(
			"""
			SELECT m.name, m.trip, m.stop_sequence, m.delivery_otp,
			       m.delivery_otp_verified, m.delivery_datetime,
			       m.creation, m.modified, m.modified_by
			FROM `tabCH Transfer Manifest` m
			LEFT JOIN `tabCH Logistics OTP Log` l ON l.manifest = m.name
			WHERE l.name IS NULL
			  AND (
			    IFNULL(m.delivery_otp, '') != ''
			    OR IFNULL(m.delivery_otp_verified, 0) = 1
			  )
			ORDER BY m.creation
			LIMIT 500
			""",
			as_dict=True,
		)
		if not rows:
			break

		for row in rows:
			generated_at = row.modified or row.creation or now_datetime()
			verified = bool(cint(row.delivery_otp_verified))
			expires_at = add_to_date(generated_at, minutes=expiry_minutes)
			status = "Verified" if verified else (
				"Expired" if now_datetime() > expires_at else "Pending"
			)
			verified_at = (row.delivery_datetime or row.modified) if verified else None
			log = frappe.get_doc({
				"doctype": "CH Logistics OTP Log",
				"manifest": row.name,
				"trip": row.trip,
				"stop_sequence": row.stop_sequence,
				"request_source": "Legacy Backfill",
				"status": status,
				"otp_digest": row.delivery_otp,
				"generated_at": generated_at,
				"expires_at": expires_at,
				"generated_by": row.modified_by or "Administrator",
				"attempts": 0,
				"max_attempts": max(cint(
					frappe.db.get_single_value("CH Logistics Settings", "delivery_otp_max_attempts") or 5
				), 1),
				"verified_at": verified_at,
				"verified_by": (row.modified_by or "Administrator") if verified else None,
				"failure_reason": (
					"Backfilled from legacy manifest evidence; the original OTP was not retained."
					if verified else "Legacy OTP expired during audit migration."
				),
				"dispatch_status": "Legacy Unknown",
			})
			log.insert(ignore_permissions=True)
			updates = {
				"delivery_otp_log": log.name,
				"delivery_otp_generated_at": generated_at,
				"delivery_otp_expires_at": expires_at,
			}
			if verified:
				updates.update({
					"delivery_otp_verified_at": verified_at,
					"delivery_otp_verified_by": row.modified_by or "Administrator",
				})
			elif status == "Expired":
				updates["delivery_otp"] = None
			frappe.db.set_value(
				"CH Transfer Manifest", row.name, updates, update_modified=False
			)


def _ensure_otp_settings():
	"""Materialize safe defaults on existing Single settings records."""
	settings = frappe.get_single("CH Logistics Settings")
	changed = False
	for fieldname, default in (
		("delivery_otp_expiry_minutes", 10),
		("delivery_otp_max_attempts", 5),
	):
		if cint(settings.get(fieldname)) <= 0:
			settings.set(fieldname, default)
			changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save()


# ─────────────────────────────────────────────────────────────────────────────
# Business user seeding — logistics rollout
#
# The seed reuses ONLY pre-existing Roles and Role Profiles; it invents no
# authorization objects. It is idempotent and additive: an existing user
# gains missing roles / scope rows, nothing is ever removed. Runs on every
# migrate so the same users land on prod unchanged.
#
# Function mapping (business-provided, 2026-09-02):
#   Warehouse Outward (packs + creates manifests) → Role Profile "CH Hub
#     Operator" (Stock Manager grants create_manifest in the role matrix)
#     + explicit all-company scope (cross-company hub).
#   Logistics Manager → Role Profile "CH Logistics Coordinator" (Delivery
#     Manager = ops_control/ops_view; also in the app_access and
#     rejection_dispatcher_notify matrix defaults) + explicit all-company
#     scope.
#   Delivery Executive → roles "Driver" + "Delivery User" (the delivery-app
#     Page requires Delivery User; driver endpoints resolve via Driver.user)
#     + a linked Driver record. No scope rows: driver paths filter by
#     assignment, and empty scope stays fail-closed everywhere else.
#   Store account → Role Profile "CH Store Manager" (Store Manager =
#     accept_delivery/close_manifest) + scope on its own store only.
# ─────────────────────────────────────────────────────────────────────────────

LOGISTICS_SEED_USERS = (
	{
		"email": "hemanth.n@congruenceholdings.in",
		"first_name": "Hemanth",
		"last_name": "N",
		"role_profile": "CH Hub Operator",
		"scope_companies": "ALL",
	},
	{
		# Roles are profile-managed on scoped users (CH User Scope.on_update
		# rebuilds User.role_profiles; hand-added roles are dropped by
		# design) — so the Logistics Manager surface comes from the role
		# matrix instead: Delivery Manager is in app_access and
		# rejection_dispatcher_notify defaults.
		"email": "sathish.k@congruenceholdings.in",
		"first_name": "Sathish",
		"last_name": "K",
		"role_profile": "CH Logistics Coordinator",
		"scope_companies": "ALL",
	},
	{
		"email": "sameerathameem.85.st@gmail.com",
		"first_name": "Sameera",
		"last_name": "Thameem",
		"roles": ("Driver", "Delivery User"),
		"driver": True,
	},
	{
		"email": "saravananpubg886@gmail.com",
		"first_name": "Saravanan",
		"roles": ("Driver", "Delivery User"),
		"driver": True,
	},
	{
		"email": "ambattur@gofix.co.in",
		"first_name": "GoFix Ambattur Store",
		"role_profile": "CH Store Manager",
		"scope_stores": ("GF-AMBATTUR",),
	},
)


def seed_business_users():
	"""Create/complete the business-provided logistics users. Idempotent."""
	for spec in LOGISTICS_SEED_USERS:
		try:
			_seed_one_user(spec)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"seed_business_users: {spec.get('email')}",
			)
	frappe.db.commit()


def _seed_one_user(spec: dict):
	email = spec["email"]
	profile = spec.get("role_profile")
	if profile and not frappe.db.exists("Role Profile", profile):
		frappe.log_error(
			f"Role Profile {profile} missing; skipped user {email}",
			"seed_business_users",
		)
		return

	if not frappe.db.exists("User", email):
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = spec.get("first_name") or email.split("@")[0].title()
		if spec.get("last_name"):
			user.last_name = spec["last_name"]
		user.user_type = "System User"
		user.enabled = 1
		user.send_welcome_email = 0
		if profile:
			user.role_profile_name = profile
		user.flags.no_welcome_mail = True
		user.flags.ignore_permissions = True
		user.insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)
		if profile and not user.role_profile_name:
			user.role_profile_name = profile
			user.flags.ignore_permissions = True
			user.save(ignore_permissions=True)

	for role in spec.get("roles") or ():
		if not frappe.db.exists("Role", role):
			frappe.log_error(f"Role {role} missing; not granted to {email}", "seed_business_users")
			continue
		if not frappe.db.exists("Has Role", {"parent": email, "parenttype": "User", "role": role}):
			user.reload()
			user.append("roles", {"role": role})
			user.flags.ignore_permissions = True
			user.save(ignore_permissions=True)

	scope_profile = spec.get("scope_role_profile") or profile
	wants_scope = spec.get("scope_companies") or spec.get("scope_stores")
	if wants_scope and scope_profile:
		_seed_user_scope(email, scope_profile, spec)

	if spec.get("driver") and not frappe.db.exists("Driver", {"user": email}):
		driver = frappe.new_doc("Driver")
		driver.full_name = " ".join(
			part for part in (spec.get("first_name"), spec.get("last_name")) if part
		) or email.split("@")[0].title()
		driver.status = "Active"
		driver.user = email
		driver.flags.ignore_permissions = True
		driver.insert(ignore_permissions=True)


def _seed_user_scope(email: str, scope_profile: str, spec: dict):
	if frappe.db.exists("CH User Scope", email):
		scope = frappe.get_doc("CH User Scope", email)
	else:
		scope = frappe.new_doc("CH User Scope")
		scope.user = email
		scope.role_profile = scope_profile
	scope.enabled = 1

	if spec.get("scope_companies") == "ALL":
		wanted = set(frappe.get_all("Company", pluck="name"))
	else:
		wanted = set(spec.get("scope_companies") or ())
	existing = {row.company for row in (scope.get("companies") or [])}
	for company in sorted(wanted - existing):
		if frappe.db.exists("Company", company):
			scope.append("companies", {"company": company})

	existing_stores = {row.store for row in (scope.get("stores") or [])}
	for store in spec.get("scope_stores") or ():
		if store not in existing_stores and frappe.db.exists("CH Store", store):
			scope.append("stores", {"store": store})

	scope.flags.ignore_permissions = True
	scope.save(ignore_permissions=True)
	try:
		from ch_erp15.ch_erp15.scope import clear_scope_cache

		clear_scope_cache(email)
	except Exception:
		pass
