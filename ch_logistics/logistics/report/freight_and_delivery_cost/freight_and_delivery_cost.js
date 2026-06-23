frappe.query_reports["Freight and Delivery Cost"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -1) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "source_store", label: __("Source Store"), fieldtype: "Link", options: "CH Store" },
		{ fieldname: "destination_store", label: __("Destination Store"), fieldtype: "Link", options: "CH Store" },
	],
};
