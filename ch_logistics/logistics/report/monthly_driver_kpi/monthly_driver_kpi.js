frappe.query_reports["Monthly Driver KPI"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
		  default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date",
		  default: frappe.datetime.add_months(frappe.datetime.get_today(), -6) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date",
		  default: frappe.datetime.get_today() },
		{ fieldname: "driver", label: __("Driver"), fieldtype: "Link", options: "Driver" },
	],
	formatter(value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		// Highlight on-time % cells:
		//   >= 95% → green,  85..95% → amber,  < 85% → red.
		// Mirrors SAP TM ADT default KPI heat-map thresholds.
		if (column.fieldname === "on_time_pct" && value != null && value !== "") {
			let v = parseFloat(value);
			let color = v >= 95 ? "green" : (v >= 85 ? "orange" : "red");
			formatted = `<span style="color:${color};font-weight:600">${formatted}</span>`;
		}
		return formatted;
	},
};
