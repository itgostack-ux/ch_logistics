/**
 * Delivery App — Mobile-first delivery driver interface.
 *
 * Shows assigned manifests, supports pickup/delivery with photo + GPS capture.
 * Accessible at /app/delivery-app
 */

const API = "ch_logistics.api.transfer_manifest_api.";
const TRIP_API = "ch_logistics.api.logistics_api.";
const DRIVER_API = "ch_logistics.api.driver_api.";

frappe.pages["delivery-app"].on_page_load = function (wrapper) {
    frappe.ui.make_app_page({
        parent: wrapper,
        title: __("Delivery App"),
        single_column: true,
    });

    wrapper.delivery_app = new DeliveryApp(wrapper);
};

frappe.pages["delivery-app"].refresh = function (wrapper) {
    if (wrapper.delivery_app) wrapper.delivery_app.refresh();
};

class DeliveryApp {
    constructor(wrapper) {
        this.$wrapper = $(wrapper);
        this.page = wrapper.page;
        this.$body = $('<div class="delivery-app-root"></div>').appendTo(
            this.page.body
        );
        this.active_tab = "trips";
        this.manifests = [];
        this.history = [];
        this.trips = { active: [], history: [] };
        this.active_manifest = null;
        this.active_trip = null;
        this._trip_detail = null;
        this.render();
    }

    refresh() {
        this.load_data();
    }

    render() {
        this.$body.html(`
            <div class="da-statusbar" id="da-statusbar"></div>
            <div class="da-tabs">
                <button class="da-tab active" data-tab="trips">
                    <i class="fa fa-truck"></i> My Trips
                </button>
                <button class="da-tab" data-tab="assignments">
                    <i class="fa fa-list"></i> Manifests
                </button>
                <button class="da-tab" data-tab="history">
                    <i class="fa fa-history"></i> History
                </button>
            </div>
            <div class="da-content" id="da-content">
                <div class="da-loading">Loading...</div>
            </div>
        `);
        this.bind_events();
        this.load_status();
        this.load_data();
    }

    load_status() {
        frappe.call({
            method: DRIVER_API + "get_status",
            callback: (r) => this.render_status(r.message || {}),
        });
    }

    render_status(p) {
        let status = p.availability_status || "Offline";
        let cls = "da-st-" + status.replace(/ /g, "-").toLowerCase();
        let on_break = status === "Break";
        let toggle = on_break
            ? `<button id="da-resume-btn" class="btn btn-success btn-xs"><i class="fa fa-play"></i> ${__("Resume")}</button>`
            : `<button id="da-break-btn" class="btn btn-default btn-xs"><i class="fa fa-coffee"></i> ${__("Break")}</button>`;
        this.$body.find("#da-statusbar").html(`
            <span class="da-status-pill ${cls}">${__(status)}</span>
            ${toggle}
        `);
    }

    bind_events() {
        this.$body.on("click", ".da-tab", (e) => {
            let tab = $(e.currentTarget).data("tab");
            this.active_tab = tab;
            this.active_trip = null;
            this.active_manifest = null;
            this.$body.find(".da-tab").removeClass("active");
            $(e.currentTarget).addClass("active");
            this.render_content();
        });

        this.$body.on("click", ".da-manifest-card", (e) => {
            let name = $(e.currentTarget).data("name");
            this.show_manifest_detail(name);
        });

        this.$body.on("click", ".da-trip-card", (e) => {
            let name = $(e.currentTarget).data("name");
            this.show_trip_detail(name);
        });

        this.$body.on("click", "#da-back-btn", () => {
            this.active_manifest = null;
            this.active_trip = null;
            this.render_content();
        });

        this.$body.on("click", "#da-pickup-btn", () => this.do_pickup());
        this.$body.on("click", "#da-reject-btn", () => this.do_reject());
        this.$body.on("click", "#da-bulk-reject-btn", () => this.do_bulk_reject_others());
        this.$body.on("click", "#da-arrived-btn", () => this.do_mark_reached());
        this.$body.on("click", "#da-deliver-btn", () => this.do_delivery());

        this.$body.on("click", "#da-break-btn", () => this.do_break("set_break"));
        this.$body.on("click", "#da-resume-btn", () => this.do_break("end_break"));

        this.$body.on("click", "#da-trip-start-btn", () => this.do_trip_start());
        this.$body.on("click", "#da-trip-complete-btn", () => this.do_trip_complete());
        this.$body.on("click", "#da-trip-exception-btn", () => this.do_trip_exception());

        this.$body.on("click", ".da-stop-arrive-btn", (e) => {
            let seq = $(e.currentTarget).data("seq");
            this.do_stop_arrive(seq);
        });
        this.$body.on("click", ".da-stop-complete-btn", (e) => {
            let seq = $(e.currentTarget).data("seq");
            this.do_stop_complete(seq);
        });
        this.$body.on("click", ".da-stop-manifest-link", (e) => {
            let name = $(e.currentTarget).data("name");
            this.show_manifest_detail(name);
        });
    }

    load_data() {
        frappe.call({
            method: TRIP_API + "get_driver_trips",
            args: { include_closed_days: 7 },
            callback: (r) => {
                this.trips = r.message || { active: [], history: [] };
                if (this.active_tab === "trips" && !this.active_trip && !this.active_manifest)
                    this.render_content();
            },
        });
        frappe.call({
            method: API + "get_driver_assignments",
            callback: (r) => {
                this.manifests = r.message || [];
                if (this.active_tab === "assignments" && !this.active_manifest)
                    this.render_content();
            },
        });
        frappe.call({
            method: API + "get_delivery_history",
            callback: (r) => {
                this.history = r.message || [];
                if (this.active_tab === "history") this.render_content();
            },
        });
    }

