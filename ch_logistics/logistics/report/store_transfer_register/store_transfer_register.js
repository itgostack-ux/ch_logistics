frappe.query_reports["Store Transfer Register"] = {
	filters: [
		{ fieldname: "store", label: __("Store"), fieldtype: "Link", options: "CH Store", reqd: 0 },
		{ fieldname: "lens", label: __("View"), fieldtype: "Select",
		  options: ["Both", "Inbound", "Outbound"].join("\n"), default: "Both" },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -1) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "status", label: __("Status"), fieldtype: "Select",
		  options: ["", "Packed", "Assigned", "In Transit", "Delivered",
			"Partially Received", "Received", "Closed", "Rejected", "Returned"].join("\n") },
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
	],
};
