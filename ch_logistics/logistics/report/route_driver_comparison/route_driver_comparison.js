frappe.query_reports["Route Driver Comparison"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -1) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "route", label: __("Route"), fieldtype: "Link", options: "CH Route" },
		{ fieldname: "driver", label: __("Driver"), fieldtype: "Link", options: "Driver" },
		{ fieldname: "hub_warehouse", label: __("Hub Warehouse"), fieldtype: "Link", options: "Warehouse" },
		{ fieldname: "include_unassigned_route", label: __("Include Unassigned Route"),
		  fieldtype: "Check", default: 0,
		  description: __("Show trips that have no CH Route set — useful during route-master seeding.") },
	],
};