    render_content() {
        let $c = this.$body.find("#da-content");
        if (this.active_trip) {
            this.render_trip_detail($c);
            return;
        }
        if (this.active_manifest) {
            this.render_detail($c);
            return;
        }
        if (this.active_tab === "trips") {
            this.render_trips_list($c);
            return;
        }
        let list = this.active_tab === "assignments" ? this.manifests : this.history;
        if (!list.length) {
            $c.html(`<div class="da-empty">
                <i class="fa fa-inbox fa-3x"></i>
                <p>${this.active_tab === "assignments" ? __("No manifests assigned") : __("No delivery history")}</p>
            </div>`);
            return;
        }
        let html = list.map((m) => this.manifest_card(m)).join("");
        $c.html(`<div class="da-manifest-list">${html}</div>`);
    }

    render_trips_list($c) {
        let active = (this.trips && this.trips.active) || [];
        let history = (this.trips && this.trips.history) || [];
        if (!active.length && !history.length) {
            $c.html(`<div class="da-empty">
                <i class="fa fa-truck fa-3x"></i>
                <p>${__("No trips assigned")}</p>
            </div>`);
            return;
        }
        let parts = [];
        if (active.length) {
            parts.push(`<div class="da-section-label">${__("Today / Active")}</div>`);
            parts.push(`<div class="da-trip-list">${active.map((t) => this.trip_card(t)).join("")}</div>`);
        }
        if (history.length) {
            parts.push(`<div class="da-section-label">${__("Recent")}</div>`);
            parts.push(`<div class="da-trip-list">${history.map((t) => this.trip_card(t)).join("")}</div>`);
        }
        $c.html(parts.join(""));
    }

    trip_card(t) {
        let status_cls = (t.status || "").toLowerCase().replace(/\s+/g, "-");
        let dir = t.direction === "Reverse" ? '<i class="fa fa-undo"></i> Reverse'
            : (t.direction === "Mixed" ? '<i class="fa fa-random"></i> Mixed' : '<i class="fa fa-arrow-right"></i> Forward');
        let planned = t.planned_start ? frappe.datetime.str_to_user(t.planned_start) : "";
        return `
            <div class="da-trip-card" data-name="${frappe.utils.escape_html(t.name)}">
                <div class="da-card-header">
                    <span class="da-card-name">${frappe.utils.escape_html(t.name)}</span>
                    <span class="da-card-status da-status-${status_cls}">${frappe.utils.escape_html(t.status)}</span>
                </div>
                <div class="da-card-footer">
                    <span>${dir}</span>
                    <span><i class="fa fa-cube"></i> ${t.total_shipments || 0} ${__("shipments")}</span>
                    ${planned ? `<span><i class="fa fa-clock-o"></i> ${planned}</span>` : ""}
                    ${t.vehicle_number ? `<span><i class="fa fa-truck"></i> ${frappe.utils.escape_html(t.vehicle_number)}</span>` : ""}
                </div>
            </div>`;
    }

    manifest_card(m) {
        let status_cls = (m.status || "").toLowerCase().replace(/\s+/g, "-");
        let eta = m.estimated_delivery_date
            ? frappe.datetime.str_to_user(m.estimated_delivery_date)
            : "";
        return `
            <div class="da-manifest-card" data-name="${frappe.utils.escape_html(m.name)}">
                <div class="da-card-header">
                    <span class="da-card-name">${frappe.utils.escape_html(m.name)}</span>
                    <span class="da-card-status da-status-${status_cls}">${frappe.utils.escape_html(m.status)}</span>
                </div>
                <div class="da-card-route">
                    <div class="da-route-point">
                        <i class="fa fa-circle-o da-route-from"></i>
                        <span>${frappe.utils.escape_html(m.source_store || m.source_warehouse || "—")}</span>
                    </div>
                    <div class="da-route-line"></div>
                    <div class="da-route-point">
                        <i class="fa fa-map-marker da-route-to"></i>
                        <span>${frappe.utils.escape_html(m.destination_store || m.destination_warehouse || "—")}</span>
                    </div>
                </div>
                <div class="da-card-footer">
                    <span><i class="fa fa-cube"></i> ${m.total_items || 0} items &middot; ${m.total_qty || 0} qty</span>
                    ${eta ? `<span><i class="fa fa-clock-o"></i> ETA: ${eta}</span>` : ""}
                </div>
            </div>`;
    }

    show_manifest_detail(name) {
        this.active_manifest = name;
        let $c = this.$body.find("#da-content");
        $c.html('<div class="da-loading">Loading manifest...</div>');

        frappe.call({
            method: API + "get_manifest_detail",
            args: { manifest: name },
            callback: (r) => {
                if (!r.message) {
                    $c.html('<div class="da-empty">Manifest not found</div>');
                    return;
                }
                this._detail = r.message;
                this.render_detail($c);
            },
        });
    }

