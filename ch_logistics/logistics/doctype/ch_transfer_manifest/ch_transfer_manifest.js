frappe.ui.form.on("CH Transfer Manifest", {
    refresh(frm) {
        render_status_badge(frm);
        render_lane_banner(frm);
        add_action_buttons(frm);
        add_print_label_button(frm);
        add_pack_box_button(frm);
    },

    source_store(frm) {
        if (frm.doc.source_store) {
            frappe.db.get_value("CH Store", frm.doc.source_store, "warehouse", (r) => {
                if (r && r.warehouse) frm.set_value("source_warehouse", r.warehouse);
            });
        }
    },

    destination_store(frm) {
        if (frm.doc.destination_store) {
            frappe.db.get_value("CH Store", frm.doc.destination_store, "warehouse", (r) => {
                if (r && r.warehouse) frm.set_value("destination_warehouse", r.warehouse);
            });
        }
    },
});

// One-click: print the box label (FROM→TO + scannable QR = manifest number).
// Lifecycle gate (SAP EWM / Manhattan Active WMS / Oracle WMS Cloud parity):
// carton labels are an operational artefact of the pack ↔ dispatch phase.
// Once goods are in motion (In Transit) or delivered, labels are sealed on
// the carton and re-printing creates an audit-trail conflict, so the form
// hides the prominent shortcut.  Historical reprint stays available via the
// standard Print menu (Frappe page "Print" icon).
const BOX_LABEL_STATES = new Set(["Draft", "Packed", "Assigned"]);

function add_print_label_button(frm) {
    if (frm.is_new()) return;
    if (frm.doc.docstatus === 2) return;        // cancelled — nothing to label
    if (!BOX_LABEL_STATES.has(frm.doc.status)) return;
    frm.add_custom_button(__("Print Box Label"), () => {
        const params = new URLSearchParams({
            doctype: frm.doctype,
            name: frm.doc.name,
            format: "CH Transfer Manifest Label",
            trigger_print: "1",
            _lang: frappe.boot.lang || "en",
        });
        window.open("/printview?" + params.toString(), "_blank");
    });
    // Promote to primary CTA only when no other stage-action owns the slot
    // (i.e. Packed/Assigned where the next manual step is to print + hand
    // off cartons).  On Draft, submit remains primary.
    if (frm.doc.status === "Packed" || frm.doc.status === "Assigned") {
        frm.page.btn_secondary && frm.page.btn_secondary.removeClass("btn-primary");
        const $btn = frm.page.inner_toolbar.find(`.btn:contains('${__("Print Box Label")}')`);
        $btn.addClass("btn-primary");
    }
}

// Oracle WMS-style "Pack Box" pack-station shortcut. One click opens a
// quick-capture dialog (qty / weight / dimensions / seal / photo / notes),
// appends a packing-slip row with an auto-generated LPN, and lets the
// packer mint the next box without scrolling through the grid.
function add_pack_box_button(frm) {
    if (frm.is_new()) return;
    if (frm.doc.docstatus !== 0) return;     // packing is a Draft-only activity
    frm.add_custom_button(__("Pack Box"), () => show_pack_box_dialog(frm), __("Packing"));
}

