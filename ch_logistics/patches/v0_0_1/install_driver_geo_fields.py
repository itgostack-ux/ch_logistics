"""Install GPS custom fields on the upstream Driver doctype.

These fields are owned by ``ch_logistics`` (module="Logistics") so the
fixture export from this app picks them up and they migrate cleanly with
the app.

Idempotent — safe to re-run from after_install AND after_migrate.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


MODULE = "Logistics"

DRIVER_GEO_FIELDS = {
	"Driver": [
		{
			"fieldname": "geo_section_break",
			"label": "Live Location",
			"fieldtype": "Section Break",
			"insert_after": "transporter",
			"collapsible": 1,
			"module": MODULE,
		},
		{
			"fieldname": "current_lat",
			"label": "Current Latitude",
			"fieldtype": "Float",
			"precision": "7",
			"read_only": 1,
			"insert_after": "geo_section_break",
			"module": MODULE,
		},
		{
			"fieldname": "current_lng",
			"label": "Current Longitude",
			"fieldtype": "Float",
			"precision": "7",
			"read_only": 1,
			"insert_after": "current_lat",
			"module": MODULE,
		},
		{
			"fieldname": "last_geo_at",
			"label": "Last Position At",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "current_lng",
			"module": MODULE,
		},
		{
			"fieldname": "current_speed_kmh",
			"label": "Current Speed (km/h)",
			"fieldtype": "Float",
			"precision": "2",
			"read_only": 1,
			"insert_after": "last_geo_at",
			"module": MODULE,
		},
		{
			"fieldname": "current_heading",
			"label": "Current Heading (deg)",
			"fieldtype": "Float",
			"precision": "2",
			"read_only": 1,
			"insert_after": "current_speed_kmh",
			"module": MODULE,
		},
	],
}


def execute() -> None:
	if not frappe.db.exists("DocType", "Driver"):
		# Fresh ERPNext install without the Driver doctype — nothing to do.
		return
	create_custom_fields(DRIVER_GEO_FIELDS, update=True)
	frappe.db.commit()