    render_detail($c) {
        let d = this._detail;
        if (!d) return;
        let status_cls = (d.status || "").toLowerCase().replace(/\s+/g, "-");

        // Render item list
        let items_html = "";
        for (let t of d.transfer_items_detail || []) {
            items_html += `<div class="da-se-group">
                <div class="da-se-name">${frappe.utils.escape_html(t.stock_entry)}</div>
                <div class="da-se-route">${frappe.utils.escape_html(t.from_warehouse || "")} → ${frappe.utils.escape_html(t.to_warehouse || "")}</div>`;
            for (let item of t.items || []) {
                items_html += `<div class="da-item-row">
                    <span class="da-item-code">${frappe.utils.escape_html(item.item_code)}</span>
                    <span class="da-item-name">${frappe.utils.escape_html(item.item_name || "")}</span>
                    <span class="da-item-qty">${item.qty}</span>
                </div>`;
            }
            items_html += `</div>`;
        }

        // Action buttons — carrier-grade three-stage contract:
        //   Assigned   → Start Pickup | Reject Pickup | Reject Other Assigns
        //   In Transit → Complete Delivery | Reject Delivery (mid-trip)
        //   Delivered  → (read-only, waiting on receiver to accept)
        let action_html = "";
        if (d.status === "Assigned") {
            // Count sibling Assigned manifests this driver still owns — only
            // worth showing the bulk-reject button when there are siblings.
            let sibling_count = (this.manifests || []).filter(
                m => m.status === "Assigned" && m.name !== d.name
                     && (!d.trip || m.trip === d.trip)
            ).length;
            action_html = `<button id="da-pickup-btn" class="btn btn-primary btn-lg btn-block da-action-btn">
                <i class="fa fa-camera"></i> ${__("Start Pickup")}
            </button>
            <button id="da-reject-btn" class="btn btn-danger btn-sm btn-block da-action-btn">
                <i class="fa fa-ban"></i> ${__("Reject Pickup")}
            </button>`;
            if (sibling_count > 0) {
                action_html += `<button id="da-bulk-reject-btn" class="btn btn-warning btn-sm btn-block da-action-btn">
                    <i class="fa fa-list-ul"></i> ${__("Accept this & reject {0} other", [sibling_count])}
                </button>`;
            }
        } else if (d.status === "In Transit") {
            // Two-stage POD: the driver must record arrival at the destination
            // before the Complete Delivery dialog unlocks. Until arrival is
            // recorded we surface a prominent "Reached Location" CTA that
            // captures GPS + timestamp on the manifest.
            if (!d.arrival_datetime) {
                action_html = `<button id="da-arrived-btn" class="btn btn-primary btn-lg btn-block da-action-btn">
                    <i class="fa fa-map-marker"></i> ${__("Reached Location")}
                </button>
                <button id="da-deliver-btn" class="btn btn-success btn-lg btn-block da-action-btn da-btn-disabled" disabled
                        style="opacity:0.45;cursor:not-allowed;"
                        title="${__("Tap Reached Location first")}">
                    <i class="fa fa-lock"></i> ${__("Complete Delivery")}
                </button>
                <button id="da-reject-btn" class="btn btn-danger btn-sm btn-block da-action-btn">
                    <i class="fa fa-exclamation-triangle"></i> ${__("Failed Delivery (mid-trip)")}
                </button>`;
            } else {
                let arrived_at = frappe.datetime.str_to_user(d.arrival_datetime);
                action_html = `<div class="alert alert-info da-arrival-banner" style="padding:8px 12px;border-radius:6px;margin-bottom:8px;font-size:13px;">
                    <i class="fa fa-map-marker"></i> ${__("Arrived at destination")}: ${frappe.utils.escape_html(arrived_at)}
                </div>
                <button id="da-deliver-btn" class="btn btn-success btn-lg btn-block da-action-btn">
                    <i class="fa fa-check-circle"></i> ${__("Complete Delivery")}
                </button>
                <button id="da-reject-btn" class="btn btn-danger btn-sm btn-block da-action-btn">
                    <i class="fa fa-exclamation-triangle"></i> ${__("Failed Delivery (mid-trip)")}
                </button>`;
            }
        }

        $c.html(`
            <div class="da-detail">
                <div class="da-detail-header">
                    <button id="da-back-btn" class="btn btn-default btn-sm">
                        <i class="fa fa-arrow-left"></i> ${__("Back")}
                    </button>
                    <span class="da-detail-name">${frappe.utils.escape_html(d.name)}</span>
                    <span class="da-card-status da-status-${status_cls}">${frappe.utils.escape_html(d.status)}</span>
                </div>

                <div class="da-detail-route">
                    <div class="da-route-box da-from">
                        <div class="da-route-label">${__("From")}</div>
                        <div class="da-route-wh">${frappe.utils.escape_html(d.source_store || d.source_warehouse || "—")}</div>
                        ${d.source_address ? `<div class="da-route-addr">${frappe.utils.escape_html(d.source_address)}</div>` : ""}
                    </div>
                    <div class="da-route-arrow"><i class="fa fa-long-arrow-right fa-2x"></i></div>
                    <div class="da-route-box da-to">
                        <div class="da-route-label">${__("To")}</div>
                        <div class="da-route-wh">${frappe.utils.escape_html(d.destination_store || d.destination_warehouse || "—")}</div>
                        ${d.destination_address ? `<div class="da-route-addr">${frappe.utils.escape_html(d.destination_address)}</div>` : ""}
                    </div>
                </div>

                <div class="da-detail-summary">
                    <div class="da-stat"><span class="da-stat-val">${d.total_stock_entries || 0}</span><span class="da-stat-label">Transfers</span></div>
                    <div class="da-stat"><span class="da-stat-val">${d.total_items || 0}</span><span class="da-stat-label">Items</span></div>
                    <div class="da-stat"><span class="da-stat-val">${d.total_qty || 0}</span><span class="da-stat-label">Qty</span></div>
                </div>

                <div class="da-detail-items">
                    <h5>${__("Transfer Items")}</h5>
                    ${items_html}
                </div>

                ${d.pickup_photo ? `
                <div class="da-proof-section">
                    <h5><i class="fa fa-camera"></i> ${__("Pickup Proof")}</h5>
                    <img src="${d.pickup_photo}" class="da-proof-img" />
                    <div class="da-proof-time">${frappe.datetime.str_to_user(d.pickup_datetime)}</div>
                </div>` : ""}

                ${d.delivery_photo ? `
                <div class="da-proof-section">
                    <h5><i class="fa fa-camera"></i> ${__("Delivery Proof")}</h5>
                    <img src="${d.delivery_photo}" class="da-proof-img" />
                    <div class="da-proof-time">${frappe.datetime.str_to_user(d.delivery_datetime)}</div>
                    <div class="da-proof-receiver">Receiver: ${frappe.utils.escape_html(d.receiver_name || "")}</div>
                </div>` : ""}

                <div class="da-action-area">
                    ${action_html}
                </div>
            </div>
        `);
    }

