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
        // Sign-out button drops the driver to Offline (mirrors carrier driver
        // apps: an explicit end-of-shift action that releases their slot from
        // dispatch). Available while NOT actively in transit so a driver
        // doesn't accidentally sign out mid-delivery.
        let signout = (status === "In Transit" || status === "Assigned")
            ? `<button id="da-signout-btn" class="btn btn-default btn-xs" disabled
                       style="opacity:0.45;cursor:not-allowed;"
                       title="${__("Finish or reject your current manifests before signing out")}">
                  <i class="fa fa-sign-out"></i> ${__("Sign Out")}
               </button>`
            : `<button id="da-signout-btn" class="btn btn-danger btn-xs">
                  <i class="fa fa-sign-out"></i> ${__("Sign Out")}
               </button>`;
        this.$body.find("#da-statusbar").html(`
            <span class="da-status-pill ${cls}">${__(status)}</span>
            ${toggle}
            ${signout}
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
        this.$body.on("click", "#da-signout-btn", () => this.do_signout());

        this.$body.on("click", "#da-trip-start-btn", () => this.do_trip_start());
        this.$body.on("click", "#da-trip-accept-btn", () => this.do_trip_accept());
        this.$body.on("click", "#da-trip-reject-btn", () => this.do_trip_reject());
        this.$body.on("click", "#da-trip-complete-btn", () => this.do_trip_complete());
        this.$body.on("click", "#da-trip-exception-btn", () => this.do_trip_exception());
        this.$body.on("click", "#da-manifest-close-btn", () => this.do_manifest_close(this.active_manifest));
        this.$body.on("click", ".da-stop-manifest-close-btn", (e) => {
            e.stopPropagation();
            let name = $(e.currentTarget).data("name");
            this.do_manifest_close(name);
        });

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
                if (this.active_tab === "trips" && !this.active_trip && !this.active_manifest)
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
        let list = this.history;
        if (!list.length) {
            $c.html(`<div class="da-empty">
                <i class="fa fa-inbox fa-3x"></i>
                <p>${__("No delivery history")}</p>
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
        } else if (d.status === "Delivered" || d.status === "Received" || d.status === "Partially Received") {
            action_html = `<button id="da-manifest-close-btn" class="btn btn-success btn-lg btn-block da-action-btn">
                <i class="fa fa-archive"></i> ${__("Close Manifest")}
            </button>`;
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

    do_signout() {
        // End-of-shift hand-off. Driver explicitly takes themselves out of
        // dispatch rotation — mirrors Delhivery / BlueDart / Uber Freight
        // 'End Shift' actions. The on_logout hook in ch_logistics.hooks
        // flips the duty machine to OFFLINE automatically when the Frappe
        // session ends, so we just need to drop the browser session.
        frappe.confirm(
            __("Sign out for the day? You will be marked Offline and need to log back in to receive new manifests."),
            () => {
                frappe.show_alert({
                    message: __("Signing out…"),
                    indicator: "blue",
                });
                // Frappe's standard web logout — fires on_logout hook which
                // drops Driver.availability_status to Offline server-side.
                window.location.href = "/?cmd=web_logout";
            },
        );
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

        const to_num = (v) => {
            let n = parseFloat(v);
            return Number.isFinite(n) ? n : null;
        };
        const valid_latlng = (lat, lng) => (
            lat !== null && lng !== null
            && Math.abs(lat) <= 90 && Math.abs(lng) <= 180
            && !(lat === 0 && lng === 0)
        );

        const manifest_pickup_by_stop = {};
        const manifest_drop_by_stop = {};
        for (let m of (t.manifests || [])) {
            let key = m.stop_sequence || 0;
            if (!key) continue;
            let p_lat = to_num(m.pickup_latitude);
            let p_lng = to_num(m.pickup_longitude);
            let d_lat = to_num(m.delivery_latitude);
            let d_lng = to_num(m.delivery_longitude);
            if (!manifest_pickup_by_stop[key] && valid_latlng(p_lat, p_lng)) {
                manifest_pickup_by_stop[key] = { lat: p_lat, lng: p_lng };
            }
            if (!manifest_drop_by_stop[key] && valid_latlng(d_lat, d_lng)) {
                manifest_drop_by_stop[key] = { lat: d_lat, lng: d_lng };
            }
        }

        const map_points = [];
        for (let s of (t.stops || [])) {
            let lat = to_num(s.gps_lat);
            let lng = to_num(s.gps_lng);
            if (!valid_latlng(lat, lng)) {
                let fallback = null;
                if ((s.stop_type || "").toLowerCase() === "pickup") {
                    fallback = manifest_pickup_by_stop[s.sequence] || manifest_drop_by_stop[s.sequence];
                } else {
                    fallback = manifest_drop_by_stop[s.sequence] || manifest_pickup_by_stop[s.sequence];
                }
                if (fallback) {
                    lat = fallback.lat;
                    lng = fallback.lng;
                }
            }
            if (!valid_latlng(lat, lng)) {
                // Last-resort fallback: the store / warehouse master geocode.
                let s_lat = to_num(s.store_lat);
                let s_lng = to_num(s.store_lng);
                if (valid_latlng(s_lat, s_lng)) {
                    lat = s_lat;
                    lng = s_lng;
                } else {
                    let w_lat = to_num(s.warehouse_lat);
                    let w_lng = to_num(s.warehouse_lng);
                    if (valid_latlng(w_lat, w_lng)) {
                        lat = w_lat;
                        lng = w_lng;
                    }
                }
            }
            if (valid_latlng(lat, lng)) {
                map_points.push({
                    seq: s.sequence,
                    label: s.store || s.warehouse || "Stop",
                    stop_type: s.stop_type || "",
                    status: s.status || "",
                    lat,
                    lng,
                });
            }
        }

        // Stable id so we can re-render multiple times without colliding
        // with a previously-mounted Leaflet container.
        const map_dom_id = `da-trip-map-${Date.now()}`;
        let map_html = "";
        if (map_points.length) {
            // Build a "Directions" link that pre-fills every stop in the
            // requested order — mirrors the Google Maps "multi-stop route"
            // flow used by Bringg / Onfleet / FarEye driver apps as an
            // OS-native turn-by-turn handoff.
            const directions_href = (() => {
                if (map_points.length === 1) {
                    const p = map_points[0];
                    return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(p.lat + "," + p.lng)}`;
                }
                const origin = map_points[0];
                const dest = map_points[map_points.length - 1];
                const mids = map_points.slice(1, -1).map((p) => `${p.lat},${p.lng}`).join("|");
                let url = `https://www.google.com/maps/dir/?api=1`
                    + `&origin=${encodeURIComponent(origin.lat + "," + origin.lng)}`
                    + `&destination=${encodeURIComponent(dest.lat + "," + dest.lng)}`
                    + `&travelmode=driving`;
                if (mids) url += `&waypoints=${encodeURIComponent(mids)}`;
                return url;
            })();

            const links = map_points.map((p) => {
                const text = `#${p.seq} ${frappe.utils.escape_html(p.label)}`;
                const href = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(p.lat + "," + p.lng)}`;
                return `<a class="da-map-link" target="_blank" rel="noopener" href="${href}">${text}</a>`;
            }).join("");

            map_html = `<div class="da-trip-map-wrap">
                <div class="da-trip-map-head">
                    <h5><i class="fa fa-map"></i> ${__("Trip Map")}</h5>
                    <a class="da-map-nav-btn" target="_blank" rel="noopener" href="${directions_href}">
                        <i class="fa fa-location-arrow"></i> ${__("Navigate")}
                    </a>
                </div>
                <div class="da-trip-map-frame">
                    <div id="${map_dom_id}" class="da-trip-leaflet"></div>
                </div>
                <div class="da-map-links">${links}</div>
            </div>`;
        } else {
            map_html = `<div class="da-trip-map-wrap">
                <h5><i class="fa fa-map"></i> ${__("Trip Map")}</h5>
                <div class="da-map-empty text-muted">${__("Map preview will appear after stop GPS is captured or manifests include coordinates.")}</div>
            </div>`;
        }

        // Group manifests under every stop that serves them. The server
        // (get_trip_detail) resolves pickup_stop_sequence (source side) and
        // drop_stop_sequence (destination side) per manifest — raw
        // stop_sequence only ever pointed at the destination stop, which left
        // pickup stops with no manifest chips (no Manifest ID, no pickup
        // photo, and a bare "Arrive" CTA).
        let manifests_by_stop = {};
        const _push_stop_manifest = (key, m) => {
            if (!key) return;
            (manifests_by_stop[key] = manifests_by_stop[key] || []).push(m);
        };
        for (let m of t.manifests || []) {
            const pickup_seq = m.pickup_stop_sequence;
            const drop_seq = m.drop_stop_sequence || m.stop_sequence;
            _push_stop_manifest(pickup_seq, m);
            if (drop_seq && drop_seq !== pickup_seq) _push_stop_manifest(drop_seq, m);
            if (!pickup_seq && !drop_seq) _push_stop_manifest(m.stop_sequence || 0, m);
        }

        let stops_html = (t.stops || []).map((s) => {
            let st_cls = (s.status || "").toLowerCase().replace(/\s+/g, "-");
            let manifests = manifests_by_stop[s.sequence] || [];
            let active_manifest_rows = manifests.filter((m) => ["Assigned", "Pickup Started", "In Transit"].includes(m.status));
            const _proof_line = (m) => {
                // Keep the captured proof visible on the stop card: once the
                // combined Arrive flow (or the manifest-level Start Pickup)
                // runs, the driver should still see WHAT was captured, where.
                const bits = [];
                if (m.pickup_photo) {
                    const when = m.pickup_datetime ? ` ${frappe.datetime.str_to_user(m.pickup_datetime)}` : "";
                    bits.push(`<a href="${frappe.utils.escape_html(m.pickup_photo)}" target="_blank" rel="noopener">
                        <i class="fa fa-camera"></i> ${__("Pickup photo")}</a>${frappe.utils.escape_html(when)}`);
                }
                if (m.delivery_photo) {
                    const when = m.delivery_datetime ? ` ${frappe.datetime.str_to_user(m.delivery_datetime)}` : "";
                    bits.push(`<a href="${frappe.utils.escape_html(m.delivery_photo)}" target="_blank" rel="noopener">
                        <i class="fa fa-camera"></i> ${__("Delivery photo")}</a>${frappe.utils.escape_html(when)}`);
                }
                if (m.receiver_name) {
                    bits.push(`<i class="fa fa-user"></i> ${frappe.utils.escape_html(m.receiver_name)}`);
                }
                return bits.length
                    ? `<div class="da-stop-manifest-proof text-muted" style="font-size:11px;padding:2px 8px 6px;">${bits.join(" &middot; ")}</div>`
                    : "";
            };
            let manifests_html = manifests.map((m) => `
                <div class="da-stop-manifest da-stop-manifest-link"
                     data-name="${frappe.utils.escape_html(m.name)}">
                    <span><i class="fa fa-file-text-o"></i> ${frappe.utils.escape_html(m.name)}</span>
                    <span class="da-card-status da-status-${(m.status || "").toLowerCase().replace(/\s+/g, "-")}">${frappe.utils.escape_html(m.status)}</span>
                    ${["Delivered", "Received", "Partially Received"].includes(m.status)
                        ? `<button class="btn btn-xs btn-success da-stop-manifest-close-btn" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-archive"></i> ${__("Close")}</button>`
                        : ""}
                </div>${_proof_line(m)}`).join("");

            let can_arrive = (t.status === "Started" && s.status === "Pending");
            let can_complete = (t.status === "Started" && s.status === "Arrived");
            // Per-stop CTA label reflects what the combined flow actually
            // does: pickup stops capture goods + per-manifest QR; drop stops
            // capture delivery photo + receiver + per-manifest OTP/QR. We
            // still call it "Arrive ..." so the action remains discoverable.
            const st_type = (s.stop_type || "").toLowerCase();
            let arrive_label = __("Arrive");
            let arrive_icon = "fa-location-arrow";
            if (st_type === "pickup" && active_manifest_rows.length) {
                arrive_label = __("Arrive & Pick Up");
                arrive_icon = "fa-camera";
            } else if (st_type === "drop" && active_manifest_rows.length) {
                arrive_label = __("Arrive & Deliver");
                arrive_icon = "fa-check-circle";
            }
            let completion_hint = "";
            if (can_arrive && active_manifest_rows.length) {
                const verb = st_type === "pickup"
                    ? __("photo + QR scan for each manifest")
                    : __("delivery photo, receiver name + per-manifest OTP/QR");
                completion_hint = `<div class="text-muted" style="font-size:11px;margin-bottom:6px;">
                    ${__("One tap arrives, captures {0}, and closes the stop.", [verb])}
                </div>`;
            } else if (can_complete && active_manifest_rows.length) {
                // Stop is already Arrived but manifests are still open — that
                // means the driver finished pickup/delivery on individual
                // manifest cards. Direct them to the manifest to wrap up.
                completion_hint = `<div class="text-muted" style="font-size:11px;margin-bottom:6px;">
                    ${__("Open the manifest below to finish OTP/photo, then tap Complete.")}
                </div>`;
            }

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
                    ${completion_hint}
                    ${manifests_html ? `<div class="da-stop-manifests">${manifests_html}</div>` : ""}
                    <div class="da-stop-actions">
                        ${can_arrive ? `<button class="btn btn-primary btn-sm da-stop-arrive-btn" data-seq="${s.sequence}"><i class="fa ${arrive_icon}"></i> ${arrive_label}</button>` : ""}
                        ${can_complete ? `<button class="btn btn-success btn-sm da-stop-complete-btn" data-seq="${s.sequence}"><i class="fa fa-check"></i> ${__("Complete")}</button>` : ""}
                    </div>
                </div>`;
        }).join("");

        // Trip-level action buttons
        let action_html = "";
        if (t.status === "Assigned") {
            action_html += `<button id="da-trip-accept-btn" class="btn btn-primary btn-lg btn-block da-action-btn"><i class="fa fa-check-circle"></i> ${__("Accept &amp; Start Trip")}</button>`;
            action_html += `<button id="da-trip-reject-btn" class="btn btn-danger btn-sm btn-block da-action-btn"><i class="fa fa-ban"></i> ${__("Reject Trip")}</button>`;
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

        // Derive TMS-standard counters from the stop graph rather than a
        // single ambiguous "Stops" total. SAP TM "Stop Sequence" / Oracle
        // OTM "Trip Stops" / Blue Yonder TMS "Tour Stops" all expose pickup
        // and delivery counts side-by-side; last-mile players (Bringg,
        // Onfleet, FarEye, Locus) follow the same pattern in driver UIs.
        const pickup_count = (t.stops || []).filter(
            (s) => (s.stop_type || "").toLowerCase() === "pickup"
        ).length;
        const drop_count = (t.stops || []).filter(
            (s) => (s.stop_type || "").toLowerCase() === "drop"
        ).length;
        const shipment_count = t.total_shipments != null
            ? t.total_shipments
            : (t.manifests || []).length;
        const direction_label = frappe.utils.escape_html(t.direction || "Forward");

        $c.html(`
            <div class="da-detail">
                <div class="da-detail-header">
                    <button id="da-back-btn" class="btn btn-default btn-sm">
                        <i class="fa fa-arrow-left"></i> ${__("Back")}
                    </button>
                    <span class="da-detail-name">${frappe.utils.escape_html(t.name)}</span>
                    <span class="da-card-status da-status-${status_cls}">${frappe.utils.escape_html(t.status)}</span>
                </div>

                <div class="da-trip-direction-row">
                    <span class="da-trip-direction-badge da-dir-${(t.direction || "forward").toLowerCase()}">
                        <i class="fa fa-arrows-h"></i> ${direction_label}
                    </span>
                </div>

                <div class="da-detail-summary">
                    <div class="da-stat"><span class="da-stat-val">${pickup_count}</span><span class="da-stat-label">${__("Pickups")}</span></div>
                    <div class="da-stat"><span class="da-stat-val">${drop_count}</span><span class="da-stat-label">${__("Drops")}</span></div>
                    <div class="da-stat"><span class="da-stat-val">${shipment_count}</span><span class="da-stat-label">${__("Shipments")}</span></div>
                </div>

                ${map_html}

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

        // Mount the Leaflet map once the markup is in the DOM. Lazy-loads
        // Leaflet from CDN on first render; subsequent renders re-use the
        // already-loaded library. Failures degrade gracefully — the static
        // "stop chips" list below the map remains functional and the
        // Navigate button still hands off to the OS map app.
        if (map_points.length) {
            this._render_trip_leaflet_map(map_dom_id, map_points);
        }
    }

    _ensure_leaflet_loaded() {
        if (window.L && window.L.map) return Promise.resolve(window.L);
        if (this._leaflet_loading) return this._leaflet_loading;

        // Pin a known-good Leaflet release served via unpkg (also mirrored
        // on cdnjs). Integrity hashes from leafletjs.com release notes.
        const css_url = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
        const js_url = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";

        if (!document.querySelector('link[data-da-leaflet-css="1"]')) {
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = css_url;
            link.setAttribute("data-da-leaflet-css", "1");
            link.crossOrigin = "";
            document.head.appendChild(link);
        }

        this._leaflet_loading = new Promise((resolve, reject) => {
            if (window.L && window.L.map) return resolve(window.L);
            let script = document.querySelector('script[data-da-leaflet-js="1"]');
            if (script) {
                script.addEventListener("load", () => resolve(window.L));
                script.addEventListener("error", reject);
                return;
            }
            script = document.createElement("script");
            script.src = js_url;
            script.async = true;
            script.crossOrigin = "";
            script.setAttribute("data-da-leaflet-js", "1");
            script.addEventListener("load", () => resolve(window.L));
            script.addEventListener("error", reject);
            document.head.appendChild(script);
        });
        return this._leaflet_loading;
    }

    _render_trip_leaflet_map(dom_id, points) {
        const container = document.getElementById(dom_id);
        if (!container || !points.length) return;
        this._ensure_leaflet_loaded().then((L) => {
            // The container may have been torn down by a re-render while
            // Leaflet was still loading — bail out cleanly in that case.
            if (!document.body.contains(container)) return;

            const map = L.map(container, {
                zoomControl: true,
                attributionControl: true,
                scrollWheelZoom: false,
                tap: true,
            });

            // OpenStreetMap tiles — same provider used by Locus, FarEye,
            // Onfleet, and most non-Google driver-app fallbacks. Free and
            // requires no API key. We respect the OSM tile usage policy by
            // crediting them inline (Leaflet does this automatically).
            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
            }).addTo(map);

            const latlngs = points.map((p) => [p.lat, p.lng]);

            // Draw the route polyline first so markers sit on top.
            if (latlngs.length >= 2) {
                L.polyline(latlngs, {
                    color: "#3b82f6",
                    weight: 4,
                    opacity: 0.85,
                    dashArray: "6 6",
                }).addTo(map);
            }

            // Numbered, type-coloured markers — green = Pickup, red = Drop,
            // gray = anything else (mirrors SAP TM / Oracle OTM legend).
            points.forEach((p) => {
                const stop_type = (p.stop_type || "").toLowerCase();
                const status = (p.status || "").toLowerCase().replace(/\s+/g, "-");
                const color = stop_type === "pickup"
                    ? "#16a34a"
                    : stop_type === "drop"
                        ? "#dc2626"
                        : "#64748b";
                const icon = L.divIcon({
                    className: "da-stop-marker",
                    iconSize: [28, 36],
                    iconAnchor: [14, 34],
                    popupAnchor: [0, -30],
                    html: `<div class="da-stop-marker-pin da-stop-marker-${stop_type || "other"} da-stop-marker-status-${status}" style="--marker-color:${color}"><span>${p.seq}</span></div>`,
                });
                const marker = L.marker([p.lat, p.lng], { icon }).addTo(map);
                const safe_label = frappe.utils.escape_html(p.label);
                const safe_type = frappe.utils.escape_html(p.stop_type || "Stop");
                const safe_status = frappe.utils.escape_html(p.status || "");
                marker.bindPopup(
                    `<div class="da-stop-popup">
                        <div class="da-stop-popup-title">#${p.seq} · ${safe_type}</div>
                        <div class="da-stop-popup-where">${safe_label}</div>
                        ${safe_status ? `<div class="da-stop-popup-status">${safe_status}</div>` : ""}
                    </div>`
                );
            });

            if (latlngs.length === 1) {
                map.setView(latlngs[0], 14);
            } else {
                map.fitBounds(L.latLngBounds(latlngs), { padding: [24, 24] });
            }

            // Mobile browsers compute layout async — invalidate once the
            // container has its final dimensions, otherwise tiles render
            // into the top-left 256x256 square only.
            setTimeout(() => {
                if (document.body.contains(container)) map.invalidateSize();
            }, 100);
        }).catch(() => {
            // Network blocked / CDN down — leave the inert div in place and
            // surface a minimal hint. The chip list + Navigate button below
            // still work, so the driver is not stuck.
            container.innerHTML = `<div class="da-map-empty text-muted" style="padding:18px;">${__("Could not load the map library. Use the Navigate button to open the route in your map app.")}</div>`;
        });
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

    do_trip_accept() {
        // Carrier-grade trip-start gate (Delhivery / BlueDart / Ekart / Bringg
        // / FarEye dock-out flow): the driver must verify that every shipment
        // for this trip is physically in the vehicle BEFORE the trip flips to
        // Started. We force one load photo + one QR scan per source-stop
        // manifest, capture GPS, then run accept → start_pickup → stop_arrive
        // → stop_complete in a single atomic chain.
        //
        // For a standard forward trip (1 Pickup stop, N Drop stops) "all
        // manifests at the first Pickup" == "all manifests on the trip".
        // For multi-pickup milk-runs the gate covers just the first pickup
        // stop; later pickup stops keep using the per-stop "Arrive & Pick Up"
        // dialog.
        const t = this._trip_detail || {};
        const first_pickup = (t.stops || []).find(
            (s) => (s.stop_type || "").toLowerCase() === "pickup"
        );
        const source_manifests = first_pickup
            ? this._gather_stop_manifests(first_pickup.sequence, ["Assigned"])
            : [];

        if (!first_pickup || !source_manifests.length) {
            // Nothing to scan at the source (delivery-only trip, or all
            // manifests at the source already picked up). Fall back to the
            // legacy quick-accept flow so the driver isn't blocked.
            this._do_trip_accept_quick();
            return;
        }
        this._open_trip_start_dialog(first_pickup, source_manifests);
    }

    _do_trip_accept_quick() {
        frappe.confirm(__("Accept this trip and start now?"), () => {
            this._call_promise(TRIP_API + "driver_accept_trip", {
                trip: this.active_trip,
            }).then(() => {
                frappe.show_alert({ message: __("Trip accepted"), indicator: "green" });
                this.show_trip_detail(this.active_trip);
                this.load_data();
            });
        });
    }

    _open_trip_start_dialog(pickup_stop, source_manifests) {
        const t = this._trip_detail || {};
        const manifest_rows_html = source_manifests.map((m) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(m.name)}</div>
                <div class="text-muted" style="font-size:11px;">${frappe.utils.escape_html(m.destination_store || m.destination_warehouse || "—")}</div>
            </div>`).join("");

        const qr_fields = source_manifests.map((m) => ({
            fieldname: `qr__${m.name.replace(/[^A-Za-z0-9_]/g, "_")}`,
            fieldtype: "Data",
            label: __("Scan QR for {0}", [m.name]),
            reqd: 1,
        }));

        const fields = [
            {
                fieldname: "summary", fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:8px 10px;border-radius:6px;margin-bottom:8px;">
                    <strong><i class="fa fa-truck"></i> ${__("Verify load before trip starts")}</strong>
                    <div style="font-size:12px;margin-top:4px;">
                        ${__("Pickup at {0}", [frappe.utils.escape_html(pickup_stop.store || pickup_stop.warehouse || "—")])}
                        &middot; ${__("{0} manifest(s)", [source_manifests.length])}
                    </div>
                </div>
                <div class="da-stop-batch-list">${manifest_rows_html}</div>`,
            },
            {
                fieldname: "pickup_photo",
                fieldtype: "Attach Image",
                label: __("Photo of Loaded Vehicle / Goods"),
                reqd: 1,
                description: __("One photo of the loaded vehicle or dock — covers the whole load."),
            },
            {
                fieldname: "notes",
                fieldtype: "Small Text",
                label: __("Notes (optional)"),
            },
            { fieldtype: "Section Break", label: __("Scan each manifest QR") },
            ...qr_fields,
        ];

        const d = new frappe.ui.Dialog({
            title: __("Accept & Start Trip {0}", [t.name || ""]),
            size: "small",
            fields,
            primary_action_label: __("Confirm & Start Trip"),
            primary_action: (values) => {
                d.hide();
                this._submit_trip_start(pickup_stop, source_manifests, values);
            },
        });
        d.show();
    }

    _submit_trip_start(pickup_stop, source_manifests, values) {
        frappe.dom.freeze(__("Accepting trip & loading manifests…"));
        let captured_gps = null;
        return this._capture_gps_promise()
            .then(({ lat, lng }) => {
                captured_gps = { lat, lng };
                // Step 1: flip Assigned → Started so subsequent stop_arrive /
                // stop_complete calls are allowed by the server.
                return this._call_promise(TRIP_API + "driver_accept_trip", {
                    trip: this.active_trip,
                });
            })
            .then(() => {
                // Step 2: start_pickup for every Assigned manifest at the
                // source stop, with the shared load photo + per-manifest QR.
                return this._run_sequential(source_manifests, (m) => {
                    const qr_key = `qr__${m.name.replace(/[^A-Za-z0-9_]/g, "_")}`;
                    return this._call_promise(API + "start_pickup", {
                        manifest: m.name,
                        pickup_photo: values.pickup_photo,
                        scanned_qr: values[qr_key],
                        lat: captured_gps.lat, lng: captured_gps.lng,
                        notes: values.notes,
                    });
                });
            })
            .then((results) => {
                const { ok, fail } = this._summarise_batch(results);
                // Step 3: only close out the source stop if every manifest
                // was picked up cleanly. Otherwise leave it Pending so the
                // driver can retry the failing manifest individually.
                if (ok.length && !fail.length) {
                    return this._call_promise(TRIP_API + "stop_arrive", {
                        trip: this.active_trip,
                        sequence: pickup_stop.sequence,
                        gps_lat: captured_gps.lat,
                        gps_lng: captured_gps.lng,
                    }).then(() =>
                        this._call_promise(TRIP_API + "stop_complete", {
                            trip: this.active_trip,
                            sequence: pickup_stop.sequence,
                            scan_compliance_pct: 100,
                        })
                    ).then(() => ({ ok, fail }));
                }
                return { ok, fail };
            })
            .then(({ ok, fail }) => {
                frappe.dom.unfreeze();
                this._show_batch_result(__("Trip start pickup"), ok, fail);
                this.show_trip_detail(this.active_trip);
                this.load_data();
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                // driver_accept_trip / start_pickup errors already surface a
                // toast from Frappe's default ajax error handler. Refresh so
                // the driver sees the latest server-truth state — the trip
                // may already be Started even if a later step failed.
                this.show_trip_detail(this.active_trip);
                console.error("trip start gate failed", err);
            });
    }

    do_trip_reject() {
        frappe.prompt(
            [
                {
                    fieldname: "reason",
                    fieldtype: "Select",
                    label: __("Reason"),
                    options: "Vehicle Issue\nCapacity Full\nRoute Constraint\nPersonal Emergency\nOther",
                    reqd: 1,
                },
                {
                    fieldname: "notes",
                    fieldtype: "Small Text",
                    label: __("Notes"),
                },
            ],
            (values) => {
                frappe.call({
                    method: TRIP_API + "driver_reject_trip",
                    args: {
                        trip: this.active_trip,
                        reason: values.reason,
                        notes: values.notes,
                    },
                    callback: () => {
                        frappe.show_alert({ message: __("Trip rejected"), indicator: "orange" });
                        this.active_trip = null;
                        this.render_content();
                        this.load_data();
                    },
                });
            },
            __("Reject Trip"),
            __("Confirm")
        );
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

    do_manifest_close(manifest_name) {
        if (!manifest_name) return;
        frappe.confirm(__("Close manifest {0}?", [manifest_name]), () => {
            frappe.call({
                method: API + "driver_close_manifest",
                args: { manifest: manifest_name },
                callback: () => {
                    frappe.show_alert({ message: __("Manifest closed"), indicator: "green" });
                    if (this.active_trip) this.show_trip_detail(this.active_trip);
                    if (this.active_manifest === manifest_name) this.show_manifest_detail(manifest_name);
                    this.load_data();
                },
            });
        });
    }

    // ── Stop-level "Arrive" smart dispatcher ─────────────────────
    //
    // Carrier driver apps (Delhivery, Ekart, BlueDart, Bringg, FarEye, Onfleet)
    // collapse the per-shipment POD UI into a single per-stop CTA. The driver
    // arrives at the door, taps one button, photographs the load once,
    // collects the OTP(s) from the receiver, and the app fans the action out
    // across every manifest the route attached to that stop. We mirror that
    // here so the driver does not have to drill into each manifest card.
    do_stop_arrive(seq) {
        const stop = this._find_stop(seq);
        if (!stop) {
            frappe.show_alert({ message: __("Stop not found"), indicator: "red" });
            return;
        }
        const stop_type = (stop.stop_type || "").toLowerCase();
        if (stop_type === "pickup") {
            this._do_stop_pickup_flow(seq);
        } else if (stop_type === "drop") {
            this._do_stop_drop_flow(seq);
        } else {
            // Unknown stop type — fall back to a plain GPS arrival ping so we
            // never block the driver if a custom stop type slips through.
            this._record_stop_arrival(seq).then(() => {
                frappe.show_alert({ message: __("Stop arrival recorded"), indicator: "green" });
                this.show_trip_detail(this.active_trip);
            });
        }
    }

    do_stop_complete(seq) {
        // The combined "Arrive" flows already auto-fire stop_complete when all
        // manifests at the stop are handled. This manual CTA remains as a
        // fallback for legacy/partial cases where the driver finished a stop
        // through the per-manifest flow. Keep the original guard: refuse to
        // mark the stop complete while any manifest on it is still mid-flight.
        if (this._trip_detail && (this._trip_detail.manifests || []).length) {
            const open_rows = (this._trip_detail.manifests || []).filter((m) =>
                m.stop_sequence === seq && ["Assigned", "Pickup Started", "In Transit"].includes(m.status)
            );
            if (open_rows.length) {
                frappe.msgprint({
                    title: __("Finish Manifest Actions First"),
                    indicator: "orange",
                    message: __("Manifest <b>{0}</b> on this stop is still open. Use the stop's <b>Arrive</b> button to run pickup/delivery for all manifests at once, or open the manifest to finish it individually.", [open_rows[0].name]),
                });
                return;
            }
        }
        frappe.prompt(
            [{
                fieldname: "scan_compliance_pct",
                fieldtype: "Percent",
                label: __("Scan Compliance %"),
                default: 100,
                reqd: 1,
            }],
            (values) => {
                this._call_promise(TRIP_API + "stop_complete", {
                    trip: this.active_trip,
                    sequence: seq,
                    scan_compliance_pct: values.scan_compliance_pct,
                }).then(() => {
                    frappe.show_alert({ message: __("Stop completed"), indicator: "green" });
                    this.show_trip_detail(this.active_trip);
                });
            },
            __("Complete Stop"),
            __("Confirm")
        );
    }

    // ── Stop-level combined flows ────────────────────────────────

    _find_stop(seq) {
        const t = this._trip_detail || {};
        return (t.stops || []).find((s) => cint(s.sequence) === cint(seq));
    }

    _gather_stop_manifests(seq, statuses) {
        // ``stop_sequence`` on a manifest is set by ``_assign_stop_sequence``
        // to the manifest's DESTINATION stop (for forward trips) or SOURCE
        // stop (for reverse trips). So filtering purely by stop_sequence
        // works for the delivery side but never matches at the pickup stop.
        //
        // To make the combined Arrive & Pick Up flow work on multi-pickup
        // trips, we additionally match a pickup stop against the manifest's
        // source warehouse/store. Drop stops keep the strict stop_sequence
        // match so a manifest only appears under the stop it's actually
        // being delivered to.
        const t = this._trip_detail || {};
        const stop = (t.stops || []).find((s) => cint(s.sequence) === cint(seq));
        const stop_type = ((stop && stop.stop_type) || "").toLowerCase();
        const allowed = new Set((statuses || []).map((x) => x.toLowerCase()));
        return (t.manifests || []).filter((m) => {
            let matches = false;
            if (stop_type === "pickup" || stop_type === "pickup+drop") {
                // Server-resolved pickup stop (get_trip_detail) wins; the
                // source warehouse/store checks remain as legacy fallbacks.
                if (m.pickup_stop_sequence && cint(m.pickup_stop_sequence) === cint(seq)) matches = true;
                if (!matches && stop && stop.warehouse && m.source_warehouse === stop.warehouse) matches = true;
                if (!matches && stop && stop.store && m.source_store === stop.store) matches = true;
            }
            if (!matches && (stop_type === "drop" || stop_type === "pickup+drop")) {
                if (m.drop_stop_sequence && cint(m.drop_stop_sequence) === cint(seq)) matches = true;
                if (!matches && cint(m.stop_sequence) === cint(seq)) matches = true;
                // Fallback for trips that pre-date stop_sequence assignment.
                if (!matches) {
                    if (stop && stop.warehouse && m.destination_warehouse === stop.warehouse) matches = true;
                    if (stop && stop.store && m.destination_store === stop.store) matches = true;
                }
            }
            if (!matches) return false;
            if (!allowed.size) return true;
            return allowed.has((m.status || "").toLowerCase());
        });
    }

    _call_promise(method, args) {
        // Promisified frappe.call so we can chain the multi-manifest sequence
        // without nesting callbacks five levels deep. Server-side messages
        // (frappe.throw) still surface as red toasts via Frappe's default
        // ajax error handler, so we resolve only on success and reject on
        // network / server errors with the response payload.
        return new Promise((resolve, reject) => {
            frappe.call({
                method,
                args: args || {},
                callback: (r) => resolve(r && r.message),
                error: (err) => reject(err),
            });
        });
    }

    _capture_gps_promise() {
        // Self-contained GPS capture that rejects on denial/timeout so chained
        // ``frappe.dom.freeze`` calls in the combined-stop flows can always be
        // released. ``_capture_gps`` never invokes its callback on error
        // (it only shows a msgprint), which would leave the freeze overlay
        // stuck if we wrapped it directly.
        return new Promise((resolve, reject) => {
            if (!navigator.geolocation) {
                frappe.msgprint({
                    title: __("Location Required"),
                    indicator: "red",
                    message: __("This device does not support geolocation. Pickup / delivery cannot be confirmed without driver location."),
                });
                reject(new Error("geolocation unsupported"));
                return;
            }
            navigator.geolocation.getCurrentPosition(
                (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
                (err) => {
                    frappe.msgprint({
                        title: __("Location Required"),
                        indicator: "red",
                        message: __("Could not capture driver location ({0}). Enable Location on the device and retry.",
                            [(err && err.message) || __("permission denied")]),
                    });
                    reject(err || new Error("geolocation denied"));
                },
                { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 }
            );
        });
    }

    _record_stop_arrival(seq) {
        return this._capture_gps_promise().then(({ lat, lng }) => {
            return this._call_promise(TRIP_API + "stop_arrive", {
                trip: this.active_trip,
                sequence: seq,
                gps_lat: lat,
                gps_lng: lng,
            }).then(() => ({ lat, lng }));
        });
    }

    _run_sequential(items, fn) {
        // Run an async fn(item) over items one-at-a-time. We collect per-row
        // outcomes so the dialog can show the driver exactly which manifests
        // failed and which succeeded (carrier apps never silently swallow a
        // failed shipment in a batched POD).
        const results = [];
        let chain = Promise.resolve();
        items.forEach((item) => {
            chain = chain.then(() => {
                return fn(item)
                    .then((res) => results.push({ item, ok: true, res }))
                    .catch((err) => results.push({ item, ok: false, err }));
            });
        });
        return chain.then(() => results);
    }

    _summarise_batch(results) {
        const ok = results.filter((r) => r.ok).map((r) => r.item.name);
        const fail = results.filter((r) => !r.ok).map((r) => r.item.name);
        return { ok, fail };
    }

    // ---------- Pickup stop combined flow ------------------------
    _do_stop_pickup_flow(seq) {
        const candidates = this._gather_stop_manifests(seq, ["Assigned"]);
        const stop = this._find_stop(seq) || {};
        if (!candidates.length) {
            // Nothing left to pick up here. If every manifest at this stop
            // was already picked up (manifest-level Start Pickup), say so
            // and close the stop out in one tap — the old silent "arrival
            // recorded" ping made drivers think the pickup photo / manifest
            // scan had been skipped.
            const here = this._gather_stop_manifests(seq, []);
            const picked = here.filter((m) =>
                ["In Transit", "Delivered", "Received", "Partially Received", "Closed"].includes(m.status));
            if (here.length && picked.length === here.length) {
                return this._record_stop_arrival(seq)
                    .then(() => this._call_promise(TRIP_API + "stop_complete", {
                        trip: this.active_trip,
                        sequence: seq,
                        scan_compliance_pct: 100,
                    }))
                    .then(() => {
                        frappe.msgprint({
                            title: __("Pickup Already Recorded"),
                            indicator: "green",
                            message: __(
                                "Manifest(s) <b>{0}</b> at this stop were already picked up via the manifest flow — " +
                                "the pickup photo and QR scan are stored on each manifest (tap its card to view). " +
                                "Stop marked Completed.",
                                [picked.map((m) => m.name).join(", ")]
                            ),
                        });
                        this.show_trip_detail(this.active_trip);
                        this.load_data();
                    });
            }
            // No manifest could be matched to this stop at all — record the
            // arrival but tell the driver why no pickup dialog opened.
            return this._record_stop_arrival(seq).then(() => {
                frappe.show_alert({
                    message: here.length
                        ? __("Arrival recorded — no manifest here is awaiting pickup")
                        : __("Arrival recorded — no manifest is linked to this stop"),
                    indicator: here.length ? "green" : "orange",
                });
                this.show_trip_detail(this.active_trip);
            });
        }

        // ── Bundle-QR fast path ──────────────────────────────────
        // If the dispatcher printed a consolidated bundle label for this
        // stop (pickup_token on CH Logistics Trip Stop), let the driver
        // SCAN ONCE for the whole load instead of N times. Same pattern
        // as Delhivery's stop-handover scan and Ekart's bag-master scan.
        if (stop.has_pickup_token) {
            return this._do_stop_pickup_bundle(seq, candidates, stop);
        }

        // Build per-manifest QR fields. One pickup photo (the goods on the
        // dock) is shared across the load — that mirrors Ekart / Delhivery
        // handover scans where one rack photo covers all bundles in the lot.
        const manifest_rows_html = candidates.map((m) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(m.name)}</div>
                <div class="text-muted" style="font-size:11px;">${frappe.utils.escape_html(m.destination_store || m.destination_warehouse || "—")}</div>
            </div>`).join("");

        const qr_fields = candidates.map((m) => ({
            fieldname: `qr__${m.name.replace(/[^A-Za-z0-9_]/g, "_")}`,
            fieldtype: "Data",
            label: __("QR for {0}", [m.name]),
            reqd: 1,
        }));

        const fields = [
            {
                fieldname: "summary", fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:8px 10px;border-radius:6px;margin-bottom:8px;">
                    <strong><i class="fa fa-map-marker"></i> ${frappe.utils.escape_html(stop.store || stop.warehouse || "—")}</strong>
                    <div style="font-size:12px;">${__("Picking up {0} manifest(s) at this stop.", [candidates.length])}</div>
                </div>
                <div class="da-stop-batch-list">${manifest_rows_html}</div>`,
            },
            {
                fieldname: "pickup_photo",
                fieldtype: "Attach Image",
                label: __("Photo of Goods (single photo covers this load)"),
                reqd: 1,
            },
            {
                fieldname: "notes",
                fieldtype: "Small Text",
                label: __("Notes (optional)"),
            },
            { fieldtype: "Section Break", label: __("Scan each manifest QR") },
            ...qr_fields,
        ];

        const d = new frappe.ui.Dialog({
            title: __("Arrive & Pick Up — Stop #{0}", [seq]),
            size: "small",
            fields,
            primary_action_label: __("Confirm Pickup"),
            primary_action: (values) => {
                d.hide();
                this._submit_stop_pickup(seq, candidates, values);
            },
        });
        d.show();
    }

    _submit_stop_pickup(seq, candidates, values) {
        frappe.dom.freeze(__("Recording arrival & pickup…"));
        return this._record_stop_arrival(seq)
            .then(({ lat, lng }) => {
                return this._run_sequential(candidates, (m) => {
                    const qr_key = `qr__${m.name.replace(/[^A-Za-z0-9_]/g, "_")}`;
                    return this._call_promise(API + "start_pickup", {
                        manifest: m.name,
                        pickup_photo: values.pickup_photo,
                        scanned_qr: values[qr_key],
                        lat, lng,
                        notes: values.notes,
                    });
                });
            })
            .then((results) => {
                const { ok, fail } = this._summarise_batch(results);
                // If every manifest at this pickup stop succeeded, auto-mark
                // the stop Completed so the driver doesn't need a second tap.
                if (ok.length && !fail.length) {
                    return this._call_promise(TRIP_API + "stop_complete", {
                        trip: this.active_trip,
                        sequence: seq,
                        scan_compliance_pct: 100,
                    }).then(() => ({ ok, fail }));
                }
                return { ok, fail };
            })
            .then(({ ok, fail }) => {
                frappe.dom.unfreeze();
                this._show_batch_result(__("Pickup"), ok, fail);
                this.show_trip_detail(this.active_trip);
                this.load_data();
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                // Errors from frappe.call already show a toast/dialog. Refresh
                // anyway so the driver sees the latest server state.
                this.show_trip_detail(this.active_trip);
                console.error("stop pickup flow failed", err);
            });
    }

    // ---------- Drop stop combined flow --------------------------
    _do_stop_drop_flow(seq) {
        const deliverable = this._gather_stop_manifests(seq, ["In Transit"]);
        const not_yet = this._gather_stop_manifests(seq, ["Assigned", "Pickup Started"]);
        const stop = this._find_stop(seq) || {};

        if (!deliverable.length) {
            // No In-Transit manifest at this drop. Either everything was
            // already delivered (just ping GPS), or pickup at the source
            // never happened (block and tell the driver).
            if (not_yet.length) {
                frappe.msgprint({
                    title: __("Pickup Not Done Yet"),
                    indicator: "orange",
                    message: __(
                        "These manifest(s) at this drop are not In Transit yet: <b>{0}</b>.<br>" +
                        "Go back to the pickup stop and run <b>Arrive &amp; Pick Up</b> first, " +
                        "or open the manifest to complete pickup individually.",
                        [not_yet.map((m) => m.name).join(", ")]
                    ),
                });
                return;
            }
            return this._record_stop_arrival(seq).then(() => {
                frappe.show_alert({ message: __("Stop arrival recorded"), indicator: "green" });
                this.show_trip_detail(this.active_trip);
            });
        }

        // ── Bundle-QR fast path ──────────────────────────────────
        // If the dispatcher printed a consolidated drop label (delivery
        // token on CH Logistics Trip Stop), use the one-scan / one-OTP
        // path. The server mints a single shared OTP for the whole stop
        // and complete_stop_delivery cascades to every manifest.
        if (stop.has_delivery_token) {
            return this._do_stop_drop_bundle(seq, deliverable, not_yet, stop);
        }

        // Two-stage POD: record stop arrival + per-manifest geofence ping +
        // generate OTPs BEFORE opening the dialog, so the driver sees fresh
        // OTPs land at the store before they ask for them.
        frappe.dom.freeze(__("Recording arrival & sending OTPs…"));
        this._record_stop_arrival(seq)
            .then(({ lat, lng }) => {
                this._stop_drop_gps = { lat, lng };
                return this._run_sequential(deliverable, (m) => {
                    // Per-manifest: mark reached (sets arrival_datetime so
                    // complete_delivery is unlocked) then request fresh OTP.
                    return this._call_promise(API + "mark_reached_destination", {
                        manifest: m.name, lat, lng,
                    }).then(() => {
                        return this._call_promise(API + "request_delivery_otp", {
                            manifest: m.name,
                        }).then((info) => ({ manifest: m, otp_info: info || {} }));
                    });
                });
            })
            .then((results) => {
                frappe.dom.unfreeze();
                // Manifests that failed reached/OTP — surface but still allow
                // the driver to deliver the ones that succeeded.
                const ready = results.filter((r) => r.ok).map((r) => r.res);
                const skipped = results.filter((r) => !r.ok).map((r) => r.item);
                if (!ready.length) {
                    frappe.msgprint({
                        title: __("Could Not Prepare Delivery"),
                        indicator: "red",
                        message: __("None of the manifests at this stop accepted the arrival ping. Check the manifest cards for details."),
                    });
                    this.show_trip_detail(this.active_trip);
                    return;
                }
                this._open_stop_delivery_dialog(seq, ready, skipped, stop);
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                this.show_trip_detail(this.active_trip);
                console.error("stop drop prepare failed", err);
            });
    }

    _open_stop_delivery_dialog(seq, ready, skipped, stop) {
        // ``ready`` = [{ manifest, otp_info }] (mark_reached + OTP succeeded)
        const recipients_block = (info) => {
            if (!info) return "";
            const parts = [];
            if ((info.masked_emails || []).length) {
                parts.push(__("Email: {0}", [info.masked_emails.map(frappe.utils.escape_html).join(", ")]));
            }
            if ((info.masked_mobiles || []).length) {
                parts.push(__("SMS: {0}", [info.masked_mobiles.map(frappe.utils.escape_html).join(", ")]));
            }
            return parts.length
                ? `<div class="text-muted" style="font-size:11px;">${__("OTP sent")} — ${parts.join(" • ")}</div>`
                : `<div class="text-muted" style="font-size:11px;">${__("OTP regenerated. Ask the store directly.")}</div>`;
        };

        const ready_rows_html = ready.map((r) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(r.manifest.name)}</div>
                ${recipients_block(r.otp_info)}
            </div>`).join("");

        const skipped_html = skipped.length
            ? `<div class="alert alert-warning" style="padding:8px 10px;border-radius:6px;margin:8px 0;font-size:12px;">
                ${__("Skipped (not ready)")}: <b>${skipped.map((m) => frappe.utils.escape_html(m.name)).join(", ")}</b>
              </div>` : "";

        const per_manifest_fields = [];
        ready.forEach((r, idx) => {
            const safe = r.manifest.name.replace(/[^A-Za-z0-9_]/g, "_");
            per_manifest_fields.push({ fieldtype: "Section Break", label: r.manifest.name });
            per_manifest_fields.push({
                fieldname: `otp__${safe}`,
                fieldtype: "Data",
                label: __("OTP for {0}", [r.manifest.name]),
                reqd: 1,
            });
            per_manifest_fields.push({
                fieldname: `qr__${safe}`,
                fieldtype: "Data",
                label: __("Scan QR for {0}", [r.manifest.name]),
                reqd: 1,
            });
            if (idx < ready.length - 1) {
                per_manifest_fields.push({ fieldtype: "Column Break" });
            }
        });

        const fields = [
            {
                fieldname: "summary", fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:8px 10px;border-radius:6px;margin-bottom:8px;">
                    <strong><i class="fa fa-map-marker"></i> ${frappe.utils.escape_html(stop.store || stop.warehouse || "—")}</strong>
                    <div style="font-size:12px;">${__("Delivering {0} manifest(s) at this stop.", [ready.length])}</div>
                </div>
                <div class="da-stop-batch-list">${ready_rows_html}</div>
                ${skipped_html}`,
            },
            {
                fieldname: "delivery_photo",
                fieldtype: "Attach Image",
                label: __("Photo of Delivery (single photo covers this drop)"),
                reqd: 1,
            },
            {
                fieldname: "receiver_name",
                fieldtype: "Data",
                label: __("Receiver Name (delivered to)"),
                reqd: 1,
            },
            { fieldtype: "Section Break", label: __("Per-manifest OTP & QR") },
            ...per_manifest_fields,
        ];

        const d = new frappe.ui.Dialog({
            title: __("Arrive & Deliver — Stop #{0}", [seq]),
            size: "large",
            fields,
            primary_action_label: __("Confirm Delivery"),
            primary_action: (values) => {
                d.hide();
                this._submit_stop_delivery(seq, ready, skipped, values);
            },
            secondary_action_label: __("Resend OTPs"),
            secondary_action: () => {
                this._run_sequential(ready, (r) =>
                    this._call_promise(API + "request_delivery_otp", { manifest: r.manifest.name })
                ).then(() => {
                    frappe.show_alert({ message: __("OTPs resent"), indicator: "blue" });
                });
            },
        });
        d.show();
    }

    _submit_stop_delivery(seq, ready, skipped, values) {
        const gps = this._stop_drop_gps || {};
        frappe.dom.freeze(__("Completing delivery…"));
        return this._run_sequential(ready, (r) => {
            const safe = r.manifest.name.replace(/[^A-Za-z0-9_]/g, "_");
            return this._call_promise(API + "complete_delivery", {
                manifest: r.manifest.name,
                delivery_photo: values.delivery_photo,
                receiver_name: values.receiver_name,
                scanned_qr: values[`qr__${safe}`],
                otp: values[`otp__${safe}`],
                lat: gps.lat, lng: gps.lng,
            });
        })
            .then((results) => {
                const { ok, fail } = this._summarise_batch(
                    results.map((r) => ({ item: r.item.manifest, ok: r.ok, err: r.err }))
                );
                // Auto-complete the stop only when every deliverable manifest
                // succeeded AND we didn't pre-skip any (i.e. no pickup-debt
                // left). Otherwise leave the stop open so the driver can
                // resolve the stragglers individually.
                if (ok.length && !fail.length && !skipped.length) {
                    return this._call_promise(TRIP_API + "stop_complete", {
                        trip: this.active_trip,
                        sequence: seq,
                        scan_compliance_pct: 100,
                    }).then(() => ({ ok, fail }));
                }
                return { ok, fail };
            })
            .then(({ ok, fail }) => {
                frappe.dom.unfreeze();
                this._show_batch_result(__("Delivery"), ok, fail);
                this.show_trip_detail(this.active_trip);
                this.load_data();
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                this.show_trip_detail(this.active_trip);
                console.error("stop drop submit failed", err);
            });
    }

    _show_batch_result(label, ok, fail) {
        if (ok.length && !fail.length) {
            frappe.show_alert({
                message: __("{0} completed for {1} manifest(s).", [label, ok.length]),
                indicator: "green",
            });
            return;
        }
        if (ok.length && fail.length) {
            frappe.msgprint({
                title: __("{0} partly completed", [label]),
                indicator: "orange",
                message: __("Succeeded: <b>{0}</b><br>Failed: <b>{1}</b><br>Open the failed manifest(s) to retry.",
                    [ok.join(", ") || "—", fail.join(", ")]),
            });
            return;
        }
        if (fail.length) {
            frappe.msgprint({
                title: __("{0} failed", [label]),
                indicator: "red",
                message: __("None of the manifests completed: <b>{0}</b>. Open them individually to see the server error.",
                    [fail.join(", ")]),
            });
        }
    }

    // ── Bundle-QR pickup flow ────────────────────────────────────
    // One scan unlocks the WHOLE stop. Backed by start_stop_pickup,
    // which cascades start_pickup to every manifest at the stop
    // (Assigned → In Transit) and sets stop.status = Arrived.
    _do_stop_pickup_bundle(seq, candidates, stop) {
        const manifest_rows_html = candidates.map((m) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(m.name)}</div>
                <div class="text-muted" style="font-size:11px;">${frappe.utils.escape_html(m.destination_store || m.destination_warehouse || "—")}</div>
            </div>`).join("");

        const fields = [
            {
                fieldname: "summary", fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:8px 10px;border-radius:6px;margin-bottom:8px;">
                    <strong><i class="fa fa-qrcode"></i> ${__("Bundle pickup")} — ${frappe.utils.escape_html(stop.store || stop.warehouse || "—")}</strong>
                    <div style="font-size:12px;">${__("One scan picks up all {0} manifest(s) at this stop.", [candidates.length])}</div>
                </div>
                <div class="da-stop-batch-list">${manifest_rows_html}</div>`,
            },
            {
                fieldname: "scanned_qr",
                fieldtype: "Data",
                label: __("Scan Stop Pickup QR"),
                reqd: 1,
                description: __("Scan the consolidated pickup label printed at dispatch. Pickup is blocked until it matches."),
            },
            {
                fieldname: "pickup_photo",
                fieldtype: "Attach Image",
                label: __("Photo of Goods (single photo covers the load)"),
                reqd: 1,
            },
            {
                fieldname: "notes",
                fieldtype: "Small Text",
                label: __("Notes (optional)"),
            },
        ];

        const d = new frappe.ui.Dialog({
            title: __("Arrive & Pick Up — Stop #{0} (Bundle)", [seq]),
            size: "small",
            fields,
            primary_action_label: __("Confirm Pickup"),
            primary_action: (values) => {
                d.hide();
                this._submit_stop_pickup_bundle(seq, candidates, values);
            },
        });
        d.show();
    }

    _submit_stop_pickup_bundle(seq, candidates, values) {
        frappe.dom.freeze(__("Recording arrival & cascading pickup…"));
        return this._capture_gps_promise()
            .then(({ lat, lng }) => {
                return this._call_promise(TRIP_API + "start_stop_pickup", {
                    trip: this.active_trip,
                    sequence: seq,
                    scanned_qr: values.scanned_qr,
                    pickup_photo: values.pickup_photo,
                    lat, lng,
                    notes: values.notes,
                });
            })
            .then((res) => {
                const ok = (res && res.started) || [];
                const fail = ((res && res.skipped) || []).map((s) => s.name);
                // Keep parity with the legacy per-manifest path: when
                // every manifest at the stop succeeded, finalise the
                // trip-level stop accounting so scan_compliance_pct
                // reports the same value as the older flow.
                const all_ok = ok.length && !fail.length;
                const tail = all_ok
                    ? this._call_promise(TRIP_API + "stop_complete", {
                          trip: this.active_trip,
                          sequence: seq,
                          scan_compliance_pct: 100,
                      })
                    : Promise.resolve();
                return tail.then(() => ({ ok, fail }));
            })
            .then(({ ok, fail }) => {
                frappe.dom.unfreeze();
                this._show_batch_result(__("Bundle pickup"), ok, fail);
                this.show_trip_detail(this.active_trip);
                this.load_data();
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                this.show_trip_detail(this.active_trip);
                console.error("bundle pickup flow failed", err);
            });
    }

    // ── Bundle-QR drop flow ──────────────────────────────────────
    // One OTP + one scan for the whole stop. Backed by request_stop_otp
    // (mints a shared OTP across every manifest) and
    // complete_stop_delivery (cascades complete_delivery so every
    // manifest goes In Transit → Delivered in one atomic call).
    _do_stop_drop_bundle(seq, deliverable, not_yet, stop) {
        // Soft-warn about manifests still in pickup state — the bundle
        // call would reject (request_stop_otp requires every manifest at
        // the stop to be In Transit) so surface the problem up front.
        if (not_yet && not_yet.length) {
            frappe.msgprint({
                title: __("Pickup Not Done Yet"),
                indicator: "orange",
                message: __(
                    "These manifest(s) at this drop are not In Transit yet: <b>{0}</b>.<br>" +
                    "Return to the pickup stop and run <b>Arrive &amp; Pick Up</b> first, " +
                    "or open the manifest to complete pickup individually.",
                    [not_yet.map((m) => m.name).join(", ")]
                ),
            });
            return;
        }

        frappe.dom.freeze(__("Capturing arrival & sending one OTP for this stop…"));
        let gps_cache = null;
        this._capture_gps_promise()
            .then(({ lat, lng }) => {
                gps_cache = { lat, lng };
                return this._call_promise(TRIP_API + "request_stop_otp", {
                    trip: this.active_trip,
                    sequence: seq,
                    lat, lng,
                });
            })
            .then((info) => {
                frappe.dom.unfreeze();
                this._open_stop_drop_bundle_dialog(seq, deliverable, stop, info || {}, gps_cache);
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                this.show_trip_detail(this.active_trip);
                console.error("bundle drop prepare failed", err);
            });
    }

    _open_stop_drop_bundle_dialog(seq, deliverable, stop, otp_info, gps) {
        const recipients_block = (info) => {
            if (!info) return "";
            const parts = [];
            if ((info.masked_emails || []).length) {
                parts.push(__("Email: {0}", [info.masked_emails.map(frappe.utils.escape_html).join(", ")]));
            }
            if ((info.masked_mobiles || []).length) {
                parts.push(__("SMS: {0}", [info.masked_mobiles.map(frappe.utils.escape_html).join(", ")]));
            }
            return parts.length
                ? `<div class="text-muted" style="font-size:11px;margin-top:4px;">${__("OTP sent")} — ${parts.join(" • ")}</div>`
                : `<div class="text-muted" style="font-size:11px;margin-top:4px;">${__("OTP regenerated. Ask the store directly.")}</div>`;
        };

        const manifest_rows_html = deliverable.map((m) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(m.name)}</div>
            </div>`).join("");

        const fields = [
            {
                fieldname: "summary", fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:8px 10px;border-radius:6px;margin-bottom:8px;">
                    <strong><i class="fa fa-qrcode"></i> ${__("Bundle drop")} — ${frappe.utils.escape_html(stop.store || stop.warehouse || "—")}</strong>
                    <div style="font-size:12px;">${__("One scan + one OTP delivers all {0} manifest(s) at this stop.", [deliverable.length])}</div>
                    ${recipients_block(otp_info)}
                </div>
                <div class="da-stop-batch-list">${manifest_rows_html}</div>`,
            },
            {
                fieldname: "scanned_qr",
                fieldtype: "Data",
                label: __("Scan Stop Delivery QR"),
                reqd: 1,
                description: __("Scan the consolidated drop label printed at dispatch. Delivery is blocked until it matches."),
            },
            {
                fieldname: "otp",
                fieldtype: "Data",
                label: __("Receiver OTP (one code unlocks the whole stop)"),
                reqd: 1,
            },
            {
                fieldname: "receiver_name",
                fieldtype: "Data",
                label: __("Receiver Name (delivered to)"),
                reqd: 1,
            },
            {
                fieldname: "delivery_photo",
                fieldtype: "Attach Image",
                label: __("Photo of Delivery (single photo covers this drop)"),
                reqd: 1,
            },
            {
                fieldname: "notes",
                fieldtype: "Small Text",
                label: __("Notes (optional)"),
            },
        ];

        const d = new frappe.ui.Dialog({
            title: __("Arrive & Deliver — Stop #{0} (Bundle)", [seq]),
            size: "small",
            fields,
            primary_action_label: __("Confirm Delivery"),
            primary_action: (values) => {
                d.hide();
                this._submit_stop_drop_bundle(seq, deliverable, values, gps);
            },
            secondary_action_label: __("Resend OTP"),
            secondary_action: () => {
                this._call_promise(TRIP_API + "request_stop_otp", {
                    trip: this.active_trip,
                    sequence: seq,
                    lat: gps && gps.lat,
                    lng: gps && gps.lng,
                }).then(() => {
                    frappe.show_alert({ message: __("OTP resent"), indicator: "blue" });
                });
            },
        });
        d.show();
    }

    _submit_stop_drop_bundle(seq, deliverable, values, gps) {
        frappe.dom.freeze(__("Completing delivery for the whole stop…"));
        const lat = (gps && gps.lat) || null;
        const lng = (gps && gps.lng) || null;
        return this._call_promise(TRIP_API + "complete_stop_delivery", {
            trip: this.active_trip,
            sequence: seq,
            scanned_qr: values.scanned_qr,
            delivery_photo: values.delivery_photo,
            receiver_name: values.receiver_name,
            otp: values.otp,
            lat, lng,
            notes: values.notes,
        })
            .then((res) => {
                const ok = (res && res.delivered) || [];
                const fail = ((res && res.skipped) || []).map((s) => s.name);
                const all_ok = ok.length && !fail.length;
                const tail = all_ok
                    ? this._call_promise(TRIP_API + "stop_complete", {
                          trip: this.active_trip,
                          sequence: seq,
                          scan_compliance_pct: 100,
                      })
                    : Promise.resolve();
                return tail.then(() => ({ ok, fail }));
            })
            .then(({ ok, fail }) => {
                frappe.dom.unfreeze();
                this._show_batch_result(__("Bundle delivery"), ok, fail);
                this.show_trip_detail(this.active_trip);
                this.load_data();
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                this.show_trip_detail(this.active_trip);
                console.error("bundle drop submit failed", err);
            });
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
