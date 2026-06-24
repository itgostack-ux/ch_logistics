frappe.query_reports["Delivery Exceptions and Rejections"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -1) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "record_type", label: __("Type"), fieldtype: "Select",
		  options: ["", "Rejection", "Exception"].join("\n") },
		{ fieldname: "driver", label: __("Driver"), fieldtype: "Link", options: "Driver" },
	],
};