    // ── Actions ──────────────────────────────────────────────────

    do_pickup() {
        let d = new frappe.ui.Dialog({
            title: __("Start Pickup"),
            fields: [
                {
                    fieldname: "scanned_qr",
                    fieldtype: "Data",
                    label: __("Scan / Enter Manifest QR"),
                    reqd: 1,
                    description: __("Scan the manifest/order QR. Pickup is blocked until it matches."),
                },
                {
                    fieldname: "pickup_photo",
                    fieldtype: "Attach Image",
                    label: __("Take Photo of Goods"),
                    reqd: 1,
                },
                {
                    fieldname: "notes",
                    fieldtype: "Small Text",
                    label: __("Notes"),
                },
            ],
            primary_action_label: __("Confirm Pickup"),
            primary_action: (values) => {
                d.hide();
                this._capture_gps((lat, lng) => {
                    frappe.call({
                        method: API + "start_pickup",
                        args: {
                            manifest: this.active_manifest,
                            pickup_photo: values.pickup_photo,
                            scanned_qr: values.scanned_qr,
                            lat, lng,
                            notes: values.notes,
                        },
                        callback: () => {
                            frappe.show_alert({
                                message: __("Pickup confirmed!"),
                                indicator: "green",
                            });
                            this.show_manifest_detail(this.active_manifest);
                            this.load_data();
                        },
                    });
                });
            },
        });
        d.show();
    }

    do_mark_reached() {
        // "Reached Location" — two-stage POD's first phase at the receiver
        // end. Captures device GPS, surfaces the readings to the driver for
        // visual confirmation, then POSTs to ``mark_reached_destination``.
        // We show the captured lat/lng (read-only) so the driver can verify
        // they're at the right doorstep before committing the arrival ping.
        let manifest = (this.manifests || []).find(m => m.name === this.active_manifest) || {};
        if (manifest.status !== "In Transit") {
            frappe.show_alert({
                message: __("Reached Location is only available while In Transit."),
                indicator: "orange",
            });
            return;
        }
        frappe.dom.freeze(__("Capturing GPS…"));
        this._capture_gps((lat, lng) => {
            frappe.dom.unfreeze();
            this._show_arrival_dialog(lat, lng);
        });
    }

    _show_arrival_dialog(lat, lng) {
        let destination = (this.manifests || []).find(
            m => m.name === this.active_manifest) || {};
        let dest_label = destination.destination_store
            || destination.destination_warehouse || "—";
        let lat_str = (typeof lat === "number") ? lat.toFixed(6) : String(lat);
        let lng_str = (typeof lng === "number") ? lng.toFixed(6) : String(lng);
        let maps_url = `https://maps.google.com/?q=${lat_str},${lng_str}`;
        let d = new frappe.ui.Dialog({
            title: __("Confirm Arrival at Destination"),
            fields: [
                {
                    fieldname: "info", fieldtype: "HTML",
                    options: `<div class="alert alert-success" style="padding:10px;border-radius:6px;margin-bottom:10px;">
                        <strong><i class="fa fa-map-marker"></i> ${__("Destination")}:</strong>
                        ${frappe.utils.escape_html(dest_label)}
                    </div>`,
                },
                {
                    fieldname: "arrival_lat", fieldtype: "Data",
                    label: __("Latitude"), default: lat_str, read_only: 1,
                },
                {
                    fieldname: "arrival_lng", fieldtype: "Data",
                    label: __("Longitude"), default: lng_str, read_only: 1,
                },
                {
                    fieldname: "preview", fieldtype: "HTML",
                    options: `<div style="text-align:center;margin:8px 0 4px 0;">
                        <a href="${maps_url}" target="_blank" rel="noopener"
                           class="btn btn-default btn-xs">
                           <i class="fa fa-external-link"></i> ${__("Open in Google Maps")}
                        </a>
                    </div>`,
                },
                {
                    fieldname: "note", fieldtype: "HTML",
                    options: `<small class="text-muted">${__(
                        "Tap 'Confirm Arrival' to record this location on the manifest. " +
                        "Complete Delivery will then unlock.")}</small>`,
                },
            ],
            primary_action_label: __("Confirm Arrival"),
            primary_action: () => {
                d.hide();
                frappe.dom.freeze(__("Recording arrival…"));
                frappe.call({
                    method: API + "mark_reached_destination",
                    args: {
                        manifest: this.active_manifest,
                        lat: lat, lng: lng,
                    },
                    callback: () => {
                        frappe.dom.unfreeze();
                        frappe.show_alert({
                            message: __("Arrival recorded. You can now Complete Delivery."),
                            indicator: "green",
                        });
                        // The detail view renders from ``this._detail`` (fetched
                        // via get_manifest_detail), not from ``this.manifests``.
                        // We must re-fetch the detail so the new
                        // ``arrival_datetime`` flips the Complete Delivery
                        // button out of its locked state. ``load_data()`` also
                        // refreshes the list bucket for the back-list view.
                        this.show_manifest_detail(this.active_manifest);
                        this.load_data();
                    },
                    error: () => frappe.dom.unfreeze(),
                });
            },
            secondary_action_label: __("Re-capture GPS"),
            secondary_action: () => {
                d.hide();
                // Re-open the GPS capture loop — useful when the driver moved
                // a few steps and wants a fresher fix.
                this.do_mark_reached();
            },
        });
        d.show();
    }