function show_pack_box_dialog(frm) {
    const next_seq = ((frm.doc.packages || []).length || 0) + 1;
    const suggested_label = `${frm.doc.name}-B${String(next_seq).padStart(2, "0")}`;
    const total_qty = frm.doc.total_qty || 0;
    const packed_so_far = (frm.doc.packages || []).reduce(
        (s, p) => s + (p.packed_qty || 0), 0
    );
    const remaining = Math.max(0, total_qty - packed_so_far);

    const d = new frappe.ui.Dialog({
        title: __("Pack Box — {0}", [suggested_label]),
        fields: [
            {
                fieldname: "summary_html",
                fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:8px 12px;border-radius:6px;background:#d1ecf1;border:1px solid #bee5eb;font-size:13px">
                    <b>${__("Manifest total qty")}:</b> ${total_qty} &middot;
                    <b>${__("Packed so far")}:</b> ${packed_so_far} &middot;
                    <b>${__("Remaining")}:</b> ${remaining}
                </div>`,
            },
            { fieldname: "packed_qty", fieldtype: "Int", label: __("Packed Qty"), reqd: 1,
              default: remaining || null,
              description: __("How many item units are physically in this box? Max: {0} (remaining on manifest).", [remaining]) },
            { fieldname: "weight_kg", fieldtype: "Float", label: __("Weight (kg)") },
            { fieldname: "dimensions_cm", fieldtype: "Data", label: __("Dimensions (LxWxH cm)"),
              description: __("Optional — used for courier dimensional weight, e.g. 30x20x15") },
            { fieldname: "col_break", fieldtype: "Column Break" },
            { fieldname: "seal_number", fieldtype: "Data", label: __("Seal / Tamper Tag") },
            { fieldname: "packing_photo", fieldtype: "Attach Image", label: __("Packing Photo") },
            { fieldname: "notes", fieldtype: "Small Text", label: __("Notes") },
        ],
        primary_action_label: __("Add Box & Save"),
        primary_action(values) {
            // Client-side overpack guard so the user sees a clear message
            // before we hit the server. The controller's _validate_packing()
            // is the source-of-truth guard for direct form edits / API calls.
            const req = Number(values.packed_qty || 0);
            if (req > remaining) {
                frappe.msgprint({
                    title: __("Overpack Blocked"),
                    indicator: "red",
                    message: __("Cannot pack {0} units — only {1} remaining on this manifest (total {2}, already packed {3}).",
                        [req, remaining, total_qty, packed_so_far]),
                });
                return;
            }
            const row = frm.add_child("packages", {
                package_label: suggested_label,
                packed_qty: values.packed_qty,
                weight_kg: values.weight_kg,
                dimensions_cm: values.dimensions_cm,
                seal_number: values.seal_number,
                packing_photo: values.packing_photo,
                notes: values.notes,
                packed_by: frappe.session.user,
                packed_at: frappe.datetime.now_datetime(),
            });
            frm.refresh_field("packages");
            d.hide();
            frm.save().then(() => {
                frappe.show_alert({
                    message: __("Box {0} packed ({1} units).", [row.package_label, values.packed_qty || 0]),
                    indicator: "green",
                }, 5);
            });
        },
    });
    d.show();
}

frappe.ui.form.on("CH Transfer Manifest Item", {
    stock_entry(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        if (!row.stock_entry) return;
        frappe.call({
            method: "frappe.client.get",
            args: { doctype: "Stock Entry", name: row.stock_entry },
            callback(r) {
                if (!r.message) return;
                let se = r.message;
                frappe.model.set_value(cdt, cdn, "from_warehouse", se.from_warehouse);
                frappe.model.set_value(cdt, cdn, "to_warehouse", se.to_warehouse);
                frappe.model.set_value(cdt, cdn, "material_request", se.material_request);
                frappe.model.set_value(cdt, cdn, "transfer_status", se.custom_status || "Draft");
                let item_count = (se.items || []).length;
                let total_qty = (se.items || []).reduce((s, i) => s + (i.qty || 0), 0);
                frappe.model.set_value(cdt, cdn, "item_count", item_count);
                frappe.model.set_value(cdt, cdn, "total_qty", total_qty);
            }
        });
    }
});

function render_status_badge(frm) {
    if (!frm.doc.status) return;
    const colors = {
        "Draft": "gray", "Packed": "blue", "Assigned": "orange",
        "Pickup Started": "yellow", "In Transit": "blue",
        "Delivered": "purple", "Received": "green", "Closed": "darkgray",
        "Recall Initiated": "red", "Returned": "gray",
        "Cancelled": "red",
    };
    frm.page.set_indicator(frm.doc.status, colors[frm.doc.status] || "gray");

    if (frm.doc.driver_name) {
        frm.dashboard.set_headline(
            `<span class="indicator-pill ${colors[frm.doc.status] || "gray"}">
                <i class="fa fa-truck"></i> ${frm.doc.driver_name}
                ${frm.doc.driver_phone ? " &middot; " + frm.doc.driver_phone : ""}
            </span>`
        );
    }
}

// Phase B lane banner — classifies the manifest as Outward (source-store
// dispatch lane) or Inward (destination-store receipt lane) so staff at each
// end see at-a-glance whether they own the next action. Pure UX overlay; no
// state mutation — actual role gating lives in transfer_manifest_api.py.
const OUTWARD_STATES = new Set(["Draft", "Packed", "Assigned", "Pickup Started", "In Transit"]);
const INWARD_STATES  = new Set(["Delivered", "Partially Received", "Received"]);

function render_lane_banner(frm) {
    if (!frm.doc.status) return;
    if (frm.doc.docstatus === 2) return;
    let pill = null;
    if (OUTWARD_STATES.has(frm.doc.status)) {
        pill = `<span class="indicator-pill orange">
            ↗ OUTWARD — source store: ${frappe.utils.escape_html(frm.doc.source_store || frm.doc.source_warehouse || "?")}
        </span>`;
    } else if (INWARD_STATES.has(frm.doc.status)) {
        pill = `<span class="indicator-pill green">
            ↙ INWARD — destination store: ${frappe.utils.escape_html(frm.doc.destination_store || frm.doc.destination_warehouse || "?")}
        </span>`;
    }
    if (pill) {
        const prior = (frm.dashboard.headline && frm.dashboard.headline.html) ? frm.dashboard.headline.html() : "";
        frm.dashboard.set_headline(prior ? prior + "&nbsp;" + pill : pill);
    }
}

function add_action_buttons(frm) {
    if (frm.doc.docstatus !== 1) return;
    const api = "ch_logistics.api.transfer_manifest_api.";

    // Driver assignment is handled from the Logistics Trip flow, not per
    // manifest — the "Assign Driver" button was intentionally removed here.

    if (frm.doc.status === "Assigned") {
        frm.add_custom_button(__("Start Pickup"), () => show_pickup_dialog(frm, api));
    }

    if (frm.doc.status === "In Transit") {
        frm.add_custom_button(__("Complete Delivery"), () => show_delivery_dialog(frm, api));
    }

    if (frm.doc.status === "Delivered") {
        frm.add_custom_button(__("Accept Delivery"), () => show_accept_dialog(frm, api));
    }

    if (frm.doc.status === "Received") {
        frm.add_custom_button(__("Close Manifest"), () => {
            frappe.confirm(__("Close this manifest?"), () => {
                frappe.call({
                    method: api + "close_manifest",
                    args: { manifest: frm.doc.name },
                    callback: () => frm.reload_doc()
                });
            });
        });
    }

    // ── Recall / Reversal ─────────────────────────────────────────────
    const recallAllowed = ["Packed", "Assigned", "In Transit", "Delivered"];
    if (recallAllowed.includes(frm.doc.status)) {
        frm.add_custom_button(__("Initiate Recall"), () => show_recall_dialog(frm, api), __("Actions"));
    }

    if (frm.doc.status === "Recall Initiated") {
        frm.add_custom_button(__("Confirm Return"), () => show_return_confirm_dialog(frm, api), __("Actions"));
        // Highlight the recall state prominently
        frm.dashboard.set_headline(
            `<span class="indicator-pill red">
                ⚠ Transfer Recalled — ${frm.doc.recall_reason || ""}
                <br/><small>Initiated by ${frm.doc.recall_initiated_by || ""} at ${frm.doc.recall_initiated_at || ""}</small>
            </span>`
        );
    }

    if (frm.doc.status === "Returned") {
        frm.dashboard.set_headline(
            `<span class="indicator-pill gray">
                ↩ Transfer Returned — Stock reversed
                <br/><small>Confirmed by ${frm.doc.return_confirmed_by || ""} at ${frm.doc.return_confirmed_at || ""}</small>
            </span>`
        );
    }

    // Resend OTP for Delivered status
    if (frm.doc.status === "Assigned" || frm.doc.status === "In Transit") {
        frm.add_custom_button(__("Resend OTP"), () => {
            frappe.call({
                method: api + "resend_otp",
                args: { manifest: frm.doc.name },
                callback: (r) => {
                    frappe.msgprint(__("OTP sent to destination store."));
                    frm.reload_doc();
                }
            });
        }, __("Actions"));
    }

    // ── e-Way Bills ───────────────────────────────────────────────────
    // GST Rule 138: one EWB per Stock Entry, generated atomically with the
    // driver+vehicle assignment. The driver carries one printout per
    // consignment — bundled into a single print job here.
    //
    // Lifecycle gates (SAP TM / Oracle TM / Manhattan TMS parity):
    //  • Print  → transport phase only (Assigned…In Transit).  Once the
    //    consignment is Delivered the e-Way Bill has served its legal
    //    purpose; historical reprint stays accessible via the standard
    //    Print menu so we don't pollute the next-step CTA bar.
    //  • Refresh Status → extended to Delivered so accept-delivery staff
    //    can confirm the NIC closure before posting GRN.  Drops at Received.
    //  • Re-sync → generation/Part-B fixes are only meaningful while goods
    //    are still in motion; restricted to transport phase.
    const ewbPrintStates  = ["Assigned", "Pickup Started", "In Transit"];
    const ewbStatusStates = ["Assigned", "Pickup Started", "In Transit", "Delivered"];
    const ewbResyncStates = ["Assigned", "Pickup Started", "In Transit"];
    if (ewbStatusStates.includes(frm.doc.status)) {
        const ewb_api = "ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest.";

        if (ewbPrintStates.includes(frm.doc.status)) {
            frm.add_custom_button(__("Print e-Way Bills"), () => {
                show_ewaybill_print_dialog(frm, ewb_api);
            }, __("e-Way Bill"));
        }

        frm.add_custom_button(__("Refresh Status"), () => {
            frappe.call({
                method: ewb_api + "refresh_ewaybill_summary",
                args: { manifest: frm.doc.name },
                freeze: true,
                freeze_message: __("Refreshing e-Way Bill status..."),
                callback: () => frm.reload_doc(),
            });
        }, __("e-Way Bill"));

        // Resync (re-enqueue generation/Part-B update) — visible only while
        // goods are still in motion AND the NIC sync is not yet Generated.
        if (ewbResyncStates.includes(frm.doc.status)
            && frm.doc.ewaybill_status
            && frm.doc.ewaybill_status !== "Generated"
            && frm.doc.ewaybill_status !== "Not Required") {
            frm.add_custom_button(__("Re-sync e-Way Bills"), () => {
                frappe.confirm(
                    __("Re-enqueue e-Way Bill generation / Part-B update for every Stock Entry on this manifest?"),
                    () => {
                        frappe.call({
                            method: ewb_api + "resync_ewaybills",
                            args: { manifest: frm.doc.name },
                            freeze: true,
                            freeze_message: __("Submitting to NIC..."),
                            callback: () => {
                                frappe.show_alert({
                                    message: __("e-Way Bill jobs queued. Check status in 30s."),
                                    indicator: "blue",
                                });
                                frm.reload_doc();
                            },
                        });
                    }
                );
            }, __("e-Way Bill"));
        }
    }
}

function show_ewaybill_print_dialog(frm, ewb_api) {
    frappe.call({
        method: ewb_api + "refresh_ewaybill_summary",
        args: { manifest: frm.doc.name },
        freeze: true,
        freeze_message: __("Fetching e-Way Bills..."),
        callback: (r) => {
            const rows = r.message || [];
            if (!rows.length) {
                frappe.msgprint(__("No Stock Entries on this manifest."));
                return;
            }
            const html = `
                <table class="table table-bordered" style="margin-top:8px">
                    <thead>
                        <tr>
                            <th>${__("Stock Entry")}</th>
                            <th>${__("e-Way Bill")}</th>
                            <th>${__("Status")}</th>
                            <th>${__("Valid Till")}</th>
                            <th>${__("Vehicle")}</th>
                            <th>${__("Print")}</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.map(r => `
                            <tr>
                                <td><a href="/app/stock-entry/${encodeURIComponent(r.stock_entry)}" target="_blank">${frappe.utils.escape_html(r.stock_entry)}</a></td>
                                <td>${r.ewaybill ? `<code>${frappe.utils.escape_html(r.ewaybill)}</code>` : `<span class="text-muted">—</span>`}</td>
                                <td>${r.status ? `<span class="indicator-pill ${r.ewaybill ? "green" : "orange"}">${frappe.utils.escape_html(r.status)}</span>` : ""}</td>
                                <td>${r.ewaybill_validity || ""}</td>
                                <td>${r.vehicle_no ? `<code>${frappe.utils.escape_html(r.vehicle_no)}</code>` : ""}</td>
                                <td>
                                    ${r.ewaybill
                                        ? `<a class="btn btn-xs btn-default" href="/app/stock-entry/${encodeURIComponent(r.stock_entry)}?print=1" target="_blank">${__("Print")}</a>`
                                        : ""}
                                </td>
                            </tr>
                        `).join("")}
                    </tbody>
                </table>
                <div class="text-muted" style="margin-top:8px">
                    ${__("Driver must carry one printout per Stock Entry. Click 'Print All' to open all in new tabs (allow pop-ups).")}
                </div>
            `;
            const d = new frappe.ui.Dialog({
                title: __("e-Way Bills — {0}", [frm.doc.name]),
                size: "large",
                fields: [{ fieldtype: "HTML", fieldname: "ewb_table", options: html }],
                primary_action_label: __("Print All"),
                primary_action: () => {
                    rows.forEach(r => {
                        if (r.ewaybill) {
                            window.open(
                                `/app/stock-entry/${encodeURIComponent(r.stock_entry)}?print=1`,
                                "_blank"
                            );
                        }
                    });
                    d.hide();
                },
            });
            d.show();
        },
    });
}

function show_pickup_dialog(frm, api) {
    let d = new frappe.ui.Dialog({
        title: __("Start Pickup"),
        fields: [
            { fieldname: "scanned_qr", fieldtype: "Data", label: __("Scan / Enter Manifest QR"), reqd: 1,
              description: __("Scan the manifest/order QR. Pickup is blocked until it matches.") },
            { fieldname: "pickup_photo", fieldtype: "Attach Image", label: __("Pickup Photo"), reqd: 1 },
            { fieldname: "notes", fieldtype: "Small Text", label: __("Notes") },
        ],
        primary_action_label: __("Confirm Pickup"),
        primary_action(values) {
            d.hide();
            // Get GPS
            capture_gps((lat, lng) => {
                frappe.call({
                    method: api + "start_pickup",
                    args: {
                        manifest: frm.doc.name,
                        pickup_photo: values.pickup_photo,
                        scanned_qr: values.scanned_qr,
                        lat, lng,
                        notes: values.notes,
                    },
                    callback: () => frm.reload_doc()
                });
            });
        }
    });
    d.show();
}

function show_delivery_dialog(frm, api) {
    let d = new frappe.ui.Dialog({
        title: __("Complete Delivery"),
        fields: [
            { fieldname: "scanned_qr", fieldtype: "Data", label: __("Scan / Enter Manifest QR"), reqd: 1,
              description: __("Scan the manifest/order QR at the receiver. Delivery is blocked until it matches.") },
            { fieldname: "delivery_photo", fieldtype: "Attach Image", label: __("Delivery Photo"), reqd: 1 },
            { fieldname: "receiver_name", fieldtype: "Data", label: __("Receiver Name"), reqd: 1 },
            { fieldname: "otp", fieldtype: "Data", label: __("Delivery OTP"), reqd: 1 },
        ],
        primary_action_label: __("Confirm Delivery"),
        primary_action(values) {
            d.hide();
            capture_gps((lat, lng) => {
                frappe.call({
                    method: api + "complete_delivery",
                    args: {
                        manifest: frm.doc.name,
                        delivery_photo: values.delivery_photo,
                        receiver_name: values.receiver_name,
                        scanned_qr: values.scanned_qr,
                        otp: values.otp,
                        lat, lng,
                    },
                    callback: () => frm.reload_doc()
                });
            });
        }
    });
    d.show();
}

function show_accept_dialog(frm, api) {
    const transfers = frm.doc.transfers || [];
    let d = new frappe.ui.Dialog({
        title: __("Accept Delivery"),
        size: "large",
        fields: [
            { fieldname: "receipt_html", fieldtype: "HTML", label: __("Received Quantities") },
            { fieldname: "damage_reported", fieldtype: "Check", label: __("Damage Reported") },
            { fieldname: "damage_notes", fieldtype: "Small Text", label: __("Damage Notes"), depends_on: "damage_reported" },
            { fieldname: "damage_photo", fieldtype: "Attach Image", label: __("Damage Photo"), depends_on: "damage_reported" },
        ],
        primary_action_label: __("Accept"),
        primary_action(values) {
            const received_lines = [];
            d.$wrapper.find(".ch-recv-qty").each(function () {
                received_lines.push({
                    stock_entry: $(this).data("se"),
                    received_qty: parseFloat($(this).val()) || 0,
                });
            });
            d.hide();
            frappe.call({
                method: api + "accept_delivery",
                args: {
                    manifest: frm.doc.name,
                    damage_reported: values.damage_reported,
                    damage_notes: values.damage_notes,
                    damage_photo: values.damage_photo,
                    received_lines: JSON.stringify(received_lines),
                },
                callback: () => frm.reload_doc()
            });
        }
    });

    // Per-transfer received-qty grid (defaults to the full expected qty).
    let body;
    if (transfers.length) {
        let rows = transfers.map((t) => `
            <tr>
                <td>${frappe.utils.escape_html(t.stock_entry || "—")}</td>
                <td class="text-right">${flt(t.total_qty) || 0}</td>
                <td style="width:120px">
                    <input type="number" min="0" step="any"
                        class="form-control input-sm ch-recv-qty"
                        data-se="${frappe.utils.escape_html(t.stock_entry || "")}"
                        value="${flt(t.total_qty) || 0}">
                </td>
            </tr>`).join("");
        body = `
            <div class="text-muted small" style="margin-bottom:6px">
                ${__("Confirm the quantity physically received per transfer. A shortage auto-raises a delivery claim.")}
            </div>
            <table class="table table-bordered" style="margin-bottom:0">
                <thead><tr>
                    <th>${__("Stock Entry")}</th>
                    <th class="text-right">${__("Expected")}</th>
                    <th>${__("Received")}</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
    } else {
        body = `<div class="text-muted">${__("No transfers on this manifest.")}</div>`;
    }
    d.fields_dict.receipt_html.$wrapper.html(body);

    d.show();
}

function capture_gps(callback) {
    // Driver location is mandatory at pickup / delivery (proof of presence).
    // Do NOT silently fall back to (0, 0) — that sentinel is rejected by the
    // server and would also defeat the audit trail.
    if (!navigator.geolocation) {
        frappe.msgprint({
            title: __("Location Required"),
            indicator: "red",
            message: __("This device does not support geolocation. Pickup / delivery cannot be confirmed without driver location."),
        });
        return;
    }
    navigator.geolocation.getCurrentPosition(
        (pos) => callback(pos.coords.latitude, pos.coords.longitude),
        (err) => {
            frappe.msgprint({
                title: __("Location Required"),
                indicator: "red",
                message: __("Could not capture driver location ({0}). Enable Location on the device and retry.",
                            [(err && err.message) || __("permission denied")]),
            });
        },
        { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 }
    );
}

// ── Recall / Return Dialogs ──────────────────────────────────────────────────

function show_recall_dialog(frm, api) {
    let d = new frappe.ui.Dialog({
        title: __("Initiate Transfer Recall"),
        fields: [
            {
                fieldname: "info_html",
                fieldtype: "HTML",
                options: `<div class="alert alert-warning" style="padding:12px;border-radius:6px;background:#fff3cd;border:1px solid #ffc107;">
                    <strong>⚠ Warning:</strong> This will recall the transfer and notify the driver and store contacts immediately.
                    The driver will be instructed to return all items to <strong>${frm.doc.source_warehouse}</strong>.
                    Stock will be reversed once the driver confirms the return.
                </div>`
            },
            {
                fieldname: "reason",
                fieldtype: "Select",
                label: __("Recall Reason"),
                options: [
                    "Wrong items packed",
                    "Wrong destination",
                    "Customer order cancelled",
                    "Pricing error — items not to be dispatched",
                    "Quality issue — items need re-inspection",
                    "Transfer not authorized",
                    "Emergency stock requirement at source",
                    "Other"
                ].join("\n"),
                reqd: 1
            },
            {
                fieldname: "notes",
                fieldtype: "Small Text",
                label: __("Additional Notes"),
                description: __("Provide any extra context for the driver and store")
            },
        ],
        primary_action_label: __("Recall Transfer"),
        primary_action(values) {
            d.hide();
            frappe.confirm(
                __("Are you sure you want to recall manifest {0}? The driver will be notified immediately.", [frm.doc.name]),
                () => {
                    frappe.call({
                        method: api + "initiate_recall",
                        args: {
                            manifest: frm.doc.name,
                            reason: values.reason,
                            notes: values.notes,
                        },
                        freeze: true,
                        freeze_message: __("Sending recall notifications..."),
                        callback(r) {
                            if (r.message) {
                                frappe.show_alert({
                                    message: r.message.message || __("Recall initiated. Driver and stores notified."),
                                    indicator: "orange"
                                }, 5);
                                frm.reload_doc();
                            }
                        }
                    });
                }
            );
        }
    });
    d.show();
}

function show_return_confirm_dialog(frm, api) {
    let d = new frappe.ui.Dialog({
        title: __("Confirm Return to Source"),
        fields: [
            {
                fieldname: "info_html",
                fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:12px;border-radius:6px;background:#d1ecf1;border:1px solid #bee5eb;">
                    <strong>Return Checklist:</strong>
                    <ul style="margin:8px 0 0 0;padding-left:18px">
                        <li>All items have been physically returned to <strong>${frm.doc.source_warehouse}</strong></li>
                        <li>Each item has been scanned / counted and matches the manifest</li>
                        <li>A photo has been taken of the returned items</li>
                    </ul>
                </div>`
            },
            {
                fieldname: "return_photo",
                fieldtype: "Attach Image",
                label: __("Return Photo (Required)"),
                description: __("Photo of all items returned to source warehouse"),
                reqd: 1
            },
            {
                fieldname: "confirmed_by",
                fieldtype: "Data",
                label: __("Received By (Name at Source)"),
                description: __("Name of person who received the returned items at source warehouse")
            },
        ],
        primary_action_label: __("Confirm Return & Reverse Stock"),
        primary_action(values) {
            d.hide();
            frappe.confirm(
                __("Confirm that all items have been returned? This will reverse the stock entries and cannot be undone."),
                () => {
                    frappe.call({
                        method: api + "confirm_return",
                        args: {
                            manifest: frm.doc.name,
                            return_photo: values.return_photo,
                            confirmed_by: values.confirmed_by,
                        },
                        freeze: true,
                        freeze_message: __("Reversing stock entries..."),
                        callback(r) {
                            if (r.message) {
                                let reversed = (r.message.reversed_stock_entries || []).join(", ");
                                frappe.show_alert({
                                    message: __("Return confirmed. Stock reversed: {0}", [reversed || "N/A"]),
                                    indicator: "green"
                                }, 7);
                                frm.reload_doc();
                            }
                        }
                    });
                }
            );
        }
    });
    d.show();
}
