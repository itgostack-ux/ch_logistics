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
