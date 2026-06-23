frappe.query_reports["Manifest Delivery and SLA"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -1) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "status", label: __("Status"), fieldtype: "Select",
		  options: ["", "Draft", "Packed", "Assigned", "Pickup Started", "Rejected",
			"In Transit", "Delivered", "Partially Received", "Received", "Closed",
			"Cancelled", "Returned"].join("\n") },
		{ fieldname: "destination_store", label: __("Destination Store"), fieldtype: "Link", options: "CH Store" },
		{ fieldname: "source_store", label: __("Source Store"), fieldtype: "Link", options: "CH Store" },
		{ fieldname: "driver", label: __("Driver"), fieldtype: "Link", options: "Driver" },
		{ fieldname: "overdue_only", label: __("Overdue Only"), fieldtype: "Check" },
	],
};
