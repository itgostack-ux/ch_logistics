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
    // "Has been picked up" — the Drops tile/dialog counts a manifest from
    // the moment it's picked up all the way through delivery, rather than
    // dropping it the instant it's actually Delivered. Excludes dead-end
    // states (Rejected/Cancelled/Returned) since nothing was actually
    // dropped there. Shared by the Drops stat tile and
    // _show_shipment_breakdown("drop") so they can never disagree.
    static DROP_STAGE_STATUSES = [
        "In Transit", "Recall Initiated", "Delivered",
        "Partially Received", "Received", "Closed",
    ];

    constructor(wrapper) {
        this.$wrapper = $(wrapper);
        this.page = wrapper.page;
        this.$body = $('<div class="delivery-app-root"></div>').appendTo(
            this.page.body
        );
        this.active_tab = "pickup";
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
        // Three real tabs — Pickup / Drop / History — each showing only its
        // own single list (one at a time), replacing both the old
        // My Trips/History pair AND the brief single-screen 3-column
        // layout that came before this (columns side-by-side wrapped
        // awkwardly and wasted space on a phone-width driver app).
        this.$body.html(`
            <div class="da-statusbar" id="da-statusbar"></div>
            <div class="da-tabs">
                <button class="da-tab active" data-tab="pickup">
                    <i class="fa fa-camera"></i> ${__("Pickup")} <span class="da-tab-count" id="da-tab-count-pickup"></span>
                </button>
                <button class="da-tab" data-tab="drop">
                    <i class="fa fa-check-circle"></i> ${__("Drop")} <span class="da-tab-count" id="da-tab-count-drop"></span>
                </button>
                <button class="da-tab" data-tab="history">
                    <i class="fa fa-history"></i> ${__("History")}
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

    load_status(after) {
        frappe.call({
            method: DRIVER_API + "get_status",
            callback: (r) => {
                this.render_status(r.message || {});
                if (after) after();
            },
        });
    }

    render_status(p) {
        let status = p.availability_status || "Offline";
        this.driver_status = status;
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
            // A cancelled board pickup/deliver dialog never fires the
            // callback that would otherwise clear this — an explicit tab
            // switch is as good a signal as any that the driver moved on.
            this._board_mode = false;
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

        // Pickup/Drop board (flattened across every active trip) — Accept/
        // Reject act on the board itself (stay put, just refresh). Start
        // Pickup / Deliver go straight into the QR/photo/GPS capture
        // dialog for THIS manifest — they load that manifest's trip detail
        // first (needed for stop sequence/role/candidate data) and fire
        // the same flow the trip page's own buttons use, rather than just
        // dropping the driver on the trip page to find and tap it again.
        this.$body.on("click", ".da-board-accept-btn", (e) => {
            e.stopPropagation();
            const $btn = $(e.currentTarget);
            this.do_board_manifest_accept($btn.data("name"), $btn.data("trip"));
        });
        this.$body.on("click", ".da-board-pickup-btn", (e) => {
            e.stopPropagation();
            const $btn = $(e.currentTarget);
            this.do_board_start_pickup($btn.data("trip"), $btn.data("name"));
        });
        this.$body.on("click", ".da-board-deliver-btn", (e) => {
            e.stopPropagation();
            const $btn = $(e.currentTarget);
            this.do_board_start_deliver($btn.data("trip"), $btn.data("name"));
        });

        this.$body.on("click", "#da-back-btn", () => {
            this.active_manifest = null;
            this.active_trip = null;
            this._board_mode = false;
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
        this.$body.on("click", ".da-stop-manifest-reject-btn", (e) => {
            e.stopPropagation();
            // Trip-level manifest rejection: bounce ONE shipment off the trip
            // without rejecting the whole trip (that stays #da-trip-reject-btn).
            this.active_manifest = $(e.currentTarget).data("name");
            this.do_reject({ from_trip: true });
        });
        this.$body.on("click", ".da-manifest-accept-btn", (e) => {
            e.stopPropagation();
            this.do_manifest_accept($(e.currentTarget).data("name"));
        });
        this.$body.on("click", ".da-stop-manifest-close-btn", (e) => {
            e.stopPropagation();
            let name = $(e.currentTarget).data("name");
            this.do_manifest_close(name);
        });

        this.$body.on("click", ".da-stop-arrive-btn", (e) => {
            let seq = $(e.currentTarget).data("seq");
            this.do_stop_arrive(seq);
        });
        this.$body.on("click", ".da-stop-pickup-btn", (e) => {
            let seq = $(e.currentTarget).data("seq");
            this._do_stop_pickup_flow(seq);
        });
        this.$body.on("click", ".da-stop-deliver-btn", (e) => {
            let seq = $(e.currentTarget).data("seq");
            this._do_stop_drop_flow(seq);
        });
        this.$body.on("click", ".da-stop-complete-btn", (e) => {
            let seq = $(e.currentTarget).data("seq");
            this.do_stop_complete(seq);
        });
        this.$body.on("click", ".da-stop-manifest-link", (e) => {
            let name = $(e.currentTarget).data("name");
            this.show_manifest_detail(name);
        });
        this.$body.on("click", ".da-stat-clickable", (e) => {
            let role = $(e.currentTarget).data("shipment-role");
            this._show_shipment_breakdown(role);
        });
    }

    load_data() {
        // All three feed whichever of the Pickup/Drop/History tabs is
        // currently open, so any of them arriving re-renders the view — as
        // long as the driver isn't looking at a specific trip or manifest
        // detail page right now.
        const maybe_render = () => {
            if (!this.active_trip && !this.active_manifest) this.render_content();
        };
        frappe.call({
            method: TRIP_API + "get_driver_trips",
            args: { include_closed_days: 7 },
            callback: (r) => {
                this.trips = r.message || { active: [], history: [] };
                maybe_render();
            },
        });
        frappe.call({
            method: API + "get_driver_assignments",
            callback: (r) => {
                this.manifests = r.message || [];
                this._update_tab_counts();
                maybe_render();
            },
        });
        frappe.call({
            method: API + "get_delivery_history",
            callback: (r) => {
                this.history = r.message || [];
                maybe_render();
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
        if (this.active_tab === "drop") {
            this.render_manifest_column($c, "in_transit", __("Nothing here right now."));
            return;
        }
        if (this.active_tab === "history") {
            this.render_history_column($c);
            return;
        }
        this.render_manifest_column($c, "to_pickup", __("Nothing here right now."));
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

    _update_tab_counts() {
        // Pickup = total still needing pickup. Drop = how many have moved
        // over from Pickup (picked up, now awaiting delivery) — the same
        // "in_transit" bucket the Drop tab itself now shows, so the tab
        // label and the list it opens always agree.
        const manifests = this.manifests || [];
        const pickup_n = manifests.filter((m) => m.bucket === "to_pickup").length;
        const drop_n = manifests.filter((m) => m.bucket === "in_transit").length;
        this.$body.find("#da-tab-count-pickup").text(`(${pickup_n})`);
        this.$body.find("#da-tab-count-drop").text(`(${drop_n})`);
    }

    render_manifest_column($c, bucketFilter, empty_msg) {
        // Pickup and Drop tabs, one list at a time — flattened across ALL
        // of the driver's active trips (not grouped by trip).
        // get_driver_assignments already resolves the split (bucket:
        // to_pickup / in_transit / awaiting_receipt). Drop only shows
        // "in_transit" — once a manifest is actually Delivered there's
        // nothing left for the DRIVER to act on (the receiving store has
        // to Scan & Receive it), so it clears out of Drop the same way a
        // manifest already clears out of Pickup the moment it's picked up.
        // It doesn't disappear from the app: get_delivery_history already
        // includes "Delivered", so it shows up in History instead.
        const manifests = this.manifests || [];
        const rows = manifests.filter((m) => m.bucket === bucketFilter);

        if (!rows.length) {
            $c.html(`<div class="da-empty">
                <i class="fa fa-truck fa-3x"></i>
                <p>${empty_msg}</p>
            </div>`);
            return;
        }
        $c.html(`<div class="da-manifest-list">${rows.map((m) => this._render_board_manifest_card(m)).join("")}</div>`);
    }

    render_history_column($c) {
        // Untouched from before the tab reshuffle — same data
        // (get_delivery_history) and same card rendering (manifest_card).
        let list = this.history || [];
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

    _render_board_manifest_card(m) {
        const label = (store, wh) => store
            ? frappe.utils.escape_html(store)
            : (window.ch_wh_label_html ? ch_wh_label_html(wh, "—") : frappe.utils.escape_html(wh || "—"));
        const st_cls = (m.status || "").toLowerCase().replace(/\s+/g, "-");
        const trip_badge = m.trip
            ? `<span class="da-trip-card" data-name="${frappe.utils.escape_html(m.trip)}" style="cursor:pointer;font-size:11px;color:#888;">
                   <i class="fa fa-truck"></i> ${frappe.utils.escape_html(m.trip)}
               </span>`
            : "";

        let actions = "";
        if (m.status === "Assigned" && !m.driver_accepted_at) {
            actions = `
                <button class="btn btn-success btn-sm da-board-accept-btn" data-name="${frappe.utils.escape_html(m.name)}" data-trip="${frappe.utils.escape_html(m.trip || "")}"><i class="fa fa-check"></i> ${__("Accept Shipment")}</button>
                <button class="btn btn-danger btn-sm da-stop-manifest-reject-btn" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-ban"></i> ${__("Reject")}</button>`;
        } else if (m.status === "Assigned" || m.status === "Pickup Started") {
            actions = `<button class="btn btn-primary btn-sm da-board-pickup-btn" data-trip="${frappe.utils.escape_html(m.trip || "")}" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-camera"></i> ${__("Start Pickup")}</button>`;
        } else if (m.status === "In Transit") {
            actions = `<button class="btn btn-primary btn-sm da-board-deliver-btn" data-trip="${frappe.utils.escape_html(m.trip || "")}" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-check-circle"></i> ${__("Deliver")}</button>`;
        } else if (m.status === "Delivered") {
            actions = `<span class="da-awaiting-pill text-muted" style="font-size:11px;" title="${__("The destination store must Scan & Receive this shipment")}"><i class="fa fa-clock-o"></i> ${__("Awaiting receipt")}</span>`;
        }

        return `
            <div class="da-stop-card">
                <div class="da-stop-header">
                    <span class="da-card-status da-status-${st_cls}">${frappe.utils.escape_html(m.status)}</span>
                    ${trip_badge}
                </div>
                <div class="da-stop-where">
                    <i class="fa fa-map-marker"></i> ${label(m.source_store, m.source_warehouse)}
                    <i class="fa fa-long-arrow-right" style="margin:0 4px;"></i>
                    ${label(m.destination_store, m.destination_warehouse)}
                </div>
                <div class="text-muted" style="font-size:11px;padding:0 8px 6px;">
                    <i class="fa fa-file-text-o"></i> ${frappe.utils.escape_html(m.name)}
                </div>
                <div class="da-stop-actions">${actions}</div>
            </div>`;
    }

    do_board_manifest_accept(name, trip) {
        // Board-local accept: unlike do_manifest_accept (used inside a
        // trip's own page, which reloads that trip's detail on success),
        // this must NOT navigate into the trip — the driver is browsing
        // across all their trips at once, so accepting one shipment should
        // just refresh the board, not jump them into a single trip's page.
        if (!name || !trip) return;
        // Same empty-stop pre-flight do_manifest_accept runs before a
        // trip's FIRST accept (which is what actually flips it to
        // Started) — skipping it here would hit the server's hard throw
        // instead of a dismissible warning. this.trips.active carries each
        // trip's current status, same data trip_card() already renders
        // from, so no extra round-trip is needed to check that part.
        const trip_row = ((this.trips && this.trips.active) || []).find((t) => t.name === trip);
        if (trip_row && trip_row.status === "Assigned") {
            // Load the trip detail first — _describe_empty_stops needs
            // this._trip_detail.stops to resolve store names. Without it,
            // this fell back to bare "#2, #3, #5" (the board has no trip
            // context loaded yet at this point, unlike the trip page's own
            // Accept flow which already has it).
            this._load_trip_detail_silently(trip, () => {
                this._call_promise(TRIP_API + "get_empty_stop_warning", { trip }).then((info) => {
                    const empty = (info && info.empty_stops) || [];
                    if (!empty.length) {
                        this._submit_board_manifest_accept(name, trip, 0);
                        return;
                    }
                    frappe.confirm(
                        __("Stop(s) {0} on {1} have no shipments assigned — nothing to load or unload there. Start anyway?",
                            [this._describe_empty_stops(empty).join(", "), trip]),
                        () => this._submit_board_manifest_accept(name, trip, 1)
                    );
                });
            });
            return;
        }
        this._submit_board_manifest_accept(name, trip, 0);
    }

    _submit_board_manifest_accept(name, trip, overrideEmptyStops) {
        frappe.dom.freeze(__("Accepting…"));
        this._call_promise(TRIP_API + "driver_accept_manifest", {
            trip,
            manifest: name,
            override_empty_stops: overrideEmptyStops ? 1 : 0,
        }).then(() => {
            frappe.dom.unfreeze();
            frappe.show_alert({ message: __("Manifest accepted"), indicator: "green" });
            // Accept Shipment now goes straight into the pickup capture
            // dialog instead of stopping at "accepted, tap Start Pickup
            // next" — one tap does both. If the driver cancels that dialog
            // without completing it, the card's own fallback still covers
            // them: an accepted-but-not-picked-up manifest keeps showing
            // its own Start Pickup button to retry from (see
            // _render_board_manifest_card), so nothing is lost by merging
            // the two steps here.
            this._board_fire_pickup_flow(trip, name);
        }).catch(() => frappe.dom.unfreeze());
    }

    _board_fire_pickup_flow(trip, name) {
        this._load_trip_detail_silently(trip, () => {
            const m = ((this._trip_detail && this._trip_detail.manifests) || []).find((x) => x.name === name);
            const seq = m && m.pickup_stop_sequence;
            if (!seq) {
                frappe.show_alert({ message: __("Could not find a pickup stop for this manifest — open the trip to continue."), indicator: "orange" });
                this.load_data();
                return;
            }
            this._board_mode = true;
            this._do_stop_pickup_flow(seq);
        });
    }

    _load_trip_detail_silently(trip, after) {
        // Populates this._trip_detail (stop sequence/role/candidate data
        // the pickup and drop flows need) WITHOUT rendering the trip page
        // underneath — the board stays exactly as the driver left it, and
        // only the capture dialog appears on top of it.
        frappe.call({
            method: TRIP_API + "get_trip_detail",
            args: { trip },
            callback: (r) => {
                if (!r.message) {
                    frappe.show_alert({ message: __("Trip not found"), indicator: "red" });
                    return;
                }
                this.active_trip = trip;
                this._trip_detail = r.message;
                if (typeof after === "function") after();
            },
        });
    }

    do_board_start_pickup(trip, name) {
        // Jump straight into the QR/photo/GPS capture dialog for THIS
        // manifest instead of dropping the driver on the trip page to find
        // and tap it themselves — loads the trip detail first (stop
        // sequence, roles, candidates all live there), then fires the same
        // _do_stop_pickup_flow the trip page's own Pick Up button uses. The
        // board itself stays on screen behind the dialog the whole time.
        if (!trip || !name) return;
        frappe.dom.freeze(__("Loading…"));
        this._load_trip_detail_silently(trip, () => {
            frappe.dom.unfreeze();
            const m = ((this._trip_detail && this._trip_detail.manifests) || []).find((x) => x.name === name);
            const seq = m && m.pickup_stop_sequence;
            if (!seq) {
                frappe.show_alert({ message: __("Could not find a pickup stop for this manifest — open the trip to continue."), indicator: "orange" });
                return;
            }
            // Marks that whatever "refresh the trip view" call the pickup
            // flow makes on completion should redirect back to the board
            // instead — see show_trip_detail. The trip page itself must
            // never appear anywhere in this flow.
            this._board_mode = true;
            this._do_stop_pickup_flow(seq);
        });
    }

    do_board_start_deliver(trip, name) {
        if (!trip || !name) return;
        frappe.dom.freeze(__("Loading…"));
        this._load_trip_detail_silently(trip, () => {
            frappe.dom.unfreeze();
            const m = ((this._trip_detail && this._trip_detail.manifests) || []).find((x) => x.name === name);
            const seq = m && m.drop_stop_sequence;
            if (!seq) {
                frappe.show_alert({ message: __("Could not find a drop stop for this manifest — open the trip to continue."), indicator: "orange" });
                return;
            }
            this._board_mode = true;
            this._do_stop_drop_flow(seq);
        });
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
        if (!name) return;
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
                <div class="da-se-route">${window.ch_wh_label_html ? ch_wh_label_html(t.from_warehouse, "") : frappe.utils.escape_html(t.from_warehouse || "")} → ${window.ch_wh_label_html ? ch_wh_label_html(t.to_warehouse, "") : frappe.utils.escape_html(t.to_warehouse || "")}</div>`;
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
            // A driver on Break is off the clock — Reached Location / Complete
            // Delivery are blocked until they end the break (server enforces
            // the same gate; this just avoids a round-trip error).
            let on_break = this.driver_status === "Break";
            let break_hint = on_break
                ? `<div class="text-muted" style="font-size:11px;margin-top:4px;">${__("You're on Break — end it to continue.")}</div>`
                : "";
            if (!d.arrival_datetime) {
                action_html = `<button id="da-arrived-btn" class="btn btn-primary btn-lg btn-block da-action-btn" ${on_break ? `disabled title="${__("End your break to continue")}"` : ""}>
                    <i class="fa fa-map-marker"></i> ${__("Reached Location")}
                </button>
                <button id="da-deliver-btn" class="btn btn-success btn-lg btn-block da-action-btn da-btn-disabled" disabled
                        style="opacity:0.45;cursor:not-allowed;"
                        title="${__("Tap Reached Location first")}">
                    <i class="fa fa-lock"></i> ${__("Complete Delivery")}
                </button>
                <button id="da-reject-btn" class="btn btn-danger btn-sm btn-block da-action-btn">
                    <i class="fa fa-exclamation-triangle"></i> ${__("Failed Delivery (mid-trip)")}
                </button>
                ${break_hint}`;
            } else {
                let arrived_at = frappe.datetime.str_to_user(d.arrival_datetime);
                action_html = `<div class="alert alert-info da-arrival-banner" style="padding:8px 12px;border-radius:6px;margin-bottom:8px;font-size:13px;">
                    <i class="fa fa-map-marker"></i> ${__("Arrived at destination")}: ${frappe.utils.escape_html(arrived_at)}
                </div>
                <button id="da-deliver-btn" class="btn btn-success btn-lg btn-block da-action-btn" ${on_break ? `disabled title="${__("End your break to continue")}"` : ""}>
                    <i class="fa fa-check-circle"></i> ${__("Complete Delivery")}
                </button>
                <button id="da-reject-btn" class="btn btn-danger btn-sm btn-block da-action-btn">
                    <i class="fa fa-exclamation-triangle"></i> ${__("Failed Delivery (mid-trip)")}
                </button>
                ${break_hint}`;
            }
        } else if (d.status === "Received") {
            action_html = `<button id="da-manifest-close-btn" class="btn btn-success btn-lg btn-block da-action-btn">
                <i class="fa fa-archive"></i> ${__("Close Manifest")}
            </button>`;
        } else if (d.status === "Delivered") {
            // Delivered = goods handed over, driver's job done. Closing is the
            // destination store's step (Scan & Receive posts the stock), so
            // the driver app must not offer a Close that would skip it.
            action_html = `<div class="da-awaiting-receipt text-muted" style="text-align:center;padding:12px;">
                <i class="fa fa-clock-o"></i> ${__("Delivered — awaiting destination-store receipt (Scan & Receive). The trip closes automatically once every shipment is received.")}
            </div>`;
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
                        <div class="da-route-wh">${d.source_store
                            ? frappe.utils.escape_html(d.source_store)
                            : (window.ch_wh_label_html ? ch_wh_label_html(d.source_warehouse, "—") : frappe.utils.escape_html(d.source_warehouse || "—"))}</div>
                        ${d.source_address ? `<div class="da-route-addr">${frappe.utils.escape_html(d.source_address)}</div>` : ""}
                    </div>
                    <div class="da-route-arrow"><i class="fa fa-long-arrow-right fa-2x"></i></div>
                    <div class="da-route-box da-to">
                        <div class="da-route-label">${__("To")}</div>
                        <div class="da-route-wh">${d.destination_store
                            ? frappe.utils.escape_html(d.destination_store)
                            : (window.ch_wh_label_html ? ch_wh_label_html(d.destination_warehouse, "—") : frappe.utils.escape_html(d.destination_warehouse || "—"))}</div>
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
                    options: "Barcode",
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
                this._capture_gps((lat, lng, accuracy) => {
                    // Via _call_promise so a geofence refusal offers the
                    // reasoned override here too, not only in the stop flows.
                    this._call_promise(API + "start_pickup", {
                            manifest: this.active_manifest,
                            pickup_photo: values.pickup_photo,
                            scanned_qr: values.scanned_qr,
                            lat, lng,
                            gps_accuracy_m: accuracy,
                            notes: values.notes,
                    }).then(
                        (message) => {
                            const result = message || {};
                            if (result.ok === false) {
                                d.show();
                                frappe.msgprint({
                                    title: __("OTP Verification Failed"),
                                    indicator: "red",
                                    message: result.message || __("Invalid delivery OTP."),
                                });
                                return;
                            }
                            frappe.show_alert({
                                message: __("Pickup confirmed!"),
                                indicator: "green",
                            });
                            this.show_manifest_detail(this.active_manifest);
                            this.load_data();
                        },
                        () => {},
                    );
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
        if (this.driver_status === "Break") {
            frappe.show_alert({ message: __("You're on Break — end it to continue."), indicator: "orange" });
            return;
        }
        frappe.dom.freeze(__("Capturing GPS…"));
        this._capture_gps((lat, lng, accuracy) => {
            frappe.dom.unfreeze();
            this._show_arrival_dialog(lat, lng, accuracy);
        });
    }

    _show_arrival_dialog(lat, lng, accuracy) {
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
                // Via _call_promise so a geofence refusal offers the reasoned
                // override here too.
                this._call_promise(API + "mark_reached_destination", {
                        manifest: this.active_manifest,
                        lat: lat, lng: lng,
                        gps_accuracy_m: accuracy,
                }).then(
                    () => {
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
                    () => frappe.dom.unfreeze(),
                );
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
        if (this.driver_status === "Break") {
            frappe.show_alert({ message: __("You're on Break — end it to continue."), indicator: "orange" });
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
        const seals = ((this._detail || {}).packed_seals || []).filter(Boolean);
        let d = new frappe.ui.Dialog({
            title: __("Complete Delivery"),
            fields: [
                { fieldname: "recipients_info", fieldtype: "HTML", options: recipients_html || "" },
                {
                    fieldname: "scanned_qr",
                    fieldtype: "Data",
                    options: "Barcode",
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
                // Chain of custody. Only shown when the cartons were actually
                // sealed at packing — the server treats an unsealed manifest as
                // nothing to verify, so an empty field here is never a blocker.
                ...(seals.length ? [{
                    fieldname: "seal_numbers",
                    fieldtype: "Small Text",
                    label: __("Seal / Tamper Tag Numbers"),
                    reqd: 1,
                    default: "",
                    description: __("Confirm the tag(s) on the carton(s): {0}. A mismatch blocks delivery — report it as tampered instead.", [seals.join(", ")]),
                }] : []),
            ],
            primary_action_label: __("Confirm Delivery"),
            primary_action: (values) => {
                d.hide();
                this._capture_gps((lat, lng, accuracy) => {
                    // Via _call_promise so a geofence refusal offers the
                    // reasoned override here too.
                    this._call_promise(API + "complete_delivery", {
                            manifest: this.active_manifest,
                            delivery_photo: values.delivery_photo,
                            receiver_name: values.receiver_name,
                            scanned_qr: values.scanned_qr,
                            otp: values.otp,
                            seal_numbers: values.seal_numbers,
                            lat, lng,
                            gps_accuracy_m: accuracy,
                    }).then(() => {
                        frappe.show_alert({
                            message: __("Delivery completed!"),
                            indicator: "green",
                        });
                        this.show_manifest_detail(this.active_manifest);
                        this.load_data();
                    }, () => {});
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

    do_reject(opts = {}) {
        // Status-aware rejection: carrier-grade ERPs (Delhivery / BlueDart /
        // Ekart / FedEx / Oracle TMS) use different reason codes for pickup
        // failure versus mid-trip delivery failure. We mirror that split
        // so dispatch can route the recovery action correctly.
        // opts.from_trip: invoked from a trip-detail manifest chip — the
        // manifest is resolved from the trip payload and the trip view is
        // refreshed afterwards instead of the manifest view.
        let manifest = (this.manifests || []).find(m => m.name === this.active_manifest)
                       || ((this._trip_detail && this._trip_detail.manifests) || [])
                              .find(m => m.name === this.active_manifest)
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
                    label: __("Proof Photo 1"),
                    reqd: 1,
                },
                {
                    fieldname: "rejection_photo_2",
                    fieldtype: "Attach Image",
                    label: __("Proof Photo 2"),
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
                        rejection_photo_2: values.rejection_photo_2,
                        rejection_notes: values.rejection_notes,
                    },
                    callback: () => {
                        frappe.show_alert({
                            message: in_transit
                                ? __("Failed delivery logged. Dispatch notified; goods will be returned to source.")
                                : __("Manifest rejected. Dispatcher notified."),
                            indicator: "orange",
                        });
                        if (opts.from_trip && this.active_trip) {
                            this.show_trip_detail(this.active_trip);
                        } else {
                            this.show_manifest_detail(this.active_manifest);
                        }
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
                    label: __("Proof Photo 1 (shared)"),
                    reqd: 1,
                },
                {
                    fieldname: "rejection_photo_2",
                    fieldtype: "Attach Image",
                    label: __("Proof Photo 2 (shared)"),
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
                        rejection_photo_2: values.rejection_photo_2,
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
                // The trip detail view's Accept/Complete buttons are
                // disabled based on this.driver_status (set inside
                // render_status) — re-render whatever's on screen, after the
                // status refresh lands, so they un-disable the moment the
                // break ends without needing a manual back-and-forth.
                this.load_status(() => this.render_content());
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
            // accuracy is passed through so the server can widen the geofence
            // by the fix's own margin of error instead of trusting it blindly.
            (pos) => callback(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy),
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

    show_trip_detail(name, after) {
        // A board-triggered pickup/drop action finishes by calling this the
        // same bare way the trip page's own buttons do — "refresh the
        // current trip view" — but the driver never actually left the
        // board (see _board_mode / _load_trip_detail_silently), so redirect
        // that refresh back to the board instead of surfacing the trip
        // page they were never shown. Anything that passes its own `after`
        // callback is an explicit, deliberate navigation and always wins.
        if (this._board_mode && !after) {
            this._board_mode = false;
            this.active_trip = null;
            this.load_data();
            return;
        }
        this._board_mode = false;
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
                if (typeof after === "function") after();
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
                    label: s.store
                        || (window.ch_wh_label ? ch_wh_label(s.warehouse) : s.warehouse)
                        || "Stop",
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
            // stop_roles is the server's authoritative {sequence: stop_type}
            // map (ch_logistics.api.stop_roles). Preferring it keeps the card
            // list and the stop actions driven by ONE predicate — the client
            // used to re-derive membership and could show a manifest the
            // server would then refuse to act on.
            const roles = m.stop_roles;
            if (roles && Object.keys(roles).length) {
                for (let seq of Object.keys(roles)) _push_stop_manifest(seq, m);
                continue;
            }
            const pickup_seq = m.pickup_stop_sequence;
            const drop_seq = m.drop_stop_sequence || m.stop_sequence;
            _push_stop_manifest(pickup_seq, m);
            if (drop_seq && drop_seq !== pickup_seq) _push_stop_manifest(drop_seq, m);
            if (!pickup_seq && !drop_seq) _push_stop_manifest(m.stop_sequence || 0, m);
        }

        // A route's stop list is a template — on any given trip, some stops
        // may carry no manifest at all (nothing to pick up or drop there
        // today). Showing those as an actionable "Pending — Arrive" card
        // just gives the driver a dead-end tap: nothing is deliverable, so
        // it silently falls back to a bare GPS ping with no photo/OTP
        // prompt. Hide a stop once it's confirmed empty; a stop that
        // already has status beyond Pending stays visible (it may show
        // completed proof/history worth keeping on screen).
        let visible_stops = (t.stops || []).filter(
            (s) => s.status !== "Pending" || (manifests_by_stop[s.sequence] || []).length > 0
        );

        // A driver on Break is off the clock — accept/start/complete are
        // blocked until they end the break (Reject stays available so they
        // aren't stuck holding an assignment they can't act on). Computed
        // here (rather than after stops_html, where this used to live) so
        // every card's Accept button can honour it — it's the only accept
        // action left now that the bottom "Accept & Start Trip" button is
        // gone, so it needs the same gate that button had, on the Started
        // view too (a still-unaccepted sibling manifest keeps offering
        // Accept/Reject there — see _render_manifest_route_card).
        let on_break = this.driver_status === "Break";

        // Show every manifest as its own Pickup-location -> Drop-location
        // card, keyed to that manifest's own resolved pickup/drop stop —
        // not grouped by stop. A route that revisits the same warehouse (or
        // a reverse-direction pair of manifests between the same two
        // stores) used to repeat the identical manifest chip across every
        // stop card whose location matched, which read as duplicated data
        // even though the underlying stop_roles were all individually
        // correct. One card per manifest removes that regardless of how
        // many other manifests share its stops.
        let per_stop_html = (t.manifests || [])
            .map((m) => {
                const pickup_seq = m.pickup_stop_sequence;
                const drop_seq = m.drop_stop_sequence;
                const pickup_stop = pickup_seq != null
                    ? visible_stops.find((s) => cint(s.sequence) === cint(pickup_seq)) : null;
                const drop_stop = drop_seq != null
                    ? visible_stops.find((s) => cint(s.sequence) === cint(drop_seq)) : null;
                const seq_key = cint(pickup_seq != null ? pickup_seq : (drop_seq != null ? drop_seq : 0));
                return { seq_key, html: this._render_manifest_route_card(m, pickup_stop, drop_stop, t, on_break) };
            })
            .sort((a, b) => a.seq_key - b.seq_key)
            .map((it) => it.html)
            .join("");

        // Pre-start trips get a combined Pickup+Drop-per-manifest card
        // instead of the per-stop layout above — the same manifest
        // otherwise shows up twice (once under its pickup stop, once under
        // its drop stop) before the driver has even accepted anything.
        // Once the trip is Started, the richer per-stop Arrive/Complete
        // workflow above takes back over.
        let stops_html = t.status === "Assigned"
            ? ((t.manifests || []).map((m) => this._render_assigned_manifest_card(m, t, on_break)).join("")
                || `<div class="da-stop-empty text-muted" style="font-size:12px;padding:8px;">${__("No shipments assigned to this trip yet.")}</div>`)
            : per_stop_html;

        // Trip-level action buttons
        let break_attrs = on_break
            ? `disabled title="${__("End your break to continue")}"`
            : "";
        let action_html = "";
        if (t.status === "Assigned") {
            // Accepting is now per-shipment (the manifest cards above) — the
            // first shipment a driver accepts already starts the trip
            // (driver_accept_manifest). A separate "Accept & Start Trip"
            // button here would just duplicate that in a way that skips
            // per-shipment triage. Reject Trip stays: it hands the whole
            // route back to dispatch in one lightweight step, which
            // per-manifest reject (the audited 2-photo damage-report flow)
            // isn't meant for and doesn't replace.
            action_html += `<button id="da-trip-reject-btn" class="btn btn-danger btn-sm btn-block da-action-btn"><i class="fa fa-ban"></i> ${__("Reject Trip")}</button>`;
            if (on_break) {
                action_html += `<div class="text-muted" style="font-size:11px;margin-top:4px;">${__("You're on Break — end it to accept shipments on this trip.")}</div>`;
            }
        } else if (t.status === "Started") {
            let all_done = (t.stops || []).every((s) => s.status === "Completed" || s.status === "Skipped");
            action_html += `<button id="da-trip-complete-btn" class="btn btn-success btn-lg btn-block da-action-btn" ${(all_done && !on_break) ? "" : "disabled"} ${on_break ? `title="${__("End your break to continue")}"` : ""}><i class="fa fa-flag-checkered"></i> ${__("Complete Trip")}</button>`;
            if (on_break) {
                action_html += `<div class="text-muted" style="font-size:11px;margin-top:4px;">${__("You're on Break — end it to complete this trip.")}</div>`;
            }
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
        // A Pickup+Drop stop is BOTH a pickup and a drop, so it counts in each
        // bucket. Testing for equality excluded it from both, which reported
        // "0 PICKUPS" on a trip whose hub stop was the only collection point.
        // Live workflow counts, not route structure: how many manifests
        // still need picking up vs. how many belong to the drop side of the
        // trip. Pickups is "still needs pickup" (leaves once picked up).
        // Drops is NOT the pickup-side mirror of that — it's "has been
        // picked up", so it keeps counting a manifest all the way through
        // delivery rather than dropping it the instant it's actually
        // Delivered. Matches _show_shipment_breakdown's own "drop" filter
        // (DROP_DONE_STATUSES below) so the tile and the dialog it opens
        // never disagree on what's shown.
        const pickup_count = (t.manifests || []).filter((m) => ["Assigned", "Pickup Started"].includes(m.status)).length;
        const drop_count = (t.manifests || []).filter((m) => DeliveryApp.DROP_STAGE_STATUSES.includes(m.status)).length;
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
                    <div class="da-stat da-stat-clickable" data-shipment-role="pickup" style="cursor:pointer;"><span class="da-stat-val">${pickup_count}</span><span class="da-stat-label">${__("Pickups")}</span></div>
                    <div class="da-stat da-stat-clickable" data-shipment-role="drop" style="cursor:pointer;"><span class="da-stat-val">${drop_count}</span><span class="da-stat-label">${__("Drops")}</span></div>
                    <div class="da-stat da-stat-clickable" data-shipment-role="all" style="cursor:pointer;"><span class="da-stat-val" style="font-size:20px;">${frappe.utils.escape_html(t.status)}</span><span class="da-stat-label">${__("Shipments")}</span></div>
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

        // Use the Leaflet copy shipped with Frappe so production maps do not
        // depend on a third-party CDN or its availability/security policy.
        const css_url = "/assets/frappe/js/lib/leaflet/leaflet.css";
        const js_url = "/assets/frappe/js/lib/leaflet/leaflet.js";

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

    _describe_empty_stops(seqs) {
        // Empty-stop warnings default to bare sequence numbers ("#2, #3")
        // which mean nothing to a driver mid-route. Resolve each to the
        // same store/warehouse label already shown on its stop card.
        const t = this._trip_detail || {};
        return (seqs || []).map((seq) => {
            const stop = (t.stops || []).find((s) => cint(s.sequence) === cint(seq));
            if (!stop) return "#" + seq;
            const label = stop.store
                ? stop.store
                : (window.ch_wh_label_html ? ch_wh_label_html(stop.warehouse, "") : (stop.warehouse || ""));
            return frappe.utils.escape_html(label || ("#" + seq));
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
        //
        // Pre-flight the server's empty-stop warning ONCE up front, whichever
        // path (quick-accept or the QR-scan gate below) ends up firing —
        // both call driver_accept_trip and would otherwise hit the same
        // throw. One confirm here instead of a failed call + retry.
        this._call_promise(TRIP_API + "get_empty_stop_warning", {
            trip: this.active_trip,
        }).then((info) => {
            const empty = (info && info.empty_stops) || [];
            if (!empty.length) {
                this._do_trip_accept_proceed(0);
                return;
            }
            frappe.confirm(
                __("Stop(s) {0} have no shipments assigned — nothing to load or unload there. Start anyway?",
                    [this._describe_empty_stops(empty).join(", ")]),
                () => this._do_trip_accept_proceed(1)
            );
        });
    }

    _do_trip_accept_proceed(overrideEmptyStops) {
        const t = this._trip_detail || {};
        // Find the first stop (lowest sequence) manifests are actually
        // AWAITING PICKUP at, driven by each manifest's own server-resolved
        // pickup_stop_sequence (get_trip_detail / _annotate_manifest_stop_links,
        // which matches on real source-location geography and is correct
        // regardless of trip/manifest Direction). Matching on bare stop_type
        // instead ("first stop typed Pickup or Pickup+Drop") breaks for a
        // Reverse trip (goods flow store → hub): stop #1 is the hub, which
        // is this manifest's DROP point, not its pickup point — using it as
        // the pickup gate ran start_pickup + stop_complete against the wrong
        // stop, permanently marking it "Completed" before delivery ever
        // happened and hiding its real "Arrive & Deliver" action.
        const awaiting_pickup = (t.manifests || []).filter(
            (m) => m.status === "Assigned" && m.pickup_stop_sequence
        );
        let first_pickup = null;
        let source_manifests = [];
        if (awaiting_pickup.length) {
            const first_seq = Math.min(...awaiting_pickup.map((m) => cint(m.pickup_stop_sequence)));
            first_pickup = (t.stops || []).find((s) => cint(s.sequence) === first_seq);
            source_manifests = awaiting_pickup.filter((m) => cint(m.pickup_stop_sequence) === first_seq);
        }

        if (!first_pickup || !source_manifests.length) {
            // Nothing to scan at the source (delivery-only trip, or all
            // manifests at the source already picked up). Fall back to the
            // legacy quick-accept flow so the driver isn't blocked.
            this._do_trip_accept_quick(overrideEmptyStops);
            return;
        }
        // Capture GPS up front (not after the dialog is confirmed) so the
        // driver can see — and the load photo / QR scan is provably tied to
        // — the location actually captured, mirroring the Reached Location
        // dialog's lat/lng display.
        frappe.dom.freeze(__("Capturing GPS…"));
        this._capture_gps_promise()
            .then(({ lat, lng }) => {
                frappe.dom.unfreeze();
                this._open_trip_start_dialog(first_pickup, source_manifests, { lat, lng }, overrideEmptyStops);
            })
            .catch(() => frappe.dom.unfreeze());
    }

    _do_trip_accept_quick(overrideEmptyStops) {
        frappe.confirm(__("Accept this trip and start now?"), () => {
            this._call_promise(TRIP_API + "driver_accept_trip", {
                trip: this.active_trip,
                override_empty_stops: overrideEmptyStops ? 1 : 0,
            }).then(() => {
                frappe.show_alert({ message: __("Trip accepted"), indicator: "green" });
                this.show_trip_detail(this.active_trip);
                this.load_data();
            });
        });
    }

    do_manifest_accept(name) {
        // Lightweight per-manifest acknowledgement — no photo/QR yet, that
        // still happens at Start Pickup (_do_stop_pickup_flow). This just
        // unlocks that button for THIS manifest and, on the first accept of
        // the trip, flips it to Started so pickup actions are allowed.
        if (!name) return;
        const t = this._trip_detail || {};
        if (t.status === "Assigned") {
            // First accept on this trip is what triggers mark_started() —
            // same empty-stop gate driver_accept_trip already pre-flights,
            // otherwise this call would hit the server's hard throw instead
            // of a dismissible warning. Started trips skip this: the gate
            // only fires on the Assigned → Started transition.
            this._call_promise(TRIP_API + "get_empty_stop_warning", {
                trip: this.active_trip,
            }).then((info) => {
                const empty = (info && info.empty_stops) || [];
                if (!empty.length) {
                    this._submit_manifest_accept(name, 0);
                    return;
                }
                frappe.confirm(
                    __("Stop(s) {0} have no shipments assigned — nothing to load or unload there. Start anyway?",
                        [this._describe_empty_stops(empty).join(", ")]),
                    () => this._submit_manifest_accept(name, 1)
                );
            });
            return;
        }
        this._submit_manifest_accept(name, 0);
    }

    _submit_manifest_accept(name, overrideEmptyStops) {
        frappe.dom.freeze(__("Accepting…"));
        this._call_promise(TRIP_API + "driver_accept_manifest", {
            trip: this.active_trip,
            manifest: name,
            override_empty_stops: overrideEmptyStops ? 1 : 0,
        }).then(() => {
            frappe.dom.unfreeze();
            frappe.show_alert({ message: __("Manifest accepted"), indicator: "green" });
            this.show_trip_detail(this.active_trip);
            this.load_data();
        }).catch(() => frappe.dom.unfreeze());
    }

    _stop_manifests_html(manifests) {
        // Shared manifest-chip renderer (name, status, Close/Reject) used
        // by both the standalone per-stop card and the merged pickup/drop
        // card — identical markup either way.
        const proof_line = (m) => {
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
        return (manifests || []).map((m) => `
            <div class="da-stop-manifest da-stop-manifest-link"
                 data-name="${frappe.utils.escape_html(m.name)}">
                <span><i class="fa fa-file-text-o"></i> ${frappe.utils.escape_html(m.name)}</span>
                <span class="da-card-status da-status-${(m.status || "").toLowerCase().replace(/\s+/g, "-")}">${frappe.utils.escape_html(m.status)}</span>
                ${m.status === "Received"
                    ? `<button class="btn btn-xs btn-success da-stop-manifest-close-btn" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-archive"></i> ${__("Close")}</button>`
                    : (m.status === "Delivered"
                        ? `<span class="da-awaiting-pill text-muted" style="font-size:11px;" title="${__("The destination store must Scan & Receive this shipment")}"><i class="fa fa-clock-o"></i> ${__("Awaiting receipt")}</span>`
                        : "")}
                ${["Assigned", "Pickup Started", "In Transit"].includes(m.status)
                    ? `<button class="btn btn-xs btn-danger da-stop-manifest-reject-btn" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-ban"></i> ${__("Reject")}</button>`
                    : ""}
            </div>${proof_line(m)}`).join("");
    }

    _stop_action_bits(s, manifests, t, forceDisabledReason, roleFilter) {
        // Arrive/Complete button + hint for ONE stop — factored out of the
        // per-stop card so the merged pickup/drop card (below) can render
        // the same status/GPS/Complete behaviour twice, once per leg,
        // without duplicating the underlying logic.
        // forceDisabledReason: when set, renders the leg's action button(s)
        // visible-but-inert with this as the tooltip — used by the merged
        // card to sequence Pick Up before Deliver (same "visible, click does
        // nothing" pattern used for the Control Tower Start/Complete gate).
        // roleFilter ("pickup" | "drop"): on a stop that's is_combined
        // because it serves a DIFFERENT manifest's opposite role (e.g. this
        // manifest is dropped here while another is picked up here), only
        // render the button for the role this specific leg represents — a
        // leg labelled "Drop" must never surface another manifest's Pick Up
        // button just because they happen to share the stop.
        const forceDisabledAttr = forceDisabledReason
            ? `disabled title="${frappe.utils.escape_html(forceDisabledReason)}"`
            : "";
        let active_manifest_rows = (manifests || []).filter((m) => ["Assigned", "Pickup Started", "In Transit"].includes(m.status));
        let can_arrive = (t.status === "Started" && s.status === "Pending");
        let can_complete = (t.status === "Started" && s.status === "Arrived");
        const stop_open_rows = can_complete
            ? this._gather_stop_manifests(s.sequence, ["Assigned", "Pickup Started", "In Transit"], null)
            : [];
        const complete_blocked = stop_open_rows.length > 0;
        const st_type = (s.stop_type || "").toLowerCase();
        const st_roles = st_type.split("+");
        const is_combined = st_roles.includes("pickup") && st_roles.includes("drop");
        let arrive_label = __("Arrive");
        let arrive_icon = "fa-location-arrow";
        if (active_manifest_rows.length && !is_combined) {
            if (st_roles.includes("pickup")) {
                arrive_label = __("Arrive & Pick Up");
                arrive_icon = "fa-camera";
            } else if (st_roles.includes("drop")) {
                arrive_label = __("Arrive & Deliver");
                arrive_icon = "fa-check-circle";
            }
        }
        let combined_pickup_pending = 0;
        let combined_deliver_pending = 0;
        if (is_combined) {
            if (roleFilter !== "drop") {
                combined_pickup_pending = this._gather_stop_manifests(s.sequence, ["Assigned"], "pickup", { require_accepted: true }).length;
            }
            if (roleFilter !== "pickup") {
                combined_deliver_pending = this._gather_stop_manifests(s.sequence, ["In Transit"], "drop").length;
            }
        }
        let completion_hint = "";
        if (can_arrive && is_combined && !roleFilter && active_manifest_rows.length) {
            completion_hint = `<div class="text-muted" style="font-size:11px;margin-bottom:6px;">
                ${__("Deliver captures a photo, receiver name + per-manifest OTP/QR. Pick Up captures a photo + QR scan. Do either first — the other stays available.")}
            </div>`;
        } else if (can_arrive && active_manifest_rows.length) {
            const verb = (roleFilter || st_type) === "pickup"
                ? __("photo + QR scan for each manifest")
                : __("delivery photo, receiver name + per-manifest OTP/QR");
            completion_hint = `<div class="text-muted" style="font-size:11px;margin-bottom:6px;">
                ${__("One tap arrives, captures {0}, and closes the stop.", [verb])}
            </div>`;
        } else if (can_complete && active_manifest_rows.length) {
            completion_hint = `<div class="text-muted" style="font-size:11px;margin-bottom:6px;">
                ${__("Open the manifest below to finish OTP/photo, then tap Complete.")}
            </div>`;
        }
        const actions_html = `
            ${is_combined && t.status === "Started" ? `
                ${combined_deliver_pending ? `<button class="btn btn-primary btn-sm da-stop-deliver-btn" data-seq="${s.sequence}" ${forceDisabledAttr}><i class="fa fa-check-circle"></i> ${__("Deliver")} (${combined_deliver_pending})</button>` : ""}
                ${combined_pickup_pending ? `<button class="btn btn-primary btn-sm da-stop-pickup-btn" data-seq="${s.sequence}" ${forceDisabledAttr}><i class="fa fa-camera"></i> ${__("Pick Up")} (${combined_pickup_pending})</button>` : ""}
            ` : (can_arrive ? `<button class="btn btn-primary btn-sm da-stop-arrive-btn" data-seq="${s.sequence}" ${forceDisabledAttr}><i class="fa ${arrive_icon}"></i> ${arrive_label}</button>` : "")}
            ${can_complete ? `<button class="btn btn-success btn-sm da-stop-complete-btn" data-seq="${s.sequence}"
                    ${complete_blocked ? `disabled title="${__("Finish open manifest(s) first: {0}", [stop_open_rows.map((r) => r.name).join(", ")])}"` : forceDisabledAttr}>
                    <i class="fa fa-check"></i> ${__("Complete")}</button>` : ""}`;
        return { completion_hint, actions_html };
    }

    _render_stop_card(s, manifests_by_stop, t) {
        let st_cls = (s.status || "").toLowerCase().replace(/\s+/g, "-");
        let manifests = manifests_by_stop[s.sequence] || [];
        let manifests_html = this._stop_manifests_html(manifests);
        const { completion_hint, actions_html } = this._stop_action_bits(s, manifests, t);

        return `
            <div class="da-stop-card">
                <div class="da-stop-header">
                    <span class="da-stop-seq">#${s.sequence}</span>
                    <span class="da-stop-type">${frappe.utils.escape_html(s.stop_type)}</span>
                    <span class="da-card-status da-status-${st_cls}">${frappe.utils.escape_html(s.status)}</span>
                </div>
                <div class="da-stop-where">
                    <i class="fa fa-map-marker"></i>
                    ${s.store
                        ? frappe.utils.escape_html(s.store)
                        : (window.ch_wh_label_html ? ch_wh_label_html(s.warehouse, "—") : frappe.utils.escape_html(s.warehouse || "—"))}
                </div>
                ${completion_hint}
                ${manifests_html
                    ? `<div class="da-stop-manifests">${manifests_html}</div>`
                    : `<div class="da-stop-empty text-muted" style="font-size:11px;padding:4px 8px 6px;">
                         <i class="fa fa-info-circle"></i>
                         ${__("No shipments assigned to this stop — nothing to load or unload here.")}
                       </div>`}
                <div class="da-stop-actions">${actions_html}</div>
            </div>`;
    }

    _render_manifest_route_card(m, pickup_stop, drop_stop, t, on_break) {
        // One card per manifest: Pickup-location -> Drop-location, always —
        // regardless of whether other manifests share either stop (a route
        // revisiting the same warehouse, or a reverse-direction pair of
        // manifests between the same two stores, otherwise repeated the
        // identical chip across every matching stop card). Each leg still
        // drives its own stop's Arrive/Complete/GPS via _stop_action_bits —
        // only the grouping changed, not the underlying stop behaviour, so
        // a stop shared by several manifests just gets the same Complete
        // button reachable from each of their cards.
        const label = (stop) => !stop ? "—" : (stop.store
            ? frappe.utils.escape_html(stop.store)
            : (window.ch_wh_label_html ? ch_wh_label_html(stop.warehouse, "—") : frappe.utils.escape_html(stop.warehouse || "—")));

        // The trip starts the moment its FIRST manifest is accepted, but any
        // sibling manifest still awaiting its own accept decision must not
        // go silent once that happens — the pre-start ("Assigned" trip)
        // screen that normally shows Accept/Reject is gone the instant the
        // trip flips to Started, so this card has to keep offering it
        // itself for as long as the manifest hasn't been individually
        // accepted, regardless of what the trip as a whole is doing.
        if (m.status === "Assigned" && !m.driver_accepted_at) {
            const accept_disabled = on_break
                ? `disabled title="${__("End your break to accept shipments")}"`
                : "";
            return `
                <div class="da-stop-card da-manifest-merged-card">
                    <div class="da-stop-where">
                        <i class="fa fa-map-marker"></i> ${label(pickup_stop)}
                        <i class="fa fa-long-arrow-right" style="margin:0 4px;"></i>
                        ${label(drop_stop)}
                    </div>
                    <div class="da-stop-manifests">${this._stop_manifests_html([m])}</div>
                    <div class="da-stop-actions">
                        <button class="btn btn-success btn-sm da-manifest-accept-btn" data-name="${frappe.utils.escape_html(m.name)}" ${accept_disabled}><i class="fa fa-check"></i> ${__("Accept Shipment")}</button>
                        <button class="btn btn-danger btn-sm da-stop-manifest-reject-btn" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-ban"></i> ${__("Reject")}</button>
                    </div>
                </div>`;
        }

        // The leg badge must reflect THIS manifest's own progress at that
        // role, not the shared stop's raw status — a stop can already be
        // "Completed" purely because a DIFFERENT manifest sharing it
        // finished there (e.g. this manifest's drop point is another
        // manifest's pickup point), which made an untouched manifest's leg
        // misleadingly show "Completed" while it was still sitting at
        // status Assigned with no pickup photo at all.
        const drop_done_statuses = ["Delivered", "Received", "Partially Received", "Closed"];
        const leg = (title, stop, bits, status_label) => stop ? `
            <div class="da-merged-leg" style="margin-top:8px;padding-top:8px;border-top:1px dashed #e5e5e5;">
                <div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;margin-bottom:4px;">
                    ${title} &middot; <span class="da-card-status da-status-${status_label.toLowerCase().replace(/\s+/g, "-")}">${frappe.utils.escape_html(status_label)}</span>
                </div>
                ${bits.completion_hint}
                <div class="da-stop-actions">${bits.actions_html}</div>
            </div>` : "";

        const pickup_done = m.status !== "Assigned";
        const drop_done = drop_done_statuses.includes(m.status);

        let legs_html;
        if (pickup_stop && drop_stop && String(pickup_stop.sequence) === String(drop_stop.sequence)) {
            // Same physical stop covers both roles for this manifest — one
            // combined leg using that stop's own Pickup+Drop handling
            // (_stop_action_bits' is_combined branch) instead of two
            // artificially split sections pointing at the same place.
            const bits = this._stop_action_bits(pickup_stop, [m], t);
            const status_label = drop_done ? __("Completed") : (pickup_done ? __("In Transit") : __("Pending"));
            legs_html = leg(__("Pickup + Drop"), pickup_stop, bits, status_label);
        } else {
            // Physical sequencing: goods can't be delivered before they've
            // been picked up. "Pickup done" is read off the manifest's own
            // status (the same signal _do_stop_drop_flow already requires —
            // In Transit — before treating a manifest as deliverable).
            // roleFilter keeps a stop this manifest shares with a DIFFERENT
            // manifest's opposite role (e.g. this manifest drops where
            // another one is picked up) from leaking that other manifest's
            // button into this card's leg.
            const pickup_bits = pickup_stop
                ? this._stop_action_bits(pickup_stop, [m], t, pickup_done ? __("Already picked up") : null, "pickup")
                : null;
            const drop_bits = drop_stop
                ? this._stop_action_bits(drop_stop, [m], t, pickup_done ? null : __("Complete pickup first"), "drop")
                : null;
            legs_html = (pickup_bits ? leg(__("Pickup"), pickup_stop, pickup_bits, pickup_done ? __("Completed") : __("Pending")) : "")
                + (drop_bits ? leg(__("Drop"), drop_stop, drop_bits, drop_done ? __("Completed") : __("Pending")) : "");
        }

        return `
            <div class="da-stop-card da-manifest-merged-card">
                <div class="da-stop-where">
                    <i class="fa fa-map-marker"></i> ${label(pickup_stop)}
                    <i class="fa fa-long-arrow-right" style="margin:0 4px;"></i>
                    ${label(drop_stop)}
                </div>
                <div class="da-stop-manifests">${this._stop_manifests_html([m])}</div>
                ${legs_html}
            </div>`;
    }

    _render_assigned_manifest_card(m, t, on_break) {
        // Pre-start ("Assigned" trip) manifest card: one row per manifest
        // regardless of how many stops it touches, since the driver hasn't
        // accepted anything yet and per-stop cards would show the same
        // manifest twice (once under its pickup stop, once under its drop
        // stop) with no way to act on it as a single shipment.
        const roles = m.stop_roles || {};
        const seq_with_role = (want) => Object.keys(roles).find(
            (seq) => (roles[seq] || "").toLowerCase().split("+").includes(want)
        );
        const pickup_seq = seq_with_role("pickup") || m.pickup_stop_sequence || null;
        const drop_seq = seq_with_role("drop") || m.drop_stop_sequence || m.stop_sequence || null;

        const store_label = (seq, store_field, wh_field) => {
            const stop = seq ? (t.stops || []).find((s) => cint(s.sequence) === cint(seq)) : null;
            if (stop && stop.store) return frappe.utils.escape_html(stop.store);
            if (m[store_field]) return frappe.utils.escape_html(m[store_field]);
            return window.ch_wh_label_html
                ? ch_wh_label_html(m[wh_field], "—")
                : frappe.utils.escape_html(m[wh_field] || "—");
        };
        const from_label = store_label(pickup_seq, "source_store", "source_warehouse");
        const to_label = store_label(drop_seq, "destination_store", "destination_warehouse");

        const st_cls = (m.status || "").toLowerCase().replace(/\s+/g, "-");
        const accepted = !!m.driver_accepted_at;
        const pending_decision = m.status === "Assigned" && !accepted;
        const ready_for_pickup = m.status === "Assigned" && accepted;

        let actions = "";
        if (pending_decision) {
            const accept_disabled = on_break
                ? `disabled title="${__("End your break to accept shipments")}"`
                : "";
            actions = `
                <button class="btn btn-success btn-sm da-manifest-accept-btn" data-name="${frappe.utils.escape_html(m.name)}" ${accept_disabled}><i class="fa fa-check"></i> ${__("Accept Shipment")}</button>
                <button class="btn btn-danger btn-sm da-stop-manifest-reject-btn" data-name="${frappe.utils.escape_html(m.name)}"><i class="fa fa-ban"></i> ${__("Reject")}</button>`;
        } else if (ready_for_pickup && pickup_seq) {
            actions = `<button class="btn btn-primary btn-sm da-stop-pickup-btn" data-seq="${pickup_seq}"><i class="fa fa-camera"></i> ${__("Start Pickup")}</button>`;
        }

        return `
            <div class="da-stop-card da-assigned-manifest-card">
                <div class="da-stop-header">
                    <span class="da-card-status da-status-${st_cls}">${frappe.utils.escape_html(m.status)}</span>
                </div>
                <div class="da-stop-where">
                    <i class="fa fa-map-marker"></i> ${from_label}
                    <i class="fa fa-long-arrow-right" style="margin:0 4px;"></i>
                    ${to_label}
                </div>
                <div class="text-muted" style="font-size:11px;padding:0 8px 6px;">
                    <i class="fa fa-file-text-o"></i> ${frappe.utils.escape_html(m.name)}
                </div>
                <div class="da-stop-actions">${actions}</div>
            </div>`;
    }

    _open_trip_start_dialog(pickup_stop, source_manifests, gps, overrideEmptyStops) {
        const t = this._trip_detail || {};
        const manifest_rows_html = source_manifests.map((m) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(m.name)}</div>
                <div class="text-muted" style="font-size:11px;">${m.destination_store
                    ? frappe.utils.escape_html(m.destination_store)
                    : (window.ch_wh_label_html ? ch_wh_label_html(m.destination_warehouse, "—") : frappe.utils.escape_html(m.destination_warehouse || "—"))}</div>
            </div>`).join("");

        const qr_fields = source_manifests.map((m) => ({
            fieldname: `qr__${m.name.replace(/[^A-Za-z0-9_]/g, "_")}`,
            fieldtype: "Data",
            options: "Barcode",
            label: __("Scan QR for {0}", [m.name]),
            reqd: 1,
        }));

        const lat_str = (typeof gps.lat === "number") ? gps.lat.toFixed(6) : String(gps.lat);
        const lng_str = (typeof gps.lng === "number") ? gps.lng.toFixed(6) : String(gps.lng);
        const maps_url = `https://maps.google.com/?q=${lat_str},${lng_str}`;

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
                fieldname: "pickup_lat", fieldtype: "Data",
                label: __("Latitude"), default: lat_str, read_only: 1,
            },
            {
                fieldname: "pickup_lng", fieldtype: "Data",
                label: __("Longitude"), default: lng_str, read_only: 1,
            },
            {
                fieldname: "gps_preview", fieldtype: "HTML",
                options: `<div style="text-align:center;margin:0 0 8px 0;">
                    <a href="${maps_url}" target="_blank" rel="noopener" class="btn btn-default btn-xs">
                       <i class="fa fa-external-link"></i> ${__("Open in Google Maps")}
                    </a>
                </div>`,
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
                this._submit_trip_start(pickup_stop, source_manifests, values, gps, overrideEmptyStops);
            },
        });
        d.show();
    }

    _submit_trip_start(pickup_stop, source_manifests, values, gps, overrideEmptyStops) {
        frappe.dom.freeze(__("Accepting trip & loading manifests…"));
        let captured_gps = gps || null;
        // GPS is already captured (and shown to the driver) before this
        // dialog opens — don't ask the device for a second fix.
        return (gps ? Promise.resolve(gps) : this._capture_gps_promise())
            .then(({ lat, lng, accuracy }) => {
                captured_gps = { lat, lng, accuracy };
                // Step 1: flip Assigned → Started so subsequent stop_arrive /
                // stop_complete calls are allowed by the server.
                return this._call_promise(TRIP_API + "driver_accept_trip", {
                    trip: this.active_trip,
                    override_empty_stops: overrideEmptyStops ? 1 : 0,
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
                        gps_accuracy_m: captured_gps.accuracy,
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
        } else if (stop_type === "pickup+drop") {
            // A combined stop used to fall through to the plain GPS ping
            // below, leaving the driver unable to either deliver or collect at
            // a hub that is both. Oracle OTM sequences a combined stop as
            // unload-then-load, so deliver what is on board first and let the
            // stop stay open for the pickup leg (see _stop_open_counterpart,
            // which stops one leg from closing the stop on the other).
            const deliverable = this._gather_stop_manifests(seq, ["In Transit"], "drop");
            if (deliverable.length) {
                this._do_stop_drop_flow(seq);
            } else {
                this._do_stop_pickup_flow(seq);
            }
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
        // m.stop_sequence only ever points at a manifest's DESTINATION stop
        // (see get_trip_detail), so on a Pickup+Drop stop this used to miss
        // manifests being picked up here whose destination is a later stop —
        // letting the stop complete while a pickup was still open there.
        // _gather_stop_manifests (role=null → any role) checks stop_roles
        // instead, so it correctly covers both legs.
        if (this._trip_detail && (this._trip_detail.manifests || []).length) {
            const open_rows = this._gather_stop_manifests(
                seq, ["Assigned", "Pickup Started", "In Transit"], null
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

    // ── Pickups / Drops summary breakdown ────────────────────────
    // The Pickups/Drops tiles only ever showed a bare stop count, with no
    // way to see what's actually inside without opening each manifest one
    // at a time. Clicking either tile now lists every currently-relevant
    // manifest's source, destination, and item lines in one place.

    _show_shipment_breakdown(role) {
        const t = this._trip_detail || {};
        let manifests;
        if (role === "all") {
            // Every shipment on the trip, any status — the overall manifest
            // (not filtered to what's currently actionable, unlike the
            // Pickups/Drops tiles).
            manifests = (t.manifests || []).slice();
        } else {
            const pending_statuses = role === "drop" ? DeliveryApp.DROP_STAGE_STATUSES : ["Assigned", "Pickup Started"];
            manifests = (t.manifests || []).filter((m) => pending_statuses.includes(m.status));
        }

        if (!manifests.length) {
            frappe.show_alert({
                message: role === "all"
                    ? __("No shipments on this trip")
                    : (role === "drop"
                        ? __("No shipments have reached the drop side of this trip yet")
                        : __("Nothing currently awaiting pickup on this trip")),
                indicator: "blue",
            });
            return;
        }

        frappe.call({
            method: "ch_logistics.api.logistics_api.get_manifest_items_bulk",
            args: { manifests: manifests.map((m) => m.name) },
            freeze: true,
            callback: (r) => {
                const items_by_manifest = r.message || {};
                const esc = (s) => frappe.utils.escape_html(s || "");
                const loc = (wh, store) => esc(store || wh || "—");

                const rows = manifests.map((m) => {
                    const items = items_by_manifest[m.name] || [];
                    const item_rows = items.length
                        ? items.map((it) => `
                            <tr>
                                <td>${esc(it.item_name || it.item_code)}</td>
                                <td class="text-center">${flt(it.qty)} ${esc(it.uom || "")}</td>
                            </tr>`).join("")
                        : `<tr><td colspan="2" class="text-muted">${__("No items found")}</td></tr>`;
                    const status_badge = role === "all"
                        ? `<span class="da-card-status da-status-${(m.status || "").toLowerCase().replace(/\s+/g, "-")}" style="margin-left:8px;">${esc(m.status)}</span>`
                        : "";
                    return `
                        <div class="da-shipment-breakdown-group" style="margin-bottom:12px;">
                            <div style="font-weight:600;margin-bottom:2px;">
                                <i class="fa fa-file-text-o"></i> ${esc(m.name)}${status_badge}
                            </div>
                            <div class="text-muted" style="font-size:12px;margin-bottom:4px;">
                                ${loc(m.source_warehouse, m.source_store)}
                                <i class="fa fa-arrow-right" style="margin:0 4px;"></i>
                                ${loc(m.destination_warehouse, m.destination_store)}
                            </div>
                            <table class="table table-bordered" style="margin-bottom:0;">
                                <thead><tr><th>${__("Item")}</th><th class="text-center" style="width:110px;">${__("Qty")}</th></tr></thead>
                                <tbody>${item_rows}</tbody>
                            </table>
                        </div>`;
                }).join("");

                const title = role === "all"
                    ? __("All Shipments — Source, Destination & Items")
                    : (role === "drop" ? __("Drops — Source, Destination & Items") : __("Pickups — Source, Destination & Items"));
                const dialog = new frappe.ui.Dialog({
                    title,
                    size: "large",
                    fields: [{ fieldtype: "HTML", fieldname: "breakdown_html", options: rows }],
                });
                dialog.show();
            },
        });
    }

    // ── Stop-level combined flows ────────────────────────────────

    _find_stop(seq) {
        const t = this._trip_detail || {};
        return (t.stops || []).find((s) => cint(s.sequence) === cint(seq));
    }

    _stop_open_counterpart(seq, role) {
        // Open work at this stop for the OTHER leg. At a Pickup+Drop stop each
        // flow auto-completes the stop when its own manifests all succeed;
        // without this the first leg to finish would close the stop and strand
        // the second (deliver at the hub, then be unable to collect there).
        const other = (role || "").toLowerCase() === "pickup" ? "drop" : "pickup";
        const statuses = other === "drop"
            ? ["In Transit"]                      // on board, awaiting delivery here
            : ["Assigned", "Pickup Started"];     // waiting to be collected here
        return this._gather_stop_manifests(seq, statuses, other).length;
    }

    _gather_stop_manifests(seq, statuses, role, opts) {
        // ``role`` ("pickup" | "drop" | null) is the caller's INTENT, not the
        // stop's declared type. At a Pickup+Drop stop the two differ: the
        // pickup flow must collect only shipments originating here, and the
        // drop flow only those terminating here. Inferring intent from the
        // stop type meant a combined hub stop fed every manifest into the
        // pickup flow, including ones merely being delivered there.
        //
        // Membership itself comes from the server's stop_roles map
        // (ch_logistics.api.stop_roles), the same predicate the stop actions
        // use, so the card list and the action can no longer disagree. The
        // location checks below are the pre-stop_roles fallback.
        const t = this._trip_detail || {};
        const stop = (t.stops || []).find((s) => cint(s.sequence) === cint(seq));
        const stop_type = ((stop && stop.stop_type) || "").toLowerCase();
        const allowed = new Set((statuses || []).map((x) => x.toLowerCase()));
        const want = (role || "").toLowerCase();
        const rows = (t.manifests || []).filter((m) => {
            let matches = false;
            // A manifest with a stop_roles map is one the server has fully
            // annotated — trust it completely, INCLUDING when it has no
            // entry for this particular seq (that means "definitively not
            // relevant here", not "fall through to the legacy fallback").
            // Falling through in that case let a stale/unrelated flat
            // stop_sequence value (which only ever tracks one side of a
            // manifest) wrongly match a manifest to a stop stop_roles says
            // it has nothing to do with — e.g. a manifest whose real stops
            // are #6 and #3 got pulled into stop #5's drop list purely
            // because its stop_sequence field happened to equal 5.
            const has_stop_roles = m.stop_roles && Object.keys(m.stop_roles).length > 0;
            if (has_stop_roles) {
                const srv = m.stop_roles[String(seq)];
                if (!srv) return false;
                const have = srv.toLowerCase().split("+");
                matches = want ? have.includes(want) : true;
                if (!matches) return false;
                if (!allowed.size) return true;
                return allowed.has((m.status || "").toLowerCase());
            }
            const try_pickup = want ? want === "pickup"
                : (stop_type === "pickup" || stop_type === "pickup+drop");
            const try_drop = want ? want === "drop"
                : (stop_type === "drop" || stop_type === "pickup+drop");
            if (try_pickup) {
                if (m.pickup_stop_sequence && cint(m.pickup_stop_sequence) === cint(seq)) matches = true;
                if (!matches && stop && stop.warehouse && m.source_warehouse === stop.warehouse) matches = true;
                if (!matches && stop && stop.store && m.source_store === stop.store) matches = true;
            }
            if (!matches && try_drop) {
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
        // Per-manifest accept (driver_accept_manifest / whole-trip accept)
        // is independent of a manifest's stop_roles/status match above — a
        // manifest can be status "Assigned" and geographically at this stop
        // while still awaiting the driver's individual accept decision.
        // Callers that actually MOVE goods (pickup flow) opt into this so
        // an accepted manifest's bundle scan can't sweep in an undecided
        // sibling sitting at the same stop.
        if (opts && opts.require_accepted) {
            // Only enforce this when the trip is actually using per-manifest
            // acceptance (at least one manifest on it has driver_accepted_at
            // set). A trip started through some other, older/unknown path
            // that never stamps it would otherwise have every manifest
            // silently excluded here — dropping straight to a bare
            // GPS-arrival fallback with no pickup dialog at all, for every
            // stop, with no visible error. Fail open instead: no signal
            // that acceptance is tracked here means don't gate on it.
            const trip_manifests = t.manifests || [];
            const acceptance_tracked = trip_manifests.some((mm) => !!mm.driver_accepted_at);
            if (acceptance_tracked) {
                return rows.filter((m) => !!m.driver_accepted_at);
            }
        }
        return rows;
    }

    _is_geofence_error(payload) {
        // The server raises a dedicated GeofenceError so this check does not
        // depend on a translated message string.
        const exc = (payload && (payload.exc_type || payload.exc)) || "";
        return String(exc).indexOf("GeofenceError") !== -1;
    }

    _prompt_geofence_override() {
        // Market-standard escape hatch: a driver whose device reports a poor
        // fix (common inside a warehouse) must not be stranded mid-route. The
        // reason is mandatory and lands on the manifest and its trip, so the
        // override is auditable rather than silent.
        return new Promise((resolve) => {
            const d = new frappe.ui.Dialog({
                title: __("Confirm You Are At This Location"),
                fields: [
                    {
                        fieldtype: "HTML",
                        options: `<div class="alert alert-warning" style="padding:8px 10px;border-radius:6px;">
                            ${__("Your device's location does not match this stop. If you ARE at the right place, say why — this is recorded against the shipment.")}
                        </div>`,
                    },
                    {
                        fieldname: "reason",
                        fieldtype: "Small Text",
                        label: __("Reason"),
                        reqd: 1,
                        description: __("e.g. weak GPS indoors, store entrance is around the back, address pin is wrong."),
                    },
                ],
                primary_action_label: __("Confirm & Continue"),
                primary_action: (v) => {
                    const reason = (v.reason || "").trim();
                    if (reason.length < 10) {
                        frappe.msgprint(__("Please give a bit more detail (at least 10 characters)."));
                        return;
                    }
                    d.hide();
                    resolve(reason);
                },
                secondary_action_label: __("Cancel"),
                secondary_action: () => { d.hide(); resolve(null); },
            });
            d.onhide = () => resolve(null);
            d.show();
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
                callback: (r) => {
                    const result = r && r.message;
                    if (result && result.ok === false) {
                        frappe.msgprint({
                            title: __("OTP Verification Failed"),
                            indicator: "red",
                            message: result.message || __("Invalid delivery OTP."),
                        });
                        reject(result);
                        return;
                    }
                    resolve(result);
                },
                error: (err) => {
                    // A geofence refusal is recoverable: offer the reasoned
                    // override and retry once. Anything else propagates as
                    // before. The retry carries the reason so the server can
                    // record it — it never simply skips the check.
                    if (!args || args.geofence_override_reason || !this._is_geofence_error(err)) {
                        reject(err);
                        return;
                    }
                    this._prompt_geofence_override().then((reason) => {
                        if (!reason) {
                            reject(err);
                            return;
                        }
                        this._call_promise(method, Object.assign({}, args, {
                            geofence_override_reason: reason,
                        })).then(resolve, reject);
                    });
                },
            });
        });
    }

    _gps_display_fields(gps) {
        // Visible, read-only confirmation of the GPS fix already captured
        // for this dialog — same "Latitude / Longitude / Open in Google
        // Maps" pattern the old Accept & Start Trip dialog used. The drop
        // dialogs already captured gps upfront (needed it for the OTP
        // request) but never showed it; the pickup dialogs didn't capture
        // it until submit at all. Driver-visible confirmation matters here
        // more than it looks like it should — without it, "did this even
        // get my location" is not something the driver can tell.
        const lat_str = (gps && typeof gps.lat === "number") ? gps.lat.toFixed(6) : String((gps && gps.lat) || "");
        const lng_str = (gps && typeof gps.lng === "number") ? gps.lng.toFixed(6) : String((gps && gps.lng) || "");
        const maps_url = `https://maps.google.com/?q=${lat_str},${lng_str}`;
        return [
            { fieldname: "gps_lat_display", fieldtype: "Data", label: __("Latitude"), default: lat_str, read_only: 1 },
            { fieldname: "gps_lng_display", fieldtype: "Data", label: __("Longitude"), default: lng_str, read_only: 1 },
            {
                fieldname: "gps_preview", fieldtype: "HTML",
                options: `<div style="text-align:center;margin:0 0 8px 0;">
                    <a href="${maps_url}" target="_blank" rel="noopener" class="btn btn-default btn-xs">
                       <i class="fa fa-external-link"></i> ${__("Open in Google Maps")}
                    </a>
                </div>`,
            },
        ];
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
                (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy }),
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

    _record_stop_arrival(seq, gps) {
        // Optional gps: reuse a fix already captured (and shown to the
        // driver) rather than prompting the device for a second reading.
        return (gps ? Promise.resolve(gps) : this._capture_gps_promise()).then(({ lat, lng }) => {
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
        const candidates = this._gather_stop_manifests(seq, ["Assigned"], "pickup", { require_accepted: true });
        const stop = this._find_stop(seq) || {};
        if (!candidates.length) {
            // Nothing left to pick up here. If every manifest at this stop
            // was already picked up (manifest-level Start Pickup), say so
            // and close the stop out in one tap — the old silent "arrival
            // recorded" ping made drivers think the pickup photo / manifest
            // scan had been skipped.
            const here = this._gather_stop_manifests(seq, [], "pickup");
            const picked = here.filter((m) =>
                ["In Transit", "Delivered", "Received", "Partially Received", "Closed"].includes(m.status));
            if (here.length && picked.length === here.length
                && !this._stop_open_counterpart(seq, "pickup")) {
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

        // Capture GPS BEFORE opening the dialog so it can be shown to the
        // driver (same pattern as the bundle paths) instead of only being
        // silently captured — again — inside _record_stop_arrival on submit.
        frappe.dom.freeze(__("Capturing arrival…"));
        this._capture_gps_promise()
            .then((gps) => {
                frappe.dom.unfreeze();
                this._open_stop_pickup_dialog(seq, candidates, stop, gps);
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                console.error("stop pickup prepare failed", err);
            });
    }

    _open_stop_pickup_dialog(seq, candidates, stop, gps) {
        // Build per-manifest QR fields. One pickup photo (the goods on the
        // dock) is shared across the load — that mirrors Ekart / Delhivery
        // handover scans where one rack photo covers all bundles in the lot.
        const manifest_rows_html = candidates.map((m) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(m.name)}</div>
                <div class="text-muted" style="font-size:11px;">${m.destination_store
                    ? frappe.utils.escape_html(m.destination_store)
                    : (window.ch_wh_label_html ? ch_wh_label_html(m.destination_warehouse, "—") : frappe.utils.escape_html(m.destination_warehouse || "—"))}</div>
            </div>`).join("");

        const qr_fields = candidates.map((m) => ({
            fieldname: `qr__${m.name.replace(/[^A-Za-z0-9_]/g, "_")}`,
            fieldtype: "Data",
            options: "Barcode",
            label: __("QR for {0}", [m.name]),
            reqd: 1,
        }));

        const fields = [
            {
                fieldname: "summary", fieldtype: "HTML",
                options: `<div class="alert alert-info" style="padding:8px 10px;border-radius:6px;margin-bottom:8px;">
                    <strong><i class="fa fa-map-marker"></i> ${stop.store
                        ? frappe.utils.escape_html(stop.store)
                        : (window.ch_wh_label_html ? ch_wh_label_html(stop.warehouse, "—") : frappe.utils.escape_html(stop.warehouse || "—"))}</strong>
                    <div style="font-size:12px;">${__("Picking up {0} manifest(s) at this stop.", [candidates.length])}</div>
                </div>
                <div class="da-stop-batch-list">${manifest_rows_html}</div>`,
            },
            ...this._gps_display_fields(gps),
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
                this._submit_stop_pickup(seq, candidates, values, gps);
            },
        });
        d.show();
    }

    _submit_stop_pickup(seq, candidates, values, gps) {
        frappe.dom.freeze(__("Recording arrival & pickup…"));
        return this._record_stop_arrival(seq, gps)
            .then(({ lat, lng, accuracy }) => {
                return this._run_sequential(candidates, (m) => {
                    const qr_key = `qr__${m.name.replace(/[^A-Za-z0-9_]/g, "_")}`;
                    return this._call_promise(API + "start_pickup", {
                        manifest: m.name,
                        pickup_photo: values.pickup_photo,
                        scanned_qr: values[qr_key],
                        lat, lng,
                        gps_accuracy_m: accuracy,
                        notes: values.notes,
                    });
                });
            })
            .then((results) => {
                const { ok, fail } = this._summarise_batch(results);
                // If every manifest at this pickup stop succeeded, auto-mark
                // the stop Completed so the driver doesn't need a second tap —
                // unless the drop leg of a combined stop still has work.
                if (ok.length && !fail.length && !this._stop_open_counterpart(seq, "pickup")) {
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
        const deliverable = this._gather_stop_manifests(seq, ["In Transit"], "drop");
        const not_yet = this._gather_stop_manifests(seq, ["Assigned", "Pickup Started"], "drop");
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
                this._open_stop_delivery_dialog(seq, ready, skipped, stop, this._stop_drop_gps);
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                this.show_trip_detail(this.active_trip);
                console.error("stop drop prepare failed", err);
            });
    }

    _open_stop_delivery_dialog(seq, ready, skipped, stop, gps) {
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
                options: "Barcode",
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
                    <strong><i class="fa fa-map-marker"></i> ${stop.store
                        ? frappe.utils.escape_html(stop.store)
                        : (window.ch_wh_label_html ? ch_wh_label_html(stop.warehouse, "—") : frappe.utils.escape_html(stop.warehouse || "—"))}</strong>
                    <div style="font-size:12px;">${__("Delivering {0} manifest(s) at this stop.", [ready.length])}</div>
                </div>
                <div class="da-stop-batch-list">${ready_rows_html}</div>
                ${skipped_html}`,
            },
            ...this._gps_display_fields(gps),
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
                // resolve the stragglers individually — including the pickup
                // leg of a combined stop, which must not be closed out here.
                if (ok.length && !fail.length && !skipped.length
                    && !this._stop_open_counterpart(seq, "drop")) {
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
        // Capture GPS BEFORE opening the dialog (mirrors _do_stop_drop_bundle)
        // so it can be shown to the driver as a visible confirmation, same
        // as the fields do — rather than capturing silently on submit.
        frappe.dom.freeze(__("Capturing arrival…"));
        this._capture_gps_promise()
            .then((gps) => {
                frappe.dom.unfreeze();
                this._open_stop_pickup_bundle_dialog(seq, candidates, stop, gps);
            })
            .catch((err) => {
                frappe.dom.unfreeze();
                console.error("bundle pickup prepare failed", err);
            });
    }

    _open_stop_pickup_bundle_dialog(seq, candidates, stop, gps) {
        const manifest_rows_html = candidates.map((m) => `
            <div class="da-stop-batch-row" style="border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-weight:600;font-size:12px;">${frappe.utils.escape_html(m.name)}</div>
                <div class="text-muted" style="font-size:11px;">${m.destination_store
                    ? frappe.utils.escape_html(m.destination_store)
                    : (window.ch_wh_label_html ? ch_wh_label_html(m.destination_warehouse, "—") : frappe.utils.escape_html(m.destination_warehouse || "—"))}</div>
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
            ...this._gps_display_fields(gps),
            {
                fieldname: "scanned_qr",
                fieldtype: "Data",
                options: "Barcode",
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
                this._submit_stop_pickup_bundle(seq, candidates, values, gps);
            },
        });
        d.show();
    }

    _submit_stop_pickup_bundle(seq, candidates, values, gps) {
        frappe.dom.freeze(__("Recording arrival & cascading pickup…"));
        // Reuse the GPS fix already captured (and shown to the driver)
        // before the dialog opened rather than taking a fresh reading here.
        return (gps ? Promise.resolve(gps) : this._capture_gps_promise())
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
            ...this._gps_display_fields(gps),
            {
                fieldname: "scanned_qr",
                fieldtype: "Data",
                options: "Barcode",
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
