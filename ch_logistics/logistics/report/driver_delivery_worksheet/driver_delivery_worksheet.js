frappe.query_reports["Driver Delivery Worksheet"] = {
	filters: [
		{ fieldname: "date", label: __("Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "driver", label: __("Driver"), fieldtype: "Link", options: "Driver",
		  description: __("Delivery staff: leave blank to see your own worksheet.") },
		{ fieldname: "status", label: __("Status"), fieldtype: "Select",
		  options: ["", "Assigned", "Pickup Started", "In Transit", "Rejected",
			"Delivered", "Received", "Closed"].join("\n") },
	],
};