    do_delivery() {
        // Two-stage POD gate: never open the Complete Delivery dialog until
        // arrival has been recorded — even if the user somehow clicked an
        // un-disabled button (e.g. stale UI). Server enforces the same gate.
        let manifest = (this.manifests || []).find(m => m.name === this.active_manifest) || {};
        if (manifest.status === "In Transit" && !manifest.arrival_datetime) {
            frappe.show_alert({
                message: __("Tap Reached Location first to record arrival at the destination."),
                indicator: "orange",
            });
            return;
        }
        // Step 1: request a fresh OTP — server generates a new 6-digit code
        // and emails / SMSes it to the connected destination warehouse plus
        // the store manager contacts. Only after the OTP has been dispatched
        // do we open the dialog that asks the driver to enter it. This
        // mirrors how Delhivery / BlueDart / Ekart / FedEx driver apps
        // handle the "I'm at the destination" handshake.
        frappe.dom.freeze(__("Sending OTP to warehouse…"));
        frappe.call({
            method: API + "request_delivery_otp",
            args: { manifest: this.active_manifest },
            callback: (r) => {
                frappe.dom.unfreeze();
                let info = r.message || {};
                let recipients_html = "";
                if ((info.masked_emails || []).length || (info.masked_mobiles || []).length) {
                    let parts = [];
                    if ((info.masked_emails || []).length) {
                        parts.push(__("Email: {0}",
                            [info.masked_emails.map(frappe.utils.escape_html).join(", ")]));
                    }
                    if ((info.masked_mobiles || []).length) {
                        parts.push(__("SMS: {0}",
                            [info.masked_mobiles.map(frappe.utils.escape_html).join(", ")]));
                    }
                    recipients_html = `<div class="alert alert-success" style="padding:10px;border-radius:6px;margin-bottom:10px;">
                        <strong>${__("OTP sent")}.</strong> ${parts.join(" • ")}
                    </div>`;
                } else {
                    recipients_html = `<div class="alert alert-warning" style="padding:10px;border-radius:6px;margin-bottom:10px;">
                        ${__("OTP regenerated, but no warehouse contact is configured. Ask the store directly.")}
                    </div>`;
                }
                this._open_delivery_dialog(recipients_html);
            },
            error: () => {
                frappe.dom.unfreeze();
                // Even if OTP send failed (e.g. no SMTP), still let the driver
                // try to complete — server will gate on enforce_delivery_otp.
                this._open_delivery_dialog(
                    `<div class="alert alert-danger" style="padding:10px;border-radius:6px;margin-bottom:10px;">
                        ${__("OTP send failed. Ask the store for the OTP shown on their screen.")}
                    </div>`
                );
            },
        });
    }

    _open_delivery_dialog(recipients_html) {
        let d = new frappe.ui.Dialog({
            title: __("Complete Delivery"),
            fields: [
                { fieldname: "recipients_info", fieldtype: "HTML", options: recipients_html || "" },
                {
                    fieldname: "scanned_qr",
                    fieldtype: "Data",
                    label: __("Scan / Enter Manifest QR"),
                    reqd: 1,
                    description: __("Scan the manifest/order QR at the receiver. Delivery is blocked until it matches."),
                },
                {
                    fieldname: "delivery_photo",
                    fieldtype: "Attach Image",
                    label: __("Take Photo of Delivery"),
                    reqd: 1,
                },
                {
                    fieldname: "receiver_name",
                    fieldtype: "Data",
                    label: __("Receiver Name"),
                    reqd: 1,
                },
                {
                    fieldname: "otp",
                    fieldtype: "Data",
                    label: __("Delivery OTP (from store)"),
                    reqd: 1,
                    description: __("Ask the store staff for the 6-digit OTP just sent to their email."),
                },
            ],
            primary_action_label: __("Confirm Delivery"),
            primary_action: (values) => {
                d.hide();
                this._capture_gps((lat, lng) => {
                    frappe.call({
                        method: API + "complete_delivery",
                        args: {
                            manifest: this.active_manifest,
                            delivery_photo: values.delivery_photo,
                            receiver_name: values.receiver_name,
                            scanned_qr: values.scanned_qr,
                            otp: values.otp,
                            lat, lng,
                        },
                        callback: () => {
                            frappe.show_alert({
                                message: __("Delivery completed!"),
                                indicator: "green",
                            });
                            this.show_manifest_detail(this.active_manifest);
                            this.load_data();
                        },
                    });
                });
            },
            secondary_action_label: __("Resend OTP"),
            secondary_action: () => {
                frappe.call({
                    method: API + "request_delivery_otp",
                    args: { manifest: this.active_manifest },
                    callback: (r) => {
                        let info = r.message || {};
                        frappe.show_alert({
                            message: __("OTP resent. Emails: {0}, SMS: {1}.",
                                [info.email_count || 0, info.sms_count || 0]),
                            indicator: "blue",
                        });
                    },
                });
            },
        });
        d.show();
    }

