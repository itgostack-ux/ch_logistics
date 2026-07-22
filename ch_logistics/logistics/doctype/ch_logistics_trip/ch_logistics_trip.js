// CH Logistics Trip — Desk client script
// Adds action buttons to drive the status state machine since the
// status field is read-only (controlled server-side via _enforce_status_transition).

frappe.ui.form.on("CH Logistics Trip", {
	refresh(frm) {
		// Resolve role capabilities server-side (CH Logistics Settings → Role
		// Matrix) once, then paint the buttons. Cosmetic only — trip_close
		// re-checks head_override on the server.
		if (frm._ch_caps === undefined) {
			frm._ch_caps = null;
			frappe.xcall("ch_logistics.roles.get_my_capabilities")
				.then((caps) => {
					frm._ch_caps = caps || {};
					frm.clear_custom_buttons();
					frm.trigger("setup_status_buttons");
				})
				.catch(() => { frm._ch_caps = {}; });
		}
		frm.trigger("setup_status_buttons");
		frm.trigger("set_status_indicator");
	},

	setup_status_buttons(frm) {
		if (frm.is_new()) return;

		const status = frm.doc.status;
		const is_logistics_head = !!(frm._ch_caps || {}).head_override;

		// Route optimization — only before the driver starts executing.
		if ((status === "Draft" || status === "Assigned") && (frm.doc.stops || []).length > 1) {
			frm.add_custom_button(__("Optimize Route"), () => {
				frappe.xcall("ch_logistics.api.optimizer.optimize_trip", { trip: frm.doc.name })
					.then((r) => {
						frappe.show_alert({
							message: __("Route optimized: {0} km → {1} km (saved {2} km, {3}%)",
								[r.distance_before_km, r.distance_after_km, r.distance_saved_km, r.saved_pct]),
							indicator: "green",
						}, 8);
						frm.reload_doc();
					});
			});
		}

		// Recompute live ETA from the driver's current position.
		if ((status === "Assigned" || status === "Started") && (frm.doc.stops || []).length) {
			frm.add_custom_button(__("Re-sequence Stops"), () => {
				const label = status === "Started"
					? __("Re-sequence remaining pending stops using current driver position?")
					: __("Re-sequence planned stops now?");
				frappe.confirm(label, () => {
					frappe.xcall("ch_logistics.api.optimizer.resequence_trip", { trip: frm.doc.name })
						.then((r) => {
							frappe.show_alert({
								message: __("Resequenced: saved {0} km ({1}%)", [r.distance_saved_km, r.saved_pct]),
								indicator: "green",
							}, 8);
							frm.reload_doc();
						});
				});
			}, __("Actions"));

			frm.add_custom_button(__("Recompute ETA"), () => {
				frappe.xcall("ch_logistics.api.optimizer.compute_trip_eta", { trip: frm.doc.name })
					.then((r) => {
						frappe.show_alert({
							message: __("ETA updated for {0} stop(s).", [r.updated]),
							indicator: "blue",
						});
						frm.reload_doc();
					});
			}, __("Actions"));
		}

		// Draft → Assigned (requires driver)
		if (status === "Draft") {
			frm.add_custom_button(__("Assign Driver"), () => {
				frm.trigger("prompt_assign_driver");
			}, __("Actions"));
		}

		// Assigned → Started
		if (status === "Assigned") {
			frm.add_custom_button(__("Reassign Driver"), () => {
				frm.trigger("prompt_assign_driver");
			}, __("Actions"));

			frm.add_custom_button(__("Unassign Driver"), () => {
				frappe.confirm(
					__("Unassign the current driver and revert trip to Draft?"),
					() => {
						frappe.xcall("ch_logistics.api.logistics_api.trip_unassign", {
							trip: frm.doc.name,
						}).then(() => frm.reload_doc());
					}
				);
			}, __("Actions"));

			frm.add_custom_button(__("Start Trip"), () => {
				frappe.confirm(
					__("Mark this trip as Started?"),
					() => {
						frappe.xcall("ch_logistics.api.logistics_api.trip_start", {
							trip: frm.doc.name,
						}).then(() => frm.reload_doc());
					}
				);
			});
			frm.page.btn_primary.text(__("Start Trip")).show();
		}

		// Started → Completed
		if (status === "Started") {
			frm.add_custom_button(__("Complete Trip"), () => {
				frappe.confirm(
					__("Mark this trip as Completed?"),
					() => {
						frappe.xcall("ch_logistics.api.logistics_api.trip_complete", {
							trip: frm.doc.name,
						}).then(() => frm.reload_doc());
					}
				);
			});

			if (is_logistics_head) {
				frm.add_custom_button(__("Close Trip (Logistics Head)"), () => {
					frappe.confirm(
						__("Close this trip now? Allowed only when all manifests are Closed/Delivered/Cancelled."),
						() => {
							frappe.xcall("ch_logistics.api.logistics_api.trip_close", {
								trip: frm.doc.name,
								close_as_head: 1,
							}).then(() => frm.reload_doc());
						}
					);
				}, __("Actions"));
			}
		}

		// Completed → Closed
		if (status === "Completed") {
			frm.add_custom_button(__("Close Trip"), () => {
				frappe.confirm(
					__("Close this trip? This is a final state."),
					() => {
						frappe.xcall("ch_logistics.api.logistics_api.trip_close", {
							trip: frm.doc.name,
						}).then(() => frm.reload_doc());
					}
				);
			});
		}

		// Draft or Assigned → Cancelled
		if (["Draft", "Assigned"].includes(status)) {
			frm.add_custom_button(__("Cancel Trip"), () => {
				frappe.confirm(
					__("Cancel this trip?"),
					() => {
						frappe.xcall("ch_logistics.api.logistics_api.trip_cancel", {
							trip: frm.doc.name,
						}).then(() => frm.reload_doc());
					}
				);
			}, __("Actions"));
		}
	},

	set_status_indicator(frm) {
		const map = {
			Draft: "grey",
			Assigned: "blue",
			Started: "orange",
			Completed: "green",
			Closed: "darkgrey",
			Cancelled: "red",
		};
		const color = map[frm.doc.status] || "grey";
		frm.page.set_indicator(frm.doc.status, color);
	},

	prompt_assign_driver(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Assign Driver"),
			fields: [
				{
					fieldname: "driver",
					fieldtype: "Link",
					label: __("Driver"),
					options: "Driver",
					reqd: 1,
					default: frm.doc.driver || "",
				},
				{
					fieldname: "vehicle",
					fieldtype: "Link",
					label: __("Vehicle"),
					options: "Vehicle",
					default: frm.doc.vehicle || "",
				},
			],
			primary_action_label: __("Assign"),
			primary_action(values) {
				d.hide();
				frappe.xcall("ch_logistics.api.logistics_api.trip_assign_driver", {
					trip: frm.doc.name,
					driver: values.driver,
					vehicle: values.vehicle || null,
				}).then(() => frm.reload_doc());
			},
		});
		d.show();
	},
});
