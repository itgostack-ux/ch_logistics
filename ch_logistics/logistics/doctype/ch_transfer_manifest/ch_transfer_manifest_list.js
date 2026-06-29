// Copyright (c) 2026, GoGizmo and contributors
// For license information, please see license.txt
/* CH Transfer Manifest — list view customisation
 *
 * Goals (managers / dispatch heads):
 *  - colour-coded status badges (not the same-old grey "title" line)
 *  - urgent / critical priority chip
 *  - e-Way Bill state at a glance
 *  - SLA hint when estimated_delivery_date is past
 *  - quick filter chips at the top of the list (Today / In Transit /
 *    Awaiting EWB / SLA Breach / Damage Reported)
 */

frappe.listview_settings["CH Transfer Manifest"] = {
	// Pull these in addition to the in_list_view columns so the formatters
	// and indicators below have everything they need.
	add_fields: [
		"status",
		"direction",
		"shipment_priority",
		"trip",
		"driver",
		"driver_name",
		"courier_partner",
		"tracking_number",
		"vehicle_number",
		"source_warehouse",
		"destination_warehouse",
		"source_store",
		"destination_store",
		"estimated_delivery_date",
		"ewaybill_status",
		"ewaybill_count",
		"total_qty",
		"total_items",
		"box_count",
		"damage_reported",
		"total_weight_kg",
		"freight_amount",
	],

	// Row indicator (the dot on the very left of each row).
	get_indicator(doc) {
		// Damage / recall override
		if (doc.damage_reported) return [__("Damage Reported"), "red", "damage_reported,=,1"];
		if (doc.status === "Recall Initiated") return [__("Recall Initiated"), "red", "status,=,Recall Initiated"];
		if (doc.status === "Returned") return [__("Returned"), "gray", "status,=,Returned"];

		// SLA breach — past ETA and not yet delivered
		const eta = doc.estimated_delivery_date;
		const open_states = ["Draft", "Packed", "Assigned", "Pickup Started", "In Transit"];
		if (eta && open_states.includes(doc.status)) {
			const eta_dt = frappe.datetime.str_to_obj(eta);
			if (eta_dt && eta_dt.getTime() < Date.now()) {
				return [__("SLA Breach"), "red", `status,in,${open_states.join("|")}|estimated_delivery_date,<,${frappe.datetime.now_datetime()}`];
			}
		}

		const map = {
			"Draft":             ["Draft",             "gray"],
			"Packed":            ["Packed",            "blue"],
			"Assigned":          ["Assigned",          "purple"],
			"Pickup Started":    ["Pickup Started",    "orange"],
			"In Transit":        ["In Transit",        "orange"],
			"Delivered":         ["Delivered",         "cyan"],
			"Partially Received":["Partially Received","yellow"],
			"Received":          ["Received",          "green"],
			"Closed":            ["Closed",            "green"],
			"Rejected":          ["Rejected",          "red"],
			"Cancelled":         ["Cancelled",         "red"],
		};
		const entry = map[doc.status] || ["Unknown", "gray"];
		return [__(entry[0]), entry[1], `status,=,${doc.status}`];
	},

	// Per-column formatters. Frappe calls these for the fields it renders
	// in the row. Keep them tight — one-liners with emphasis chips.
	formatters: {
		status(value) {
			const pal = {
				"Draft":              "gray",
				"Packed":             "blue",
				"Assigned":           "purple",
				"Pickup Started":     "orange",
				"In Transit":         "orange",
				"Delivered":          "cyan",
				"Partially Received": "yellow",
				"Received":           "green",
				"Closed":             "green",
				"Rejected":           "red",
				"Recall Initiated":   "red",
				"Returned":           "gray",
				"Cancelled":          "red",
			};
			const color = pal[value] || "gray";
			return `<span class="indicator-pill no-margin ${color}">${frappe.utils.escape_html(value || "")}</span>`;
		},

		shipment_priority(value) {
			if (!value || value === "Normal") return `<span class="text-muted">${value || "—"}</span>`;
			const cls = value === "Critical" ? "red" : "orange";
			const icon = value === "Critical" ? "fa-bolt" : "fa-arrow-up";
			return `<span class="indicator-pill no-margin ${cls}"><i class="fa ${icon}" style="margin-right:2px"></i>${value}</span>`;
		},

		ewaybill_status(value) {
			if (!value) return "—";
			const pal = {
				"Not Required":   "gray",
				"Not Generated":  "orange",
				"Generating":     "blue",
				"Partial":        "yellow",
				"Generated":      "green",
				"Failed":         "red",
			};
			const color = pal[value] || "gray";
			return `<span class="indicator-pill no-margin ${color}">${frappe.utils.escape_html(value)}</span>`;
		},

		direction(value) {
			if (!value) return "";
			const sym = value === "Reverse" ? "↩" : value === "Mixed" ? "↔" : "→";
			return `<span title="${value}">${sym} ${value}</span>`;
		},

		total_qty(value, _df, doc) {
			const qty = flt(value || 0);
			const boxes = cint(doc && doc.box_count);
			if (!qty && !boxes) return `<span class="text-muted">—</span>`;
			const parts = [];
			if (qty) parts.push(`<b>${qty}</b> ${__("qty")}`);
			if (boxes) parts.push(`${boxes} ${__("box")}`);
			return parts.join(" · ");
		},

		estimated_delivery_date(value, _df, doc) {
			if (!value) return `<span class="text-muted">—</span>`;
			const eta_dt = frappe.datetime.str_to_obj(value);
			const open_states = ["Draft", "Packed", "Assigned", "Pickup Started", "In Transit"];
			const breached = eta_dt && open_states.includes(doc && doc.status) && eta_dt.getTime() < Date.now();
			const display = frappe.datetime.str_to_user(value);
			if (breached) {
				return `<span style="color:#dc2626;font-weight:600" title="${__("Past estimated delivery")}">
					<i class="fa fa-exclamation-triangle"></i> ${display}
				</span>`;
			}
			return display;
		},

		trip(value) {
			if (!value) return `<span class="text-muted">—</span>`;
			return `<a href="/app/ch-logistics-trip/${encodeURIComponent(value)}">${frappe.utils.escape_html(value)}</a>`;
		},
	},

	// Quick-filter buttons under the page heading.
	onload(listview) {
		const today = frappe.datetime.get_today();

		listview.page.add_inner_button(__("Today"), () => {
			listview.filter_area.clear();
			listview.filter_area.add([
				["CH Transfer Manifest", "manifest_date", "=", today],
			]);
		}, __("Quick Filters"));

		listview.page.add_inner_button(__("In Transit"), () => {
			listview.filter_area.clear();
			listview.filter_area.add([
				["CH Transfer Manifest", "status", "in",
					["Assigned", "Pickup Started", "In Transit"]],
			]);
		}, __("Quick Filters"));

		listview.page.add_inner_button(__("SLA Breach"), () => {
			listview.filter_area.clear();
			listview.filter_area.add([
				["CH Transfer Manifest", "status", "in",
					["Draft", "Packed", "Assigned", "Pickup Started", "In Transit"]],
				["CH Transfer Manifest", "estimated_delivery_date", "<",
					frappe.datetime.now_datetime()],
			]);
		}, __("Quick Filters"));

		listview.page.add_inner_button(__("Awaiting EWB"), () => {
			listview.filter_area.clear();
			listview.filter_area.add([
				["CH Transfer Manifest", "ewaybill_status", "in",
					["Not Generated", "Generating", "Partial", "Failed"]],
			]);
		}, __("Quick Filters"));

		listview.page.add_inner_button(__("Damage Reported"), () => {
			listview.filter_area.clear();
			listview.filter_area.add([
				["CH Transfer Manifest", "damage_reported", "=", 1],
			]);
		}, __("Quick Filters"));

		listview.page.add_inner_button(__("High Priority"), () => {
			listview.filter_area.clear();
			listview.filter_area.add([
				["CH Transfer Manifest", "shipment_priority", "in",
					["Urgent", "Critical"]],
			]);
		}, __("Quick Filters"));
	},

	// Tighter primary action label
	primary_action: null,
};