    do_reject() {
        // Status-aware rejection: carrier-grade ERPs (Delhivery / BlueDart /
        // Ekart / FedEx / Oracle TMS) use different reason codes for pickup
        // failure versus mid-trip delivery failure. We mirror that split
        // so dispatch can route the recovery action correctly.
        let manifest = (this.manifests || []).find(m => m.name === this.active_manifest)
                       || { status: "Assigned" };
        let in_transit = manifest.status === "In Transit";
        let reasons = in_transit ? [
            "Customer Not Available",
            "Address Not Found",
            "Receiver Refused",
            "Damaged in Transit",
            "Vehicle Breakdown",
            "Other",
        ] : [
            "Material Not Ready",
            "Wrong Package",
            "Store Closed",
            "Damaged Package",
            "Other",
        ];
        let title = in_transit ? __("Failed Delivery (mid-trip)") : __("Reject Pickup");
        let d = new frappe.ui.Dialog({
            title: title,
            fields: [
                {
                    fieldname: "rejection_reason",
                    fieldtype: "Select",
                    label: __("Reason"),
                    options: reasons.join("\n"),
                    reqd: 1,
                },
                {
                    fieldname: "rejection_photo",
                    fieldtype: "Attach Image",
                    label: __("Proof Photo"),
                    reqd: 1,
                },
                {
                    fieldname: "rejection_notes",
                    fieldtype: "Small Text",
                    label: __("Notes"),
                },
            ],
            primary_action_label: __("Submit Rejection"),
            primary_action: (values) => {
                d.hide();
                frappe.call({
                    method: API + "reject_manifest",
                    args: {
                        manifest: this.active_manifest,
                        rejection_reason: values.rejection_reason,
                        rejection_photo: values.rejection_photo,
                        rejection_notes: values.rejection_notes,
                    },
                    callback: () => {
                        frappe.show_alert({
                            message: in_transit
                                ? __("Failed delivery logged. Dispatch notified; goods will be returned to source.")
                                : __("Manifest rejected. Dispatcher notified."),
                            indicator: "orange",
                        });
                        this.show_manifest_detail(this.active_manifest);
                        this.load_data();
                    },
                });
            },
        });
        d.show();
    }

    do_bulk_reject_others() {
        // \"Accept this one, reject the rest\" — the handover-pool pattern
        // used by Swiggy / Zomato / Dunzo / Ekart driver apps. Scope is
        // determined server-side (same trip if the accepted manifest is on
        // a trip, else all Assigned manifests for this driver).
        let accepted = this.active_manifest;
        let manifest = (this.manifests || []).find(m => m.name === accepted) || {};
        let siblings = (this.manifests || []).filter(
            m => m.status === "Assigned" && m.name !== accepted
                 && (!manifest.trip || m.trip === manifest.trip)
        );
        if (!siblings.length) {
            frappe.show_alert({ message: __("No other Assigned manifests to reject."), indicator: "blue" });
            return;
        }
        let sibling_html = siblings.map(m =>
            `<li><code>${frappe.utils.escape_html(m.name)}</code> — ${frappe.utils.escape_html(
                (m.destination_store || m.destination_warehouse || "\u2014"))}</li>`
        ).join("");
        let d = new frappe.ui.Dialog({
            title: __("Reject {0} Other Assignment(s)", [siblings.length]),
            fields: [
                {
                    fieldname: "preview",
                    fieldtype: "HTML",
                    options: `<div class="alert alert-warning" style="padding:10px;border-radius:6px;">
                        <strong>${__("You will accept:")}</strong> <code>${frappe.utils.escape_html(accepted)}</code><br>
                        <strong>${__("You will reject:")}</strong>
                        <ul style="margin:6px 0 0 0;padding-left:20px;">${sibling_html}</ul>
                    </div>`,
                },
                {
                    fieldname: "rejection_reason",
                    fieldtype: "Select",
                    label: __("Reason (applies to all rejected)"),
                    options: ["Material Not Ready", "Wrong Package", "Store Closed",
                              "Damaged Package", "Other"].join("\n"),
                    reqd: 1,
                },
                {
                    fieldname: "rejection_photo",
                    fieldtype: "Attach Image",
                    label: __("Proof Photo (shared)"),
                    reqd: 1,
                },
                {
                    fieldname: "rejection_notes",
                    fieldtype: "Small Text",
                    label: __("Notes"),
                },
            ],
            primary_action_label: __("Reject {0} & Continue", [siblings.length]),
            primary_action: (values) => {
                d.hide();
                frappe.call({
                    method: API + "bulk_reject_other_assignments",
                    args: {
                        accepted_manifest: accepted,
                        rejection_reason: values.rejection_reason,
                        rejection_photo: values.rejection_photo,
                        rejection_notes: values.rejection_notes,
                    },
                    callback: (r) => {
                        let res = r.message || {};
                        let msg = __("Accepted {0}. Rejected {1} of {2}.",
                            [accepted, (res.rejected || []).length, siblings.length]);
                        if ((res.skipped || []).length) {
                            msg += " " + __("Skipped: {0}.", [res.skipped.map(s => s.name).join(", ")]);
                        }
                        frappe.show_alert({ message: msg, indicator: "orange" });
                        this.load_data();
                    },
                });
            },
        });
        d.show();
    }

