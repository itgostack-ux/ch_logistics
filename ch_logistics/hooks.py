app_name = "ch_logistics"
app_title = "CH Logistics"
app_publisher = "GoStack"
app_description = "Logistics driver app, trip management, live GPS tracking & manifest delivery"
app_email = "admin@gofix.in"
app_license = "MIT"

required_apps = ["frappe", "erpnext", "ch_erp15"]

# Apps screen entry — opens the live fleet map
add_to_apps_screen = [
	{
		"name": "ch_logistics",
		"logo": "/assets/ch_logistics/images/logo.svg",
		"title": "Live Fleet",
		"route": "/app/live-fleet-map",
		"has_permission": "ch_logistics.api.permissions.has_logistics_access",
	}
]

# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------
app_include_js = [
	"/assets/ch_logistics/js/google_maps_loader.js",
]
app_include_css = [
	"/assets/ch_logistics/css/ch_logistics.css",
]

# --------------------------------------------------------------------------
# Migration hooks
# --------------------------------------------------------------------------
after_install = "ch_logistics.setup.after_install"
after_migrate = "ch_logistics.setup.after_migrate"

# --------------------------------------------------------------------------
# Session hooks — keep the operational driver-status machine
# (ch_logistics.api.driver_status) aligned with Frappe's own session
# lifecycle. on_login Offline → Available, on_logout → Offline (force).
# Both are no-ops for users with no linked Driver record.
# --------------------------------------------------------------------------
on_login = "ch_logistics.api.driver_status.handle_user_login"
on_logout = "ch_logistics.api.driver_status.handle_user_logout"

# --------------------------------------------------------------------------
# Document events
# --------------------------------------------------------------------------
doc_events = {
	# Keep a store's warehouses (Sellable + group + bins) in sync with its
	# coordinates on every save, so the map/delivery layers never show blanks.
	"CH Store": {
		"on_update": "ch_logistics.api.optimizer.sync_one_store_geo",
	},
}

permission_query_conditions = {
	"CH Logistics Trip": "ch_logistics.api.permissions.trip_query_condition",
	"CH Driver Device": "ch_logistics.api.permissions.driver_device_query_condition",
	"CH Driver Break Log": "ch_logistics.api.permissions.driver_break_query_condition",
	"CH Driver Location": "ch_logistics.api.permissions.driver_location_query_condition",
	"CH Manifest Rejection": "ch_logistics.api.permissions.manifest_rejection_query_condition",
}

has_permission = {
	"CH Logistics Trip": "ch_logistics.api.permissions.has_trip_permission",
	"CH Driver Device": "ch_logistics.api.permissions.has_driver_device_permission",
	"CH Driver Break Log": "ch_logistics.api.permissions.has_driver_break_permission",
	"CH Driver Location": "ch_logistics.api.permissions.has_driver_location_permission",
	"CH Manifest Rejection": "ch_logistics.api.permissions.has_manifest_rejection_permission",
}

# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------
scheduler_events = {
	"daily": [
		# Purge driver location pings older than the retention window
		# (default 30 days, configurable in CH Tracking Settings)
		"ch_logistics.api.tracking_api.purge_old_locations",
	],
	"cron": {
		# Mark drivers offline if no ping in 10 minutes
		"*/5 * * * *": [
			"ch_logistics.api.tracking_api.mark_stale_drivers_offline",
		],
		# Predictive ETA + SLA-breach early warning for in-progress trips.
		"*/10 * * * *": [
			"ch_logistics.api.optimizer.check_eta_sla_breaches",
		],
		# Daily logistics digest to managers (server-time hour; gated by
		# CH Logistics Settings → Send Daily Logistics Digest).
		"0 3 * * *": [
			"ch_logistics.api.digest.send_logistics_daily_digest",
		],
	},
}

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
fixtures = [
	{
		"dt": "Custom Field",
		"filters": [["module", "=", "Logistics"]],
	},
]