    do_break(method) {
        frappe.call({
            method: DRIVER_API + method,
            callback: (r) => {
                let st = (r.message && r.message.status) || "";
                frappe.show_alert({
                    message: st === "Break" ? __("On break") : __("Back to work"),
                    indicator: st === "Break" ? "orange" : "green",
                });
                this.load_status();
            },
        });
    }

    _capture_gps(callback) {
        // Driver location is mandatory at pickup / delivery (proof of presence).
        // Do NOT silently fall back to (0, 0) — that sentinel is rejected by the
        // server and would also defeat the audit trail. If geolocation is
        // unavailable or denied, surface a clear error and skip the API call.
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

    // ── Trip detail view ─────────────────────────────────────────

    show_trip_detail(name) {
        this.active_trip = name;
        let $c = this.$body.find("#da-content");
        $c.html('<div class="da-loading">Loading trip...</div>');
        frappe.call({
            method: TRIP_API + "get_trip_detail",
            args: { trip: name },
            callback: (r) => {
                if (!r.message) {
                    $c.html('<div class="da-empty">Trip not found</div>');
                    return;
                }
                this._trip_detail = r.message;
                this.render_trip_detail($c);
            },
        });
    }

    render_trip_detail($c) {
        let t = this._trip_detail;
        if (!t) return;
        let status_cls = (t.status || "").toLowerCase().replace(/\s+/g, "-");

        // Group manifests by stop_sequence for the stop cards.
        let manifests_by_stop = {};
        for (let m of t.manifests || []) {
            let key = m.stop_sequence || 0;
            (manifests_by_stop[key] = manifests_by_stop[key] || []).push(m);
        }

        let stops_html = (t.stops || []).map((s) => {
            let st_cls = (s.status || "").toLowerCase().replace(/\s+/g, "-");
            let manifests = manifests_by_stop[s.sequence] || [];
            let manifests_html = manifests.map((m) => `
                <div class="da-stop-manifest da-stop-manifest-link"
                     data-name="${frappe.utils.escape_html(m.name)}">
                    <span><i class="fa fa-file-text-o"></i> ${frappe.utils.escape_html(m.name)}</span>
                    <span class="da-card-status da-status-${(m.status || "").toLowerCase().replace(/\s+/g, "-")}">${frappe.utils.escape_html(m.status)}</span>
                </div>`).join("");

            let can_arrive = (t.status === "Started" && s.status === "Pending");
            let can_complete = (t.status === "Started" && s.status === "Arrived");

            return `
                <div class="da-stop-card">
                    <div class="da-stop-header">
                        <span class="da-stop-seq">#${s.sequence}</span>
                        <span class="da-stop-type">${frappe.utils.escape_html(s.stop_type)}</span>
                        <span class="da-card-status da-status-${st_cls}">${frappe.utils.escape_html(s.status)}</span>
                    </div>
                    <div class="da-stop-where">
                        <i class="fa fa-map-marker"></i>
                        ${frappe.utils.escape_html(s.store || s.warehouse || "—")}
                    </div>
                    ${manifests_html ? `<div class="da-stop-manifests">${manifests_html}</div>` : ""}
                    <div class="da-stop-actions">
                        ${can_arrive ? `<button class="btn btn-primary btn-sm da-stop-arrive-btn" data-seq="${s.sequence}"><i class="fa fa-location-arrow"></i> ${__("Arrive")}</button>` : ""}
                        ${can_complete ? `<button class="btn btn-success btn-sm da-stop-complete-btn" data-seq="${s.sequence}"><i class="fa fa-check"></i> ${__("Complete")}</button>` : ""}
                    </div>
                </div>`;
        }).join("");

        // Trip-level action buttons
        let action_html = "";
        if (t.status === "Assigned") {
            action_html += `<button id="da-trip-start-btn" class="btn btn-primary btn-lg btn-block da-action-btn"><i class="fa fa-play"></i> ${__("Start Trip")}</button>`;
        } else if (t.status === "Started") {
            let all_done = (t.stops || []).every((s) => s.status === "Completed" || s.status === "Skipped");
            action_html += `<button id="da-trip-complete-btn" class="btn btn-success btn-lg btn-block da-action-btn" ${all_done ? "" : "disabled"}><i class="fa fa-flag-checkered"></i> ${__("Complete Trip")}</button>`;
        }
        if (t.status === "Assigned" || t.status === "Started") {
            action_html += `<button id="da-trip-exception-btn" class="btn btn-warning btn-sm btn-block da-action-btn"><i class="fa fa-exclamation-triangle"></i> ${__("Report Exception")}</button>`;
        }

        let exceptions_html = (t.exceptions || []).map((e) => `
            <div class="da-exception-row">
                <span class="da-exc-sev da-sev-${(e.severity || "").toLowerCase()}">${frappe.utils.escape_html(e.severity)}</span>
                <span class="da-exc-type">${frappe.utils.escape_html(e.exception_type)}</span>
                ${e.stop_sequence ? `<span class="da-exc-stop">#${e.stop_sequence}</span>` : ""}
                <span class="da-exc-remarks">${frappe.utils.escape_html(e.remarks || "")}</span>
            </div>`).join("");

        $c.html(`
            <div class="da-detail">
                <div class="da-detail-header">
                    <button id="da-back-btn" class="btn btn-default btn-sm">
                        <i class="fa fa-arrow-left"></i> ${__("Back")}
                    </button>
                    <span class="da-detail-name">${frappe.utils.escape_html(t.name)}</span>
                    <span class="da-card-status da-status-${status_cls}">${frappe.utils.escape_html(t.status)}</span>
                </div>

                <div class="da-detail-summary">
                    <div class="da-stat"><span class="da-stat-val">${(t.stops || []).length}</span><span class="da-stat-label">Stops</span></div>
                    <div class="da-stat"><span class="da-stat-val">${t.total_shipments || 0}</span><span class="da-stat-label">Shipments</span></div>
                    <div class="da-stat"><span class="da-stat-val">${frappe.utils.escape_html(t.direction || "Forward")}</span><span class="da-stat-label">Direction</span></div>
                </div>

                <div class="da-detail-items">
                    <h5>${__("Stops")}</h5>
                    ${stops_html || `<div class="da-empty">${__("No stops")}</div>`}
                </div>

                ${exceptions_html ? `<div class="da-detail-items">
                    <h5><i class="fa fa-exclamation-triangle"></i> ${__("Exceptions")}</h5>
                    ${exceptions_html}
                </div>` : ""}

                <div class="da-action-area">
                    ${action_html}
                </div>
            </div>
        `);
    }

    // ── Trip actions ─────────────────────────────────────────────

    do_trip_start() {
        this._capture_gps((lat, lng) => {
            frappe.call({
                method: TRIP_API + "trip_start",
                args: { trip: this.active_trip, gps_lat: lat, gps_lng: lng },
                callback: () => {
                    frappe.show_alert({ message: __("Trip started"), indicator: "green" });
                    this.show_trip_detail(this.active_trip);
                    this.load_data();
                },
            });
        });
    }

    do_trip_complete() {
        frappe.confirm(__("Complete this trip?"), () => {
            frappe.call({
                method: TRIP_API + "trip_complete",
                args: { trip: this.active_trip },
                callback: () => {
                    frappe.show_alert({ message: __("Trip completed"), indicator: "green" });
                    this.show_trip_detail(this.active_trip);
                    this.load_data();
                },
            });
        });
    }

    do_stop_arrive(seq) {
        this._capture_gps((lat, lng) => {
            frappe.call({
                method: TRIP_API + "stop_arrive",
                args: { trip: this.active_trip, sequence: seq, gps_lat: lat, gps_lng: lng },
                callback: () => {
                    frappe.show_alert({ message: __("Stop arrival recorded"), indicator: "green" });
                    this.show_trip_detail(this.active_trip);
                },
            });
        });
    }

    do_stop_complete(seq) {
        frappe.prompt(
            [{
                fieldname: "scan_compliance_pct",
                fieldtype: "Percent",
                label: __("Scan Compliance %"),
                default: 100,
                reqd: 1,
            }],
            (values) => {
                frappe.call({
                    method: TRIP_API + "stop_complete",
                    args: {
                        trip: this.active_trip,
                        sequence: seq,
                        scan_compliance_pct: values.scan_compliance_pct,
                    },
                    callback: () => {
                        frappe.show_alert({ message: __("Stop completed"), indicator: "green" });
                        this.show_trip_detail(this.active_trip);
                    },
                });
            },
            __("Complete Stop"),
            __("Confirm")
        );
    }

    do_trip_exception() {
        let d = new frappe.ui.Dialog({
            title: __("Report Exception"),
            fields: [
                {
                    fieldname: "exception_type",
                    fieldtype: "Select",
                    label: __("Type"),
                    options: "Customer Not Available\nAddress Issue\nVehicle Breakdown\nDamage\nStockout\nWrong Item\nPayment Issue\nOther",
                    reqd: 1,
                },
                {
                    fieldname: "severity",
                    fieldtype: "Select",
                    label: __("Severity"),
                    options: "Low\nMedium\nHigh\nCritical",
                    default: "Medium",
                    reqd: 1,
                },
                {
                    fieldname: "stop_sequence",
                    fieldtype: "Int",
                    label: __("Stop Seq (optional)"),
                },
                {
                    fieldname: "remarks",
                    fieldtype: "Small Text",
                    label: __("Remarks"),
                    reqd: 1,
                },
                {
                    fieldname: "photo",
                    fieldtype: "Attach Image",
                    label: __("Photo"),
                },
            ],
            primary_action_label: __("Submit"),
            primary_action: (values) => {
                d.hide();
                frappe.call({
                    method: TRIP_API + "exception_raise",
                    args: {
                        trip: this.active_trip,
                        exception_type: values.exception_type,
                        severity: values.severity,
                        stop_sequence: values.stop_sequence,
                        remarks: values.remarks,
                        photo: values.photo,
                    },
                    callback: () => {
                        frappe.show_alert({ message: __("Exception logged"), indicator: "orange" });
                        this.show_trip_detail(this.active_trip);
                    },
                });
            },
        });
        d.show();
    }
}
