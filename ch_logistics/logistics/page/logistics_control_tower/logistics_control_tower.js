/**
 * Logistics Command Center
 * Merged from: Logistics Hub (analytics) + Logistics Control Tower (dispatch ops)
 *
 * Mode tabs:
 *   Overview   — KPI dashboard, AI insights, route analysis, driver scorecard
 *   Operations — Trip kanban board, exception inbox, drivers, GPS map
 *
 * Shared page-header filters (hub_filters): Company → City → Zone → Store + date range
 * Operations-specific filters: Trip Date + Days window (inline in content area)
 */

const _LCC = "ch_logistics.api.logistics_api.";

// Why a trip is called off before it leaves. Typed reasons came back as
// "driver", "not needed", "wrongly attached" and forty other spellings of
// the same handful of causes, which is unreadable as a set — nobody could
// say how often a trip dies because a manifest was hung on the wrong one.
// Same shape as CH Manifest Rejection's own reason list: a fixed list
// ending in Other, with the details typed beside it.
const LCC_TRIP_CANCEL_REASONS = [
    "Manifest Wrongly Attached",
    "Trip Created by Mistake",
    "Duplicate Trip",
    "Driver Unavailable",
    "Vehicle Unavailable / Breakdown",
    "Consignment Not Ready",
    "Wrong Route / Wrong Hub",
    "Destination Store Closed",
    "Rescheduled to Another Day",
    "Merged into Another Trip",
    "Other",
];

// What is written to the trip's cancellation_reason: the chosen cause, with
// whatever was typed beside it, so the audit line reads as one sentence.
function lcc_trip_cancel_reason(values) {
    const cause = (values.reason_code || "").trim();
    const notes = (values.reason_notes || "").trim();
    if (!cause) return "";
    return notes ? `${cause} — ${notes}` : cause;
}
const _OPT = "ch_logistics.api.optimizer.";
const _REJ = "ch_logistics.api.rejection_api.";
const _TMA = "ch_logistics.api.transfer_manifest_api.";

// Why a shipment comes off a manifest, and why a manifest is undone. Fixed
// lists for the same reason CH Manifest Rejection and the trip cancellation
// have them: free text came back as forty spellings of the same handful of
// causes, and nobody could count how often a shipment was simply hung on the
// wrong manifest. The same two lists are offered in the CH Transfer Manifest form.
const CH_MF_REMOVE_REASONS = [
	"Wrongly Attached to This Manifest",
	"Wrong Destination",
	"Goods Not Ready",
	"Damaged Package",
	"Moving to Another Trip",
	"Dispatch Deferred",
	"Other",
];
const CH_MF_CANCEL_REASONS = [
	"Manifest Created by Mistake",
	"Duplicate Manifest",
	"Wrong Destination",
	"Goods Not Ready",
	"Re-packing Required",
	"Dispatch Deferred",
	"Other",
];

// The chosen cause with whatever was typed beside it, so the audit line on
// the manifest and the Stock Entry reads as one sentence.
function ch_mf_reason_text(values) {
	const cause = (values.reason_code || "").trim();
	const notes = (values.reason_notes || "").trim();
	return notes ? `${cause} — ${notes}` : cause;
}

function ch_mf_reason_fields(options) {
	return [
		{
			fieldtype: "Select", fieldname: "reason_code", label: __("Reason"),
			options: options.join("\n"), reqd: 1,
		},
		{
			fieldtype: "Small Text", fieldname: "reason_notes", label: __("Details"),
			description: __("Anything the next person needs to know. Required when the reason is Other."),
			mandatory_depends_on: "eval:doc.reason_code == 'Other'",
		},
	];
}


frappe.pages["logistics-control-tower"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Logistics Command Center"),
		single_column: true,
	});
	wrapper.lcc = new LogisticsCommandCenter(page, wrapper);
};

frappe.pages["logistics-control-tower"].refresh = function (wrapper) {
	if (wrapper.lcc) wrapper.lcc.refresh();
};

// A manifest can carry legs to several stores, each delivered and signed for
// at its own warehouse (driver_complete_delivery_row). The header names only
// the first, so the rest are read from the rows and shown beside it —
// otherwise a three-stop manifest looks like a one-stop one to whoever is
// planning the trip.
// Plain-text twin of lcc_destinations_html, for the overview tables. Every
// drop is named in the order the manifest carries them, so the route reads as
// the run the driver actually makes rather than a count of hidden stops.
function lcc_destinations_text(r) {
	const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh);
	const list = (r.destinations || []).filter(Boolean);
	if (!list.length) return r.destination_store || r.destination_warehouse || "";
	if (list.length === 1) return r.destination_store || label(list[0]);
	return list.map(label).join(" → ");
}

function lcc_destinations_html(m) {
	const label = (wh) => (window.ch_wh_label_html
		? ch_wh_label_html(wh, "—") : frappe.utils.escape_html(wh || "—"));
	const list = (m.destinations || []).filter(Boolean);
	if (!list.length) return label(m.destination_warehouse);
	return list.map(label).join(" → ");
}

class LogisticsCommandCenter {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;

		// shared state
		this.mode = "draft";
		this.filters = null;		// hub_filters handle
		this._auto_timer = null;

		// ops state
		this.trip_date = frappe.datetime.get_today();
		this.include_days = 1;
		this.bottom_tab = "manifests";
		this.board = { buckets: {}, totals: {} };
		this.unassigned = [];
		this.drivers = [];
		this.recalls = [];
		// Shipments a driver has not accepted yet — see ops_pickup_pending.
		this.pickup_pending = [];
		this.selected_manifests = new Set();
		this.active_trip = null;
		this._leaflet_loading = null;
		this._map = null;
		this.map_open = false;

		// Server-resolved role capabilities (CH Logistics Settings → Role
		// Matrix). Cosmetic UI gating only — every endpoint re-checks.
		this._capabilities = {};
		frappe.xcall("ch_logistics.roles.get_my_capabilities")
			.then((caps) => { this._capabilities = caps || {}; })
			.catch(() => {});

		this._boot();
	}

	// ─────────────────────────────────────────────────────────────
	// BOOT
	// ─────────────────────────────────────────────────────────────

	_boot() {
		// Page-level actions
		this.page.set_primary_action(__("New Trip"), () => this._dlg_new_trip(), "add");
		this.page.add_menu_item(__("Auto-plan Trips"), () => this._dlg_auto_plan());
		this.page.add_button(__("Refresh"), () => this.refresh(), { icon: "refresh" });

		// Shared hub_filters (header bar): company, city, zone, store, dates
		// No period until the user picks one: until then every tab shows
		// everything. Once picked, the header dates narrow every tab, and each
		// tab's own date box narrows that tab only.
		this.filters = ch_erp15.hub_filters.attach(this.page, {
			include_dates: true,
			blank_dates: true,
			on_change: () => this.refresh(),
		});

		// Root shell
		this.$root = $('<div class="lcc-root"></div>').appendTo(this.page.body);
		this._render_shell();
		// A full browser refresh recreates this page object from scratch,
		// so without remembering the tab, it always landed back on
		// Overview even if you'd been sitting in Operations. Restore
		// whichever tab was last open instead.
		// The Overview tab is no longer offered; a browser that last had it
		// open starts on Transfer Request Approved instead.
		let last_mode = "draft";
		try {
			last_mode = localStorage.getItem("lcc_last_mode") || "draft";
		} catch (e) { /* localStorage unavailable — fall back to Overview */ }
		this._switch_mode(["draft", "pending_with_goods", "packing", "mf_pickup", "mf_trip", "mf_transit", "mf_pickedup", "mf_delivered", "ops"].includes(last_mode) ? last_mode : "draft");
	}

	// ─────────────────────────────────────────────────────────────
	// SHELL
	// ─────────────────────────────────────────────────────────────

	_render_shell() {
		this.$root.html(`
			<div class="lcc-modebar">
				<div class="lcc-modebar-tabs">
					<button class="lcc-mode-btn" data-mode="draft">
						<i class="fa fa-file-text-o"></i> ${__("Transfer Request Approved")}
					</button>
					<button class="lcc-mode-btn" data-mode="pending_with_goods">
						<i class="fa fa-clock-o"></i> ${__("Pending With Goods")}
					</button>
					<button class="lcc-mode-btn" data-mode="packing">
						<i class="fa fa-cube"></i> ${__("Packed Order")}
					</button>
					<button class="lcc-mode-btn" data-mode="mf_pickup">
					<i class="fa fa-inbox"></i> ${__("Manifest")}
					<span class="lcc-tab-badge" id="lcc-reg-cnt-pickup"></span>
				</button>
				<button class="lcc-mode-btn" data-mode="mf_trip">
					<i class="fa fa-road"></i> ${__("Trip")}
					<span class="lcc-tab-badge" id="lcc-reg-cnt-trip"></span>
				</button>
				<button class="lcc-mode-btn" data-mode="mf_transit">
					<i class="fa fa-truck"></i> ${__("Pickup Pending")}
					<span class="lcc-tab-badge" id="lcc-reg-cnt-transit"></span>
				</button>
				<button class="lcc-mode-btn" data-mode="mf_pickedup">
					<i class="fa fa-check-square-o"></i> ${__("Picked Up")}
					<span class="lcc-tab-badge" id="lcc-reg-cnt-pickedup"></span>
				</button>
				<button class="lcc-mode-btn" data-mode="mf_delivered">
					<i class="fa fa-check-circle"></i> ${__("Delivered")}
					<span class="lcc-tab-badge" id="lcc-reg-cnt-delivered"></span>
				</button>
				<button class="lcc-mode-btn" data-mode="ops">
						<i class="fa fa-sitemap"></i> ${__("Operations")}
					</button>
				</div>
				<span class="lcc-live-badge" id="lcc-live-badge" style="display:none">
					<span class="lcc-pulse-dot"></span> ${__("Live · 60s")}
				</span>
			</div>
			<div class="lcc-content" id="lcc-content"></div>
			<div class="lcc-side" id="lcc-side"></div>
		`);

		this.$root.on("click", ".lcc-mode-btn", (e) => {
			this._switch_mode($(e.currentTarget).data("mode"));
		});
		this._bind_trip_drill();
	}

	_switch_mode(mode) {
		this.mode = mode;
		try { localStorage.setItem("lcc_last_mode", mode); } catch (e) { /* ignore */ }
		this.$root.find(".lcc-mode-btn").removeClass("active");
		this.$root.find(`.lcc-mode-btn[data-mode="${mode}"]`).addClass("active");

		// The trip side panel (Start/Assign Driver/Cancel/etc.) is only ever
		// meant to be live in Operations — switching modes used to leave it
		// open and fully clickable underneath Overview's read-only analytics,
		// since only #lcc-content got swapped, not #lcc-side.
		if (mode !== "ops") {
			this._ops_close_side();
		}

		if (mode === "overview") {
			$("#lcc-live-badge").show();
			this._ov_init();
		} else if (mode === "draft" || mode === "pending_with_goods") {
			$("#lcc-live-badge").hide();
			this._stop_auto_refresh();
			this._draft_pending_init(mode === "draft" ? "Draft" : "Pending With Goods");
		} else if (mode === "packing") {
			$("#lcc-live-badge").hide();
			this._stop_auto_refresh();
			this._pack_init();
		} else if (mode === "mf_trip") {
			$("#lcc-live-badge").hide();
			this._stop_auto_refresh();
			this._trips_init();
		} else if (["mf_pickup", "mf_transit", "mf_pickedup", "mf_delivered"].includes(mode)) {
			$("#lcc-live-badge").hide();
			this._stop_auto_refresh();
			this.reg_sub = { mf_pickup: "pickup", mf_transit: "transit", mf_pickedup: "pickedup",
				mf_delivered: "delivered" }[mode];
			this._reg_show_sub();
		} else {
			$("#lcc-live-badge").show();
			this._ops_init();
			this._start_auto_refresh();
		}
	}

	refresh() {
		if (this.mode === "overview") this._ov_load();
		else if (this.mode === "draft" || this.mode === "pending_with_goods") this._draft_pending_load();
		else if (this.mode === "packing") this._pack_load();
		else if (this.mode === "mf_trip") this._trips_load();
		else if (String(this.mode).startsWith("mf_")) this._reg_show_sub();
		else this._ops_load();
	}

	// ─────────────────────────────────────────────────────────────
	// AUTO-REFRESH (Overview only)
	// ─────────────────────────────────────────────────────────────

	_start_auto_refresh() {
		this._stop_auto_refresh();
		this._auto_timer = setInterval(() => {
			if (document.hidden) return;
			if (this.mode === "overview") this._ov_load(true);
			else if (this.mode === "ops") this._ops_load();
		}, 60000);
	}

	_stop_auto_refresh() {
		if (this._auto_timer) { clearInterval(this._auto_timer); this._auto_timer = null; }
	}

	// ══════════════════════════════════════════════════════════════
	// OVERVIEW MODE
	// ══════════════════════════════════════════════════════════════

	_ov_init() {
		$("#lcc-content").html(`
			<div class="lcc-ov" id="lcc-ov">
				<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading Overview…")}</div>
			</div>
		`);
		this._ov_bind_events($("#lcc-ov"));
		this._start_auto_refresh();
		this._ov_load();
	}

	// Delegated handlers bound ONCE per #lcc-ov element (recreated each
	// _ov_init). Must NOT live in the render path — _ov_render fires every
	// 60s via auto-refresh and only empties #lcc-ov, so per-render .on()
	// calls stack a duplicate handler every minute.
	_ov_bind_events($ov) {
		$ov.on("click", ".lcc-action-btn", (e) => {
			const act = $(e.currentTarget).data("act");
			const fn = {
				new_manifest:    () => frappe.new_doc("CH Transfer Manifest"),
				new_transfer:    () => frappe.new_doc("Stock Entry", { stock_entry_type: "Material Transfer" }),
				new_trip:        () => this._dlg_new_trip(),
				ops_view:        () => this._switch_mode("ops"),
				manifest_list:   () => this._go_list("CH Transfer Manifest"),
				stock_entry_list:() => this._go_list("Stock Entry", { stock_entry_type: "Material Transfer" }),
				delivery_app:    () => frappe.set_route("delivery-app"),
				buy_spares:      () => frappe.new_doc("Purchase Order"),
			}[act];
			if (fn) fn();
		});

		$ov.on("click", ".lcc-tab", function () {
			const tab = $(this).data("tab");
			$(this).siblings(".lcc-tab").removeClass("active");
			$(this).addClass("active");
			$(this).closest(".lcc-section").find(".lcc-tab-panel").removeClass("active");
			$(this).closest(".lcc-section").find(`[data-panel="${tab}"]`).addClass("active");
		});
	}

	_ov_load(silent = false) {
		const $ov = $("#lcc-ov");
		if (this._ov_refreshing) return;
		this._ov_refreshing = true;
		if (!silent || !$ov.children().length) {
			$ov.html(`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading Overview…")}</div>`);
		} else {
			$(`<div class="hub-refresh-indicator"><i class="fa fa-spinner fa-spin"></i> ${__("Updating...")}</div>`).appendTo($ov);
		}
		frappe.xcall("ch_erp15.ch_erp15.hub_api.get_logistics_hub_data", this.filters.values())
			.then((data) => this._ov_render(data))
			.catch(() => {
				if (!silent) $ov.html(`<div class="lcc-error-banner"><i class="fa fa-exclamation-circle"></i> ${__("Failed to load overview data.")}</div>`);
				else frappe.show_alert({ message: __("Update failed. Existing data has been kept."), indicator: "orange" }, 5);
			}).finally(() => {
				this._ov_refreshing = false;
				$ov.find(".hub-refresh-indicator").remove();
			});
	}

	_ov_render(data) {
		const $ov = $("#lcc-ov").empty();
		this._ov_alerts($ov, data);
		this._ov_pipeline($ov, data.pipeline || []);
		this._ov_kpis($ov, data.kpis || []);
		this._ov_intelligence($ov, data);
		this._ov_planning($ov, data.planning_suggestions || []);
		this._ov_route_lanes($ov, data.route_lanes || []);
		this._ov_driver_scorecard($ov, data.driver_scorecard || []);
		this._ov_quick_actions($ov);
		this._ov_manifest_tables($ov, data);
	}

	/* ── Alert banners ────────────────────────────────────────── */

	_ov_alerts($ov, data) {
		const kmap = {};
		(data.kpis || []).forEach((k) => (kmap[k.key] = k.value));
		const overdue = parseInt(kmap.overdue || 0);
		const pending = parseInt(kmap.pending_pickup || 0);
		const rows = [];

		if (overdue > 0) {
			rows.push(`
				<div class="lcc-alert is-critical">
					<div class="lcc-alert-icon"><i class="fa fa-clock-o"></i></div>
					<div class="lcc-alert-body">
						<div class="lcc-alert-title">${overdue} ${__("manifest(s) overdue")}</div>
						<div class="lcc-alert-sub">${__("Past estimated delivery date — contact driver / logistics manager.")}</div>
					</div>
					<button class="lcc-alert-cta" data-go="overdue">
						${__("View Overdue")} <i class="fa fa-arrow-right"></i>
					</button>
				</div>`);
		}
		if (pending > 0) {
			rows.push(`
				<div class="lcc-alert is-warning">
					<div class="lcc-alert-icon"><i class="fa fa-truck"></i></div>
					<div class="lcc-alert-body">
						<div class="lcc-alert-title">${pending} ${__("manifest(s) awaiting pickup")}</div>
						<div class="lcc-alert-sub">${__("Driver assigned but not yet packed — assign in Operations board.")}</div>
					</div>
					<button class="lcc-alert-cta" data-mode="ops">
						${__("Open Operations")} <i class="fa fa-arrow-right"></i>
					</button>
				</div>`);
		}
		if (!rows.length) return;

		$ov.append(rows.join(""));
		$ov.find(".lcc-alert-cta").on("click", (e) => {
			const go = $(e.currentTarget).data("go");
			const sw = $(e.currentTarget).data("mode");
			if (sw === "ops") {
				this._switch_mode("ops");
			} else if (go) {
				this._ov_activate_tab(go);
				$ov.find(".lcc-tabs").first()[0]?.scrollIntoView({ behavior: "smooth" });
			}
		});
	}

	/* ── Pipeline ─────────────────────────────────────────────── */

	_ov_pipeline($ov, steps) {
		const arrow = `<div class="lcc-flow-sep">
			<svg width="22" height="18" viewBox="0 0 32 24" fill="none" stroke="currentColor"
				stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
				<path d="M4 12H24M18 6l6 6-6 6"/>
			</svg></div>`;

		const nodes = steps.map((s, i) => {
			const node = `<div class="lcc-flow-node" data-step="${s.key}">
				<div class="lcc-flow-badge" style="background:${s.color}">${s.count}</div>
				<div class="lcc-flow-label">
					<i class="fa fa-${s.icon}"></i>
					<span>${__(s.label)}</span>
				</div>
				<div class="lcc-flow-sub">${s.sub || ""}</div>
			</div>`;
			return i < steps.length - 1 ? node + arrow : node;
		}).join("");

		$ov.append(`
			<div class="lcc-section">
				<h5 class="lcc-section-title"><i class="fa fa-random"></i> ${__("Delivery Pipeline")}</h5>
				<div class="lcc-flow-wrap"><div class="lcc-flow">${nodes}</div></div>
			</div>`);

		$ov.find(".lcc-flow-node").on("click", (e) => {
			const step = $(e.currentTarget).data("step");
			const sm = { draft:"Draft", assigned:"Assigned", packed:"Packed", transit:"In Transit", delivered:"Delivered", closed:"Closed" };
			if (sm[step]) this._go_list("CH Transfer Manifest", { status: sm[step] });
		});
	}

	/* ── KPI cards ────────────────────────────────────────────── */

	_ov_kpis($ov, kpis) {
		const cards = kpis.map((k) => {
			const val = typeof k.value === "string" ? k.value
				: k.key === "avg_hours" ? `${k.value}h` : k.value;
			return `<div class="lcc-kpi-card" style="--kc:${k.color}" data-kpi="${k.key}">
				<div class="lcc-kpi-val">${val}</div>
				<div class="lcc-kpi-lbl">${__(k.label)}</div>
			</div>`;
		}).join("");

		$ov.append(`
			<div class="lcc-section">
				<h5 class="lcc-section-title"><i class="fa fa-tachometer"></i> ${__("Key Metrics")}</h5>
				<div class="lcc-kpi-grid">${cards}</div>
			</div>`);

		$ov.find(".lcc-kpi-card").on("click", (e) => {
			const k = $(e.currentTarget).data("kpi");
			const map = {
				active:         ["CH Transfer Manifest", { status: ["not in", ["Closed","Cancelled"]] }],
				in_transit:     ["CH Transfer Manifest", { status: "In Transit" }],
				pending_pickup: ["CH Transfer Manifest", { status: ["in", ["Assigned","Draft"]] }],
				rejected:       ["CH Transfer Manifest", { status: "Rejected" }],
				delivered_today:["CH Transfer Manifest", { status: ["in", ["Delivered","Closed"]] }],
				overdue:        ["CH Transfer Manifest", { status: ["not in", ["Delivered","Closed","Cancelled"]] }],
				damage:         ["CH Transfer Manifest", { damage_reported: 1 }],
			};
			if (map[k]) this._go_list(map[k][0], map[k][1]);
		});
	}

	/* ── AI Insights + Financial Control ──────────────────────── */

	_ov_intelligence($ov, data) {
		const insights = data.ai_insights || [];
		const fc = data.financial_control || {};
		const sev_clr = { High: "#ef4444", Medium: "#f59e0b", Info: "#3b82f6" };

		const insight_html = insights.length
			? insights.map((i) => `
				<div class="lcc-insight-card" style="border-left-color:${sev_clr[i.severity]||"#6b7280"}">
					<div class="lcc-insight-hdr">
						<i class="fa fa-${i.icon}"></i>
						<span class="lcc-badge lcc-badge-${i.severity==="High"?"red":i.severity==="Medium"?"yellow":"blue"}">${__(i.severity)}</span>
					</div>
					<div class="lcc-insight-title">${i.title}</div>
					<div class="lcc-insight-detail">${i.detail}</div>
				</div>`).join("")
			: `<div class="lcc-empty">${__("No insights available.")}</div>`;

		const mom_arrow = fc.mom_change > 0 ? "↑" : fc.mom_change < 0 ? "↓" : "→";
		const mom_cls = fc.mom_change > 0 ? "lcc-text-green" : fc.mom_change < 0 ? "lcc-text-red" : "";

		const fc_items = [
			["#f59e0b", fc.total_manifests_mtd || 0,               "Manifests MTD"],
			["#22c55e", fc.delivered_mtd || 0,                      "Delivered MTD"],
			["#0ea5e9", parseInt(fc.qty_moved_mtd || 0),            "Qty Moved MTD"],
			["#6366f1", `${fc.avg_transit_hours || 0}h`,            "Avg Transit"],
			["#22c55e", `${fc.otd_pct || 0}%`,                      "On-Time %"],
			["#ef4444", `${fc.damage_pct || 0}%`,                   "Damage Rate"],
			["#f97316", fc.avg_items_per_manifest || 0,             "Avg Items/MF"],
			["#8b5cf6", `<span class="${mom_cls}">${mom_arrow} ${Math.abs(fc.mom_change || 0)}%</span>`, "vs Last Month"],
		];
		const fc_html = `<div class="lcc-mini-kpi-grid">
			${fc_items.map(([c, v, l]) => `
				<div class="lcc-mini-kpi" style="--mk:${c}">
					<div class="lcc-mini-kpi-val">${v}</div>
					<div class="lcc-mini-kpi-lbl">${__(l)}</div>
				</div>`).join("")}
		</div>`;

		$ov.append(`
			<div class="lcc-section">
				<div class="lcc-intel-grid">
					<div class="lcc-intel-col">
						<h5 class="lcc-section-title"><i class="fa fa-bolt"></i> ${__("AI Logistics Insights")}</h5>
						<div class="lcc-insights-list">${insight_html}</div>
					</div>
					<div class="lcc-intel-col">
						<h5 class="lcc-section-title"><i class="fa fa-bar-chart"></i> ${__("Financial Control")}</h5>
						${fc_html}
					</div>
				</div>
			</div>`);
	}

	/* ── Planning Suggestions ─────────────────────────────────── */

	_ov_planning($ov, suggestions) {
		if (!suggestions.length) return;
		const type_clr = { Consolidation:"#8b5cf6", Assignment:"#f59e0b", Action:"#3b82f6", Capacity:"#10b981", Info:"#6b7280" };
		const cards = suggestions.map((s) => `
			<div class="lcc-plan-card" style="border-left-color:${type_clr[s.type]||"#6b7280"}">
				<div class="lcc-plan-hdr">
					<i class="fa fa-${s.icon}"></i>
					<span class="lcc-plan-type">${s.type}</span>
				</div>
				<div class="lcc-plan-title">${s.title}</div>
				<div class="lcc-plan-detail">${s.detail}</div>
			</div>`).join("");

		$ov.append(`
			<div class="lcc-section">
				<h5 class="lcc-section-title"><i class="fa fa-lightbulb-o"></i> ${__("Planning Suggestions")}</h5>
				<div class="lcc-plan-grid">${cards}</div>
			</div>`);
	}

	/* ── Route Lane Analysis ──────────────────────────────────── */

	_ov_route_lanes($ov, lanes) {
		if (!lanes.length) return;
		const rows = lanes.map((l) => {
			const otd_c = l.otd_pct >= 90 ? "lcc-badge-green" : l.otd_pct >= 70 ? "lcc-badge-yellow" : "lcc-badge-red";
			const dmg_c = l.damage_pct === 0 ? "lcc-badge-green" : l.damage_pct <= 2 ? "lcc-badge-yellow" : "lcc-badge-red";
			return `<tr>
				<td><strong>${l.origin || "—"}</strong></td>
				<td><strong>${l.destination || "—"}</strong></td>
				<td class="tr">${l.manifests}</td>
				<td class="tr">${parseInt(l.total_qty)}</td>
				<td class="tr">${l.avg_hours || "—"}h</td>
				<td class="tc"><span class="lcc-badge ${otd_c}">${l.otd_pct}%</span></td>
				<td class="tc"><span class="lcc-badge ${dmg_c}">${l.damage_pct}%</span></td>
			</tr>`;
		}).join("");

		$ov.append(`
			<div class="lcc-section">
				<h5 class="lcc-section-title"><i class="fa fa-map-signs"></i> ${__("Route Lane Analysis")}</h5>
				<div class="lcc-table-wrap"><table class="lcc-table">
					<thead><tr>
						<th>${__("Origin")}</th><th>${__("Destination")}</th>
						<th class="tr">${__("Manifests")}</th><th class="tr">${__("Qty")}</th>
						<th class="tr">${__("Avg Hours")}</th>
						<th class="tc">${__("OTD %")}</th><th class="tc">${__("Damage %")}</th>
					</tr></thead>
					<tbody>${rows}</tbody>
				</table></div>
			</div>`);
	}

	/* ── Driver Scorecard ─────────────────────────────────────── */

	_ov_driver_scorecard($ov, drivers) {
		if (!drivers.length) return;
		const rows = drivers.map((d) => {
			const otd_c = d.otd_pct >= 90 ? "lcc-badge-green" : d.otd_pct >= 70 ? "lcc-badge-yellow" : "lcc-badge-red";
			const dmg_c = d.damage_pct === 0 ? "lcc-badge-green" : d.damage_pct <= 2 ? "lcc-badge-yellow" : "lcc-badge-red";
			const rating = d.otd_pct >= 95 && d.damage_pct === 0 ? "⭐" : d.otd_pct >= 80 ? "👍" : "⚠️";
			return `<tr>
				<td><strong>${d.driver_name || "—"}</strong></td>
				<td class="tr">${d.total_manifests}</td>
				<td class="tr">${d.delivered}</td>
				<td class="tc"><span class="lcc-badge ${otd_c}">${d.otd_pct}%</span></td>
				<td class="tc"><span class="lcc-badge ${dmg_c}">${d.damage_pct}%</span></td>
				<td class="tr">${d.avg_hours || "—"}h</td>
				<td class="tr">${parseInt(d.total_qty)}</td>
				<td class="tc">${rating}</td>
			</tr>`;
		}).join("");

		$ov.append(`
			<div class="lcc-section">
				<h5 class="lcc-section-title"><i class="fa fa-id-card"></i> ${__("Driver Scorecard")}</h5>
				<div class="lcc-table-wrap"><table class="lcc-table">
					<thead><tr>
						<th>${__("Driver")}</th>
						<th class="tr">${__("Total")}</th><th class="tr">${__("Delivered")}</th>
						<th class="tc">${__("OTD %")}</th><th class="tc">${__("Damage %")}</th>
						<th class="tr">${__("Avg Hours")}</th><th class="tr">${__("Qty")}</th>
						<th class="tc">${__("Rating")}</th>
					</tr></thead>
					<tbody>${rows}</tbody>
				</table></div>
			</div>`);
	}

	/* ── Quick Actions ────────────────────────────────────────── */

	_ov_quick_actions($ov) {
		$ov.append(`
			<div class="lcc-section">
				<h5 class="lcc-section-title"><i class="fa fa-bolt"></i> ${__("Quick Actions")}</h5>
				<div class="lcc-actions-grid">
					<button class="lcc-action-btn" data-act="new_manifest"><i class="fa fa-plus"></i> ${__("New Transfer Manifest")}</button>
					<button class="lcc-action-btn" data-act="new_transfer"><i class="fa fa-exchange"></i> ${__("New Material Transfer")}</button>
					<button class="lcc-action-btn" data-act="new_trip"><i class="fa fa-road"></i> ${__("New Trip")}</button>
					<button class="lcc-action-btn" data-act="ops_view"><i class="fa fa-sitemap"></i> ${__("Operations Board")}</button>
					<button class="lcc-action-btn" data-act="manifest_list"><i class="fa fa-list-ul"></i> ${__("All Manifests")}</button>
					<button class="lcc-action-btn" data-act="stock_entry_list"><i class="fa fa-cubes"></i> ${__("Stock Entries")}</button>
					<button class="lcc-action-btn" data-act="delivery_app"><i class="fa fa-map-marker"></i> ${__("Delivery App")}</button>
					<button class="lcc-action-btn" data-act="buy_spares"><i class="fa fa-shopping-cart"></i> ${__("Buy Spares")}</button>
				</div>
			</div>`);
	}

	/* ── Manifest detail tabs (Active | Deliveries | Overdue) ─── */
	/* NOTE: "Pending Pickup" removed — it lives in Operations > Unassigned Manifests */

	_ov_manifest_tables($ov, data) {
		const tabs = [
			{ key: "active",     label: "Active Manifests",   count: (data.active_manifests || []).length },
			{ key: "deliveries", label: "Recent Deliveries",  count: (data.recent_deliveries || []).length },
			{ key: "overdue",    label: "Overdue",            count: (data.overdue_manifests || []).length },
		];
		const tab_btns = tabs.map((t, i) => `
			<button class="lcc-tab ${i === 0 ? "active" : ""}" data-tab="${t.key}">
				${__(t.label)} <span class="lcc-tab-badge">${t.count}</span>
			</button>`).join("");
		const panels = tabs.map((t, i) => `
			<div class="lcc-tab-panel ${i === 0 ? "active" : ""}" data-panel="${t.key}"></div>`).join("");

		$ov.append(`
			<div class="lcc-section" id="lcc-ov-tables">
				<h5 class="lcc-section-title"><i class="fa fa-list"></i> ${__("Manifest Detail")}</h5>
				<div class="lcc-tabs">${tab_btns}</div>
				${panels}
			</div>`);

		this._ov_tbl_active($ov, data.active_manifests || []);
		this._ov_tbl_deliveries($ov, data.recent_deliveries || []);
		this._ov_tbl_overdue($ov, data.overdue_manifests || []);
	}

	_ov_activate_tab(key) {
		const $sec = $("#lcc-ov-tables");
		$sec.find(".lcc-tab").removeClass("active");
		$sec.find(`.lcc-tab[data-tab="${key}"]`).addClass("active");
		$sec.find(".lcc-tab-panel").removeClass("active");
		$sec.find(`[data-panel="${key}"]`).addClass("active");
		$sec[0]?.scrollIntoView({ behavior: "smooth", block: "start" });
	}

	_ov_tbl_active($ov, items) {
		const $p = $ov.find('[data-panel="active"]');
		if (!items.length) { $p.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No active manifests")}</div>`); return; }
		const sc = { Draft:"lcc-badge-grey", Assigned:"lcc-badge-yellow", Packed:"lcc-badge-blue", "In Transit":"lcc-badge-purple", Delivered:"lcc-badge-green" };
		// Driver-facing wording, matching the Delivery App's Pickup Pending /
		// Delivery Pending / Delivered tabs — same underlying manifest
		// status, just not "Assigned"/"In Transit" verbatim to the viewer.
		const ml = { "Assigned": __("Pickup Pending"), "In Transit": __("Delivery Pending") };
		const rows = items.map((r) => {
			const dmg = r.damage_reported ? `<span class="lcc-damage-flag"><i class="fa fa-exclamation-triangle"></i> Damage</span>` : "";
			const route = [r.source_store || r.source_warehouse, lcc_destinations_text(r)]
				.filter(Boolean).join(" → ");
			const over = r.estimated_delivery_date && new Date(r.estimated_delivery_date) < new Date()
				&& !["Delivered","Closed"].includes(r.status)
				? `<span class="lcc-badge lcc-badge-red" style="margin-left:4px">OVERDUE</span>` : "";
			return `<tr data-name="${r.name}">
				<td><a href="/app/ch-transfer-manifest/${r.name}">${r.name}</a></td>
				<td><span class="lcc-badge ${sc[r.status]||"lcc-badge-grey"}">${frappe.utils.escape_html(ml[r.status] || r.status)}</span>${dmg}${over}</td>
				<td class="lcc-route-cell" title="${route}">${route || "—"}</td>
				<td>${r.driver_name || "—"}</td>
				<td class="tr">${parseFloat(r.total_qty) || 0}</td>
				<td>${frappe.datetime.str_to_user(r.manifest_date) || "—"}</td>
			</tr>`;
		}).join("");
		$p.html(`<div class="lcc-table-wrap"><table class="lcc-table"><thead><tr>
			<th>${__("Manifest")}</th><th>${__("Status")}</th><th>${__("Route")}</th>
			<th>${__("Driver")}</th><th class="tr">${__("Qty")}</th><th>${__("Date")}</th>
		</tr></thead><tbody>${rows}</tbody></table></div>`);
		$p.find("tbody tr").on("click", function (e) { if (e.target.tagName === "A") return; frappe.set_route("Form", "CH Transfer Manifest", $(this).data("name")); });
	}

	_ov_tbl_deliveries($ov, items) {
		const $p = $ov.find('[data-panel="deliveries"]');
		if (!items.length) { $p.html(`<div class="lcc-empty"><i class="fa fa-inbox"></i> ${__("No deliveries this month")}</div>`); return; }
		const rows = items.map((r) => {
			const dmg = r.damage_reported
				? `<span class="lcc-damage-flag"><i class="fa fa-exclamation-triangle"></i> ${r.damage_notes || "Damage"}</span>`
				: `<span class="lcc-ok-flag"><i class="fa fa-check"></i> OK</span>`;
			const route = [r.source_store, r.destination_store].filter(Boolean).join(" → ");
			const sc = r.status === "Closed" ? "lcc-badge-grey" : "lcc-badge-green";
			let otd = "";
			if (r.delivery_datetime && r.estimated_delivery_date) {
				otd = new Date(r.delivery_datetime) <= new Date(r.estimated_delivery_date)
					? `<span class="lcc-badge lcc-badge-green" style="margin-left:4px">On-Time</span>`
					: `<span class="lcc-badge lcc-badge-red" style="margin-left:4px">Late</span>`;
			}
			return `<tr data-name="${r.name}">
				<td><a href="/app/ch-transfer-manifest/${r.name}">${r.name}</a></td>
				<td><span class="lcc-badge ${sc}">${r.status}</span>${otd}</td>
				<td>${route || "—"}</td>
				<td>${r.driver_name || "—"}</td>
				<td>${r.receiver_name || "—"}</td>
				<td class="tr">${parseFloat(r.total_qty) || 0}</td>
				<td>${dmg}</td>
				<td>${frappe.datetime.str_to_user(r.delivery_datetime) || "—"}</td>
			</tr>`;
		}).join("");
		$p.html(`<div class="lcc-table-wrap"><table class="lcc-table"><thead><tr>
			<th>${__("Manifest")}</th><th>${__("Status")}</th><th>${__("Route")}</th>
			<th>${__("Driver")}</th><th>${__("Receiver")}</th>
			<th class="tr">${__("Qty")}</th><th>${__("Condition")}</th><th>${__("Delivered")}</th>
		</tr></thead><tbody>${rows}</tbody></table></div>`);
		$p.find("tbody tr").on("click", function (e) { if (e.target.tagName === "A") return; frappe.set_route("Form", "CH Transfer Manifest", $(this).data("name")); });
	}

	_ov_tbl_overdue($ov, items) {
		const $p = $ov.find('[data-panel="overdue"]');
		if (!items.length) { $p.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No overdue manifests — all on track!")}</div>`); return; }
		const rows = items.map((r) => {
			const route = [r.source_store || r.source_warehouse, lcc_destinations_text(r)]
				.filter(Boolean).join(" → ");
			const days = parseInt(r.days_overdue) || 0;
			const sev = days >= 5 ? "lcc-badge-red" : days >= 2 ? "lcc-badge-orange" : "lcc-badge-yellow";
			return `<tr data-name="${r.name}">
				<td><a href="/app/ch-transfer-manifest/${r.name}">${r.name}</a></td>
				<td><span class="lcc-badge lcc-badge-grey">${r.status}</span></td>
				<td>${route || "—"}</td>
				<td>${r.driver_name || "<em>Unassigned</em>"}</td>
				<td>${frappe.datetime.str_to_user(r.estimated_delivery_date) || "—"}</td>
				<td class="tc"><span class="lcc-badge ${sev}">${days}d late</span></td>
				<td class="tr">${parseFloat(r.total_qty) || 0}</td>
			</tr>`;
		}).join("");
		$p.html(`<div class="lcc-table-wrap"><table class="lcc-table"><thead><tr>
			<th>${__("Manifest")}</th><th>${__("Status")}</th><th>${__("Route")}</th>
			<th>${__("Driver")}</th><th>${__("ETA")}</th>
			<th class="tc">${__("Overdue")}</th><th class="tr">${__("Qty")}</th>
		</tr></thead><tbody>${rows}</tbody></table></div>`);
		$p.find("tbody tr").on("click", function (e) { if (e.target.tagName === "A") return; frappe.set_route("Form", "CH Transfer Manifest", $(this).data("name")); });
	}

	// ══════════════════════════════════════════════════════════════
	// DRAFT / PENDING WITH GOODS MODES
	// Two separate, purely informational tabs for Material Transfers
	// upstream of packing. No grouping/action here; that starts once an
	// entry reaches Packed (see PACKING MODE below). Both tabs share this
	// same render/pagination/sort machinery, scoped by this.dp_status.
	// ══════════════════════════════════════════════════════════════

	_draft_pending_init(status) {
		// Sort/paging state persists across a refresh but resets whenever the
		// tab is (re)entered fresh, same as most list views.
		this.dp_status = status;
		this.dp_state = { start: 0, page_length: 10, sort_field: "creation", sort_order: "desc" };
		const hint = status === "Draft"
			? __("Showing Material Transfers still in Draft — not yet packed. Click a column heading to sort.")
			: __("Showing Delivery Challans sent out but not yet packed — one row per challan. Click a column heading to sort.");
		$("#lcc-content").html(`
			<div class="lcc-pack" id="lcc-draft-pending">
				<div class="lcc-ops-bar">
					<div class="lcc-ops-bar-actions">
						<button class="btn btn-xs btn-default lcc-dp-refresh-btn">
							<i class="fa fa-refresh"></i> ${__("Refresh")}
						</button>
					</div>
					<div class="lcc-dp-f-date" style="width:220px"></div>
					<span class="lcc-muted lcc-ops-bar-hint">
						<i class="fa fa-info-circle"></i>
						${hint}
					</span>
				</div>
				<div class="lcc-pack-body" id="lcc-dp-body">
					<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading…")}</div>
				</div>
				<div class="lcc-pack-pagination" id="lcc-dp-pagination"></div>
			</div>
		`);
		if (!this._dp_events_bound) {
			const $r = this.$root;
			$r.on("click", ".lcc-dp-refresh-btn", () => this._draft_pending_load());
			$r.on("click", ".lcc-dp-sortable", (e) => {
				const field = $(e.currentTarget).data("field");
				const st = this.dp_state;
				st.sort_order = (st.sort_field === field && st.sort_order === "asc") ? "desc" : "asc";
				st.sort_field = field;
				st.start = 0;
				this._draft_pending_load();
			});
			const dp_go = (e) => {
				const n = this._pager_target($(e.currentTarget));
				if (!n) return;
				this.dp_state.start = (n - 1) * this.dp_state.page_length;
				this._draft_pending_load();
			};
			$r.on("click", '[data-pager="dp"] [data-go], [data-pager="dp"] .lcc-np-go', dp_go);
			$r.on("keydown", '[data-pager="dp"] .lcc-np-input', (e) => { if (e.key === "Enter") dp_go(e); });
			this._dp_events_bound = true;
		}
		this._dp_mount_date();
		this._draft_pending_load();
	}

	// This tab's own date box (Transfer Request Approved / Stock Outward kept
	// apart), on top of the header dates.
	_dp_mount_date() {
		this.dp_dates = this.dp_dates || {};
		const key = this.dp_status;
		const c = frappe.ui.form.make_control({
			parent: this.$root.find(".lcc-dp-f-date"),
			df: {
				fieldtype: "DateRange", fieldname: "dp_date", placeholder: __("Date"),
				onchange: () => {
					const v = c.get_value();
					const next = (v && v[0] && v[1]) ? v : null;
					if (JSON.stringify(next) === JSON.stringify(this.dp_dates[key] || null)) return;
					this.dp_dates[key] = next;
					this.dp_state.start = 0;
					this._draft_pending_load();
				},
			},
			render_input: true,
		});
		c.$wrapper.find(".control-label, .help-box").remove();
		c.$wrapper.css("margin-bottom", 0).find(".form-group").css("margin-bottom", 0);
		if (this.dp_dates[key]) c.set_value(this.dp_dates[key]);
	}

	// Header dates narrowed by a tab's own [from, to] — the later start and
	// the earlier end of the two.
	_merged_dates(tab) {
		const out = this._header_dates();
		if (tab && tab[0] && (!out.from_date || tab[0] > out.from_date)) out.from_date = tab[0];
		if (tab && tab[1] && (!out.to_date || tab[1] < out.to_date)) out.to_date = tab[1];
		return out;
	}


	async _draft_pending_load() {
		const $b = $("#lcc-dp-body");
		$b.html(`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading…")}</div>`);
		try {
			const co = this.filters?.fields?.company?.get_value() || frappe.defaults.get_user_default("Company");
			const st = this.dp_state || (this.dp_state = { start: 0, page_length: 10, sort_field: "creation", sort_order: "desc" });
			const r = await frappe.call({
				method: "ch_erp15.ch_erp15.custom.stock_entry.get_draft_pending_stock_entries",
				args: {
					...this._merged_dates((this.dp_dates || {})[this.dp_status]),
					company: co,
					status: this.dp_status,
					start: st.start,
					page_length: st.page_length,
					sort_field: st.sort_field,
					sort_order: st.sort_order,
				},
			});
			this.draft_pending_queue = (r.message && r.message.rows) || [];
			this.dp_total_count = (r.message && r.message.total_count) || 0;
			this._draft_pending_render();
		} catch (e) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load.")}</div>`);
		}
	}

	// Age badges show minutes for anything under an hour — a Stock Entry
	// that entered its current status 5 minutes ago used to display as
	// "0h" (or worse, the wrong multi-hour figure from document creation
	// time), which reads as broken to whoever is watching it move.
	_fmt_age(age_minutes, age_hours) {
		if (age_minutes == null) return "—";
		if (age_minutes < 60) return `${age_minutes}m`;
		if (age_hours < 24) return `${age_hours}h`;
		return `${Math.floor(age_hours / 24)}d`;
	}

	_dp_sort_th(label, field, extra_cls) {
		const st = this.dp_state || {};
		let icon = `<i class="fa fa-sort lcc-dp-sort-icon"></i>`;
		if (st.sort_field === field) {
			icon = st.sort_order === "asc"
				? `<i class="fa fa-sort-asc lcc-dp-sort-icon active"></i>`
				: `<i class="fa fa-sort-desc lcc-dp-sort-icon active"></i>`;
		}
		return `<th class="lcc-dp-sortable ${extra_cls || ""}" data-field="${field}">${label} ${icon}</th>`;
	}

	// A Trip ID link anywhere on the page opens the trip window. Bound once.
	_bind_trip_drill() {
		if (this._trip_drill_bound) return;
		this._trip_drill_bound = true;
		this.$root.on("click", ".lcc-trip-drill", (e) => {
			e.preventDefault();
			this._trip_drilldown($(e.currentTarget).data("name"));
		});
	}

	// The header's From / To dates (shared hub filters), as API args.
	_header_dates() {
		const f = this.filters || {};
		const out = {};
		const from = typeof f.from_date === "function" ? f.from_date() : "";
		const to = typeof f.to_date === "function" ? f.to_date() : "";
		if (from) out.from_date = from;
		if (to) out.to_date = to;
		return out;
	}

	_trip_pill(status) {
		return {
			"Draft": "gray", "Assigned": "orange", "Started": "blue", "Picked Up": "blue",
			"Delivery Pending": "yellow", "Delivered": "green", "Completed": "green",
			"Closed": "darkgrey", "Cancelled": "red",
		}[status] || "gray";
	}

	// Client-side Asc/Desc for tables whose rows are all in the browser
	// (Packed Orders, Manifest). Same header look as _dp_sort_th; a click on
	// the sorted column flips the order, a click on another starts at Asc.
	_cs_sort_th(table, label, field, extra_cls) {
		const st = (this.cs_sort || {})[table] || {};
		let icon = `<i class="fa fa-sort lcc-dp-sort-icon"></i>`;
		if (st.field === field) {
			icon = `<i class="fa fa-sort-${st.order === "asc" ? "asc" : "desc"} lcc-dp-sort-icon active"></i>`;
		}
		return `<th class="lcc-dp-sortable lcc-cs-sortable ${extra_cls || ""}" data-table="${table}" data-field="${field}">${label} ${icon}</th>`;
	}

	_cs_sorted(table, list, getters) {
		const st = (this.cs_sort || {})[table];
		const get = st && getters[st.field];
		if (!get) return list;
		const dir = st.order === "asc" ? 1 : -1;
		return [...list].sort((a, b) => {
			const x = get(a), y = get(b);
			const xe = x === null || x === undefined || x === "", ye = y === null || y === undefined || y === "";
			if (xe || ye) return xe === ye ? 0 : (xe ? 1 : -1);   // blanks always last
			if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
			return String(x).localeCompare(String(y), undefined, { numeric: true }) * dir;
		});
	}

	_cs_bind_sort() {
		if (this._cs_sort_bound) return;
		this._cs_sort_bound = true;
		this.$root.on("click", ".lcc-cs-sortable", (e) => {
			const table = $(e.currentTarget).data("table");
			const field = $(e.currentTarget).data("field");
			this.cs_sort = this.cs_sort || {};
			const st = this.cs_sort[table] || {};
			this.cs_sort[table] = {
				field, order: (st.field === field && st.order === "asc") ? "desc" : "asc",
			};
			this._pages = this._pages || {};
			this._pages[table] = 1;
			if (table === "packed") this._pack_render();
			else if (table === "register") this._reg_render();
			else if (table === "trips") this._trips_render();
		});
	}

	_draft_pending_render() {
		const $b = $("#lcc-dp-body");
		const $p = $("#lcc-dp-pagination");
		if (!(this.draft_pending_queue || []).length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No Draft or Stock Outward Transfer Requests.")}</div>`);
			$p.html("");
			return;
		}
		const status_colors = {
			"Draft": "red", "Pending With Goods": "orange", "Partially Stock Outward": "yellow",
			"Not Packed": "orange",
			"Partially Packed": "yellow",
			"Packed": "teal", "Ready For Pickup": "teal", "Assigned": "blue",
			"Ready For Receive": "cyan", "In Transit": "yellow", "Receive At Transit": "purple",
			"Partially Transferred": "orange", "Transferred": "green", "Force Closed": "gray",
			"Rejected": "red",
		};
		const rows = this.draft_pending_queue.map((se) => {
			const nm = frappe.utils.escape_html(se.name);
			const total_qty = Number(se.total_qty || 0);
			const box_count = Number(se.box_count || 0);
			const weight    = Number(se.weight_kg || 0);

			let age_cls = "lcc-sev-low";
			if (se.age_hours != null) {
				if (se.age_hours >= 48) age_cls = "lcc-sev-critical";
				else if (se.age_hours >= 24) age_cls = "lcc-sev-high";
				else if (se.age_hours >= 8) age_cls = "lcc-sev-medium";
			}
			const age = this._fmt_age(se.age_minutes, se.age_hours);

			const status = se.custom_status || "Draft";
			const status_color = status_colors[status] || "gray";
			const since = se.custom_status_since || se.creation;
			return `<tr>
				<td>${nm}</td>
				<td>${since ? frappe.datetime.str_to_user(since) : "—"}</td>
				<td><span class="indicator-pill ${status_color}"><span>${frappe.utils.escape_html(__(status))}</span></span></td>
				<td>${window.ch_wh_label_html ? ch_wh_label_html(se.from_warehouse, "—") : frappe.utils.escape_html(se.from_warehouse || "—")}</td>
				<td>${window.ch_wh_label_html ? ch_wh_label_html(se.to_warehouse, "—") : frappe.utils.escape_html(se.to_warehouse || "—")}</td>
				<td class="tr">${total_qty}</td>
				<td class="tr">${box_count}</td>
				<td class="tr">${weight ? weight.toFixed(1) + " kg" : "—"}</td>
				<td><span class="lcc-sev ${age_cls}">${age}</span></td>
			</tr>`;
		}).join("");

		$b.html(`
			<div class="lcc-table-wrap"><table class="lcc-table lcc-dp-table">
				<thead><tr>
					${this._dp_sort_th(this.dp_status === "Draft" ? __("Order Id") : __("DC ID"), "name")}
					${this._dp_sort_th(__("Date"), "creation")}
					${this._dp_sort_th(__("Status"), "custom_status")}
					${this._dp_sort_th(__("Source Warehouse"), "from_warehouse")}
					${this._dp_sort_th(__("Target Warehouse"), "to_warehouse")}
					${this._dp_sort_th(__("Qty"), "total_qty", "tr")}
					${this._dp_sort_th(__("Boxes"), "box_count", "tr")}
					${this._dp_sort_th(__("Weight"), "weight_kg", "tr")}
					${this._dp_sort_th(__("Age"), "age_hours")}
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>
		`);

		const st = this.dp_state;
		const total = this.dp_total_count || 0;
		const from = total ? st.start + 1 : 0;
		const to = Math.min(st.start + st.page_length, total);
		const pages = Math.max(Math.ceil(total / st.page_length), 1);
		const page = Math.floor(st.start / st.page_length) + 1;
		$p.html(total > st.page_length
			? this._numbered_pager_html("dp", page, pages, from, to, total)
			: `<div class="lcc-dp-page-info">${__("Showing {0}-{1} of {2}", [from, to, total])}</div>`);
	}

	// ══════════════════════════════════════════════════════════════
	// MANIFEST MODE — every shipment on a manifest, one row per challan,
	// with where it stands: pending / fulfilled / rejected qty and its own
	// logistic status (see logistics_api.ops_manifest_register).
	// ══════════════════════════════════════════════════════════════
	// Pickup Pending is Operations' Unassigned Manifests table itself (same
	// rows, Attach / Create Trip / Shipments / Cancel), fed by _ops_load.
	// Delivery Pending and Delivered are the per-challan register below,
	// split by where each challan stands (see _reg_in_sub).
	// ── Shared filter bar: the Packed Order filters (Search, Date, Source and
	// Target Warehouse, several at once) for Manifest, Trip, Pickup Pending
	// and Delivered. Each tab says how to read its rows (cfg); the rows are
	// already in the browser, so filtering happens here.
	_flt_bar_html(key, search_ph) {
		return `<div class="lcc-flt-bar" data-key="${key}"
				style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;width:100%">
			<input type="search" class="form-control input-xs lcc-flt-search" data-key="${key}"
				style="width:260px" placeholder="${search_ph}">
			<div class="lcc-flt-date" style="width:220px"></div>
			<div class="lcc-flt-src" style="width:260px"></div>
			<div class="lcc-flt-dst" style="width:260px"></div>
		</div>`;
	}

	// cfg: rows(), text(r) -> [strings], date(r) -> "YYYY-MM-DD…",
	//      src(r) / dst(r) -> [warehouse], date_ph, page_key, rerender()
	_flt_mount(key, cfg) {
		this._flt = this._flt || {};
		this._flt_cfg = this._flt_cfg || {};
		this._flt_cfg[key] = cfg;
		const st = this._flt[key] = this._flt[key] || { search: "", date: null, src: [], dst: [] };
		const $bar = this.$root.find(`.lcc-flt-bar[data-key="${key}"]`);
		const refilter = () => {
			this._pages = this._pages || {};
			this._pages[cfg.page_key || key] = 1;
			cfg.rerender();
		};
		if (!this._flt_bound) {
			this._flt_bound = true;
			this.$root.on("input", ".lcc-flt-search", frappe.utils.debounce((e) => {
				const k = $(e.target).data("key");
				if (!this._flt[k]) return;
				this._flt[k].search = e.target.value || "";
				const c = this._flt_cfg[k];
				this._pages = this._pages || {};
				this._pages[c.page_key || k] = 1;
				c.rerender();
			}, 200));
		}
		$bar.find(".lcc-flt-search").val(st.search);
		const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh);
		const options = (pick, txt) => {
			const needle = String(txt || "").toLowerCase();
			const seen = new Set();
			return (cfg.rows() || []).flatMap((r) => pick(r) || [])
				.filter((wh) => wh && !seen.has(wh) && seen.add(wh))
				.map((wh) => ({ value: wh, label: label(wh), description: wh !== label(wh) ? wh : "" }))
				.filter((o) => !needle || `${o.label} ${o.value}`.toLowerCase().includes(needle))
				.sort((a, b) => a.label.localeCompare(b.label));
		};
		const make = (sel, df) => {
			const c = frappe.ui.form.make_control({ parent: $bar.find(sel), df, render_input: true });
			c.$wrapper.css("margin-bottom", 0);
			c.$wrapper.find(".control-label, .help-box").remove();
			c.$wrapper.find(".form-group").css("margin-bottom", 0);
			return c;
		};
		const date = make(".lcc-flt-date", {
			fieldtype: "DateRange", fieldname: `${key}_date`, placeholder: cfg.date_ph || __("Date"),
			onchange: () => {
				const v = date.get_value();
				st.date = (v && v[0] && v[1]) ? v : null;
				refilter();
			},
		});
		const src = make(".lcc-flt-src", {
			fieldtype: "MultiSelectList", fieldname: `${key}_src`, placeholder: __("Source Warehouse"),
			get_data: (txt) => options(cfg.src, txt),
			onchange: () => { st.src = src.get_value() || []; refilter(); },
		});
		const dst = make(".lcc-flt-dst", {
			fieldtype: "MultiSelectList", fieldname: `${key}_dst`, placeholder: __("Target Warehouse"),
			get_data: (txt) => options(cfg.dst, txt),
			onchange: () => { st.dst = dst.get_value() || []; refilter(); },
		});
		// Coming back to the tab keeps what was chosen.
		if (st.date) date.set_value(st.date);
		if (st.src.length) src.set_value(st.src);
		if (st.dst.length) dst.set_value(st.dst);
	}

	_flt_apply(key, list) {
		const st = (this._flt || {})[key];
		const cfg = (this._flt_cfg || {})[key];
		if (!st || !cfg) return list;
		const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh);
		const needle = String(st.search || "").trim().toLowerCase();
		return list.filter((r) => {
			if (st.src.length && !(cfg.src(r) || []).some((w) => st.src.includes(w))) return false;
			if (st.dst.length && !(cfg.dst(r) || []).some((w) => st.dst.includes(w))) return false;
			if (st.date) {
				const day = String(cfg.date(r) || "").slice(0, 10);
				if (!day || day < st.date[0] || day > st.date[1]) return false;
			}
			if (needle) {
				const whs = [...(cfg.src(r) || []), ...(cfg.dst(r) || [])];
				const hay = [...(cfg.text(r) || []), ...whs, ...whs.map(label)];
				if (!hay.some((v) => String(v || "").toLowerCase().includes(needle))) return false;
			}
			return true;
		});
	}

	_flt_active(key) {
		const st = (this._flt || {})[key];
		return !!(st && (String(st.search || "").trim() || st.date || st.src.length || st.dst.length));
	}

	_reg_show_sub() {
		const $s = $("#lcc-content");
		if (this.reg_sub === "pickup") {
			$s.html(`
				<div class="lcc-ops-bar">${this._flt_bar_html("manifest", __("Search manifest or warehouse…"))}</div>
				<div id="lcc-reg-pickup"><div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading manifests…")}</div></div>`);
			const dests = (m) => ((m.destinations || []).filter(Boolean).length
				? m.destinations.filter(Boolean) : [m.destination_warehouse]);
			this._flt_mount("manifest", {
				rows: () => this.unassigned || [],
				text: (m) => [m.name, m.status, m.direction],
				date: (m) => m.creation,
				date_ph: __("Created"),
				src: (m) => [m.source_warehouse],
				dst: dests,
				page_key: "manifests",
				rerender: () => this._reg_render_pickup(),
			});
			// The table's buttons are Operations' own delegated handlers.
			this._ops_bind_events();
			this._ops_load();
			// Fills the Delivery Pending / Delivered counts on their tabs.
			this._reg_load();
			return;
		}
		// Delivery Pending and Delivered are the same per-challan list, split
		// by where each challan stands (see _reg_in_sub). The rows are kept
		// when switching between the two, so the switch is instant.
		this._reg_init_register($s);
	}

	// Pickup Pending: on a trip, the driver has not accepted it yet.
	// Picked Up: accepted (in the vehicle), not delivered yet.
	// Delivered: everything finished. Not Assigned rows are on the Manifest tab.
	_reg_in_sub(r, sub) {
		const st = r.logistic_status;
		if (st === "Not Assigned") return false;
		const picked = ["In Transit", "Recall Initiated", "Revert Requested"].includes(st);
		if (sub === "transit") return st === "Pending Pickup";
		if (sub === "pickedup") return picked;
		return st !== "Pending Pickup" && !picked;
	}

	_reg_update_counts() {
		const rows = this.reg_rows || [];
		$("#lcc-reg-cnt-transit").text(rows.filter((r) => this._reg_in_sub(r, "transit")).length);
		$("#lcc-reg-cnt-pickedup").text(rows.filter((r) => this._reg_in_sub(r, "pickedup")).length);
		$("#lcc-reg-cnt-delivered").text(rows.filter((r) => this._reg_in_sub(r, "delivered")).length);
	}

	// Called at the end of _ops_load while the Manifest tab is open.
	_reg_render_pickup() {
		$("#lcc-reg-cnt-pickup").text((this.unassigned || []).length);
		const $p = $("#lcc-reg-pickup");
		if ($p.length) this._ops_render_manifests($p);
	}

	_reg_init_register($c) {
		$c.html(`
			<div>
				<div class="lcc-ops-bar">
					<div class="lcc-ops-bar-actions">
						<button class="btn btn-xs btn-default lcc-reg-refresh-btn">
							<i class="fa fa-refresh"></i> ${__("Refresh")}
						</button>
					</div>
					${this._flt_bar_html(`reg_${this.reg_sub}`, __("Search DC ID, manifest, driver or warehouse…"))}
				</div>
				<div id="lcc-reg-body">
					<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading manifests…")}</div>
				</div>
			</div>`);
		if (!this._reg_events_bound) {
			const $r = this.$root;
			$r.on("click", ".lcc-reg-refresh-btn", () => this._reg_load());
			$r.on("click", ".lcc-reg-remove", (e) => {
				e.preventDefault();
				const $b = $(e.currentTarget);
				this._ops_remove_pending($b.data("manifest"), $b.data("stock-entry"), $b.data("label"));
			});
			this._reg_events_bound = true;
		}
		this._flt_mount(`reg_${this.reg_sub}`, {
			rows: () => (this.reg_rows || []).filter((r) => this._reg_in_sub(r, this.reg_sub)),
			text: (r) => [r.delivery_challan, r.stock_entry, r.manifest, r.driver_name, r.trip,
				r.trip_status, r.logistic_status],
			date: (r) => r.dc_date || r.manifest_date,
			date_ph: __("DC Date"),
			src: (r) => [r.from_warehouse],
			dst: (r) => [r.to_warehouse],
			page_key: "register",
			rerender: () => this._reg_render(),
		});
		this._reg_load();
	}

	async _reg_load() {
		const $b = $("#lcc-reg-body");
		$b.html(`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading manifests…")}</div>`);
		try {
			const co = this.filters?.fields?.company?.get_value() || undefined;
			const args = {};
			if (co) args.company = co;
			Object.assign(args, this._header_dates());
			const r = await frappe.call({ method: _LCC + "ops_manifest_register", args });
			this.reg_rows = r.message || [];
			this._pages = this._pages || {};
			this._pages["register"] = 1;
			this._reg_render();
		} catch (e) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load manifests.")}</div>`);
		}
	}

	_reg_render() {
		const $b = $("#lcc-reg-body");
		const esc = frappe.utils.escape_html;
		const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh) || "";
		const html_wh = (wh) => (window.ch_wh_label_html ? ch_wh_label_html(wh, "—") : esc(wh || "—"));
		const sub = ["transit", "pickedup"].includes(this.reg_sub) ? this.reg_sub : "delivered";
		this._reg_update_counts();
		const list = this._flt_apply(`reg_${sub}`,
			(this.reg_rows || []).filter((r) => this._reg_in_sub(r, sub)));
		if (!list.length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-search"></i> ${this._flt_active(`reg_${sub}`)
				? __("No row matches the filters.")
				: ({ transit: __("No order waiting for the driver to accept."),
					pickedup: __("No order picked up and on the way.") }[sub]
					|| __("Nothing delivered in this period."))}</div>`);
			return;
		}
		const pill = {
			"Not Assigned": "gray", "Pending Pickup": "orange", "In Transit": "blue",
			"Delivered": "green", "Revert Requested": "yellow", "Reverted": "red",
			"Cancelled": "red", "Rejected": "red", "Returned": "red", "Recall Initiated": "yellow",
		};
		const qty = (v) => {
			const n = Number(v || 0);
			return n ? String(Math.round(n * 100) / 100) : `<span class="text-muted">0</span>`;
		};
		this._cs_bind_sort();
		const sorted = this._cs_sorted("register", list, {
			manifest: (r) => r.manifest,
			date: (r) => r.dc_date || r.manifest_date || "",
			driver: (r) => r.driver_name || "",
			mcreated: (r) => r.creation || "",
			trip: (r) => r.trip || "",
			tdate: (r) => r.trip_date || "",
			tstatus: (r) => r.trip_status || "",
			dc: (r) => r.delivery_challan || r.stock_entry || "",
			src: (r) => label(r.from_warehouse),
			dst: (r) => label(r.to_warehouse),
			pending: (r) => Number(r.pending_qty || 0),
			fulfill: (r) => Number(r.fulfilled_qty || 0),
			rejected: (r) => Number(r.rejected_qty || 0),
			lstatus: (r) => r.logistic_status || "",
		});
		const page = this._page_slice("register", sorted);
		const rows = page.rows.map((r) => `<tr>
			<td title="${esc(r.stock_entry || "")}"><b>${esc(r.delivery_challan || r.stock_entry || "—")}</b></td>
			<td style="white-space:nowrap">${(r.dc_date || r.manifest_date) ? frappe.datetime.str_to_user(r.dc_date || r.manifest_date) : "—"}</td>
			<td><a href="/app/ch-transfer-manifest/${encodeURIComponent(r.manifest)}" target="_blank">${esc(r.manifest)}</a></td>
			<td style="white-space:nowrap">${r.creation ? frappe.datetime.str_to_user(r.creation) : "—"}</td>
			<td>${r.trip ? `<a href="#" class="lcc-trip-drill" data-name="${esc(r.trip)}">${esc(r.trip)}</a>` : "—"}</td>
			<td style="white-space:nowrap">${r.trip_date ? frappe.datetime.str_to_user(r.trip_date) : "—"}</td>
			<td>${r.trip_status ? `<span class="indicator-pill ${this._trip_pill(r.trip_status)}"><span>${esc(__(r.trip_status))}</span></span>` : "—"}</td>
			<td>${esc(r.driver_name || "—")}</td>
			<td>${html_wh(r.from_warehouse)}</td>
			<td>${html_wh(r.to_warehouse)}</td>
			<td class="tr">${qty(r.pending_qty)}</td>
			<td class="tr">${qty(r.fulfilled_qty)}</td>
			<td class="tr">${qty(r.rejected_qty)}</td>
			<td><span class="indicator-pill ${pill[r.logistic_status] || "gray"}"><span>${esc(
				r.logistic_status === "In Transit" ? __("Picked Up") : __(r.logistic_status || "—"))}</span></span></td>
			${sub === "transit" ? `<td class="text-right">${
				// Only a driver's shipment not yet accepted can come off the trip
				// (remove_pending_shipment); a courier's moves with its trip.
				r.manifest_status === "Assigned" ? `<button class="btn btn-xs btn-default lcc-reg-remove"
					data-manifest="${esc(r.manifest)}" data-stock-entry="${esc(r.stock_entry)}"
					data-label="${esc(r.delivery_challan || r.stock_entry)}">
					<i class="fa fa-undo"></i> ${__("Remove")}</button>` : ""}</td>` : ""}
		</tr>`).join("");
		$b.html(`
			<div class="lcc-table-wrap"><table class="lcc-table">
				<thead><tr>
					${this._cs_sort_th("register", __("DC ID"), "dc")}
					${this._cs_sort_th("register", __("DC Date"), "date")}
					${this._cs_sort_th("register", __("Manifest ID"), "manifest")}
					${this._cs_sort_th("register", __("Manifest Created"), "mcreated")}
					${this._cs_sort_th("register", __("Trip ID"), "trip")}
					${this._cs_sort_th("register", __("Trip Date"), "tdate")}
					${this._cs_sort_th("register", __("Trip Status"), "tstatus")}
					${this._cs_sort_th("register", __("Driver Name"), "driver")}
					${this._cs_sort_th("register", __("Source Warehouse"), "src")}
					${this._cs_sort_th("register", __("Delivery Warehouse"), "dst")}
					${this._cs_sort_th("register", __("Pending"), "pending", "tr")}
					${this._cs_sort_th("register", __("Fulfill"), "fulfill", "tr")}
					${this._cs_sort_th("register", __("Rejected"), "rejected", "tr")}
					${this._cs_sort_th("register", __("DC Status"), "lstatus")}
					${sub === "transit" ? `<th></th>` : ""}
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>
			${this._pager_html("register", page, list.length)}`);
		this._bind_pager($b, "register", () => this._reg_render());
	}

	// ══════════════════════════════════════════════════════════════
	// TRIP MODE — every trip, newest first, with how far its shipments got
	// (see logistics_api.ops_trip_register). Create Trip from Selected on
	// the Manifest tab lands here.
	// ══════════════════════════════════════════════════════════════
	_trips_init() {
		$("#lcc-content").html(`
			<div class="lcc-section">
				<div class="lcc-ops-bar">
					<div class="lcc-ops-bar-actions">
						<button class="btn btn-xs btn-default lcc-trips-refresh-btn">
							<i class="fa fa-refresh"></i> ${__("Refresh")}
						</button>
					</div>
					${this._flt_bar_html("trips", __("Search trip, driver or vehicle…"))}
				</div>
				<div id="lcc-trips-body">
					<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading trips…")}</div>
				</div>
			</div>`);
		if (!this._trips_events_bound) {
			this._trips_events_bound = true;
			const $r = this.$root;
			$r.on("click", ".lcc-trips-refresh-btn", () => this._trips_load());

		}
		this._flt_mount("trips", {
			rows: () => this.trips_rows || [],
			text: (t) => [t.name, t.driver_name, t.carrier_name, t.vehicle_number, t.vehicle, t.status, t.route],
			date: (t) => t.trip_date,
			date_ph: __("Trip Date"),
			src: (t) => [t.hub_warehouse],
			dst: (t) => t.drops || [],
			page_key: "trips",
			rerender: () => this._trips_render(),
		});
		this._trips_load();
	}

	// Trip → its manifests, in the same window style as the manifest
	// drill-down. A manifest opens _ops_show_manifest_items, which carries on
	// to Delivery Challan → Items → IMEI exactly as from the Manifest tab.
	async _trip_drilldown(trip) {
		const esc = frappe.utils.escape_html;
		const loc = (w) => (window.ch_wh_label_html ? ch_wh_label_html(w, "—") : esc(w || "—"));
		const d = new frappe.ui.Dialog({
			title: __("Trip {0}", [trip]),
			size: "large",
			fields: [{ fieldname: "trip_html", fieldtype: "HTML" }],
		});
		const $w = d.fields_dict.trip_html.$wrapper;
		$w.html(`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading…")}</div>`);
		d.show();
		let t;
		try {
			t = (await frappe.call({ method: _LCC + "ops_trip_drilldown", args: { trip } })).message || {};
		} catch (e) {
			$w.html(`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load the trip.")}</div>`);
			return;
		}
		const manifests = t.manifests || [];
		if (!manifests.length) {
			$w.html(`<div class="lcc-empty">${__("No manifests on this trip.")}</div>`);
			return;
		}
		const body = manifests.map((m) => {
			const orders = m.orders || [];
			const dests = [...new Set(orders.map((o) => o.to_warehouse).filter(Boolean))];
			return `<tr>
				<td><a href="#" class="lcc-trip-mf-open" data-name="${esc(m.name)}">${esc(m.name)}</a></td>
				<td class="tr">${orders.length}</td>
				<td class="tr">${m.delivered}</td>
				<td class="tr">${m.total_qty || 0}</td>
				<td>${m.manifest_date ? frappe.datetime.str_to_user(m.manifest_date) : "—"}</td>
				<td>${loc(m.source_warehouse)}</td>
				<td>${dests.map(loc).join(", ") || "—"}</td>
				<td><span class="indicator-pill ${this._status_color(m.status)}"><span>${esc(__(m.status || "—"))}</span></span></td>
			</tr>`;
		}).join("");
		$w.html(`
			<p class="text-muted small">${esc(t.driver_name || t.carrier_name || "—")} · ${esc(t.vehicle_number || "—")} · ${
				t.trip_date ? frappe.datetime.str_to_user(t.trip_date) : ""} · ${esc(__(t.status || ""))}</p>
			<div class="lcc-table-wrap"><table class="lcc-table">
				<thead><tr>
					<th>${__("Manifest")}</th>
					<th class="tr">${__("Orders")}</th>
					<th class="tr">${__("Delivered")}</th>
					<th class="tr">${__("Qty")}</th>
					<th>${__("Date")}</th>
					<th>${__("From")}</th>
					<th>${__("To")}</th>
					<th>${__("Status")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table></div>`);
		$w.on("click", ".lcc-trip-mf-open", (e) => {
			e.preventDefault();
			this._ops_show_manifest_items($(e.currentTarget).data("name"));
		});
	}

	async _trips_load() {
		$("#lcc-trips-body").html(`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading trips…")}</div>`);
		try {
			const co = this.filters?.fields?.company?.get_value() || undefined;
			const args = {};
			if (co) args.company = co;
			Object.assign(args, this._header_dates());
			const r = await frappe.call({ method: _LCC + "ops_trip_register", args });
			this.trips_rows = r.message || [];
			this._pages = this._pages || {};
			this._pages["trips"] = 1;
			this._trips_render();
		} catch (e) {
			$("#lcc-trips-body").html(`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load trips.")}</div>`);
		}
	}

	_trips_render() {
		const $b = $("#lcc-trips-body");
		const esc = frappe.utils.escape_html;
		const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh) || "";
		const html_wh = (wh) => (window.ch_wh_label_html ? ch_wh_label_html(wh, "—") : esc(wh || "—"));
		const carrier = (t) => t.driver_name || t.carrier_name || "";
		const all = this.trips_rows || [];
		$("#lcc-reg-cnt-trip").text(all.length);
		const list = this._flt_apply("trips", all);
		if (!list.length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-search"></i> ${this._flt_active("trips")
				? __("No trip matches the filters.") : __("No trips on the road. A trip leaves this list once it is delivered or closed.")}</div>`);
			return;
		}
		this._cs_bind_sort();
		const sorted = this._cs_sorted("trips", list, {
			trip: (t) => t.name,
			date: (t) => t.trip_date || "",
			driver: (t) => carrier(t),
			vehicle: (t) => t.vehicle_number || t.vehicle || "",
			stops: (t) => (t.drops || []).length,
			manifests: (t) => Number(t.manifests || 0),
			shipments: (t) => Number(t.shipments || 0),
			accepted: (t) => Number(t.accepted || 0),
			delivered: (t) => Number(t.delivered || 0),
			status: (t) => t.status || "",
			created: (t) => t.creation || "",
		});
		const pill = {
			"Draft": "gray", "Assigned": "orange", "Started": "blue", "Picked Up": "blue",
			"Delivery Pending": "yellow", "Delivered": "green", "Completed": "green",
			"Closed": "darkgrey", "Cancelled": "red",
		};
		const page = this._page_slice("trips", sorted);
		const rows = page.rows.map((t) => {
			const drops = t.drops || [];
			const shown = drops.slice(0, 3).map(html_wh).join(", ");
			const more = drops.length > 3 ? ` <span class="lcc-muted">+${drops.length - 3}</span>` : "";
			return `<tr>
				<td style="white-space:nowrap">
					<a href="#" class="lcc-trip-drill" data-name="${esc(t.name)}" title="${__("Manifests, orders and items")}">${esc(t.name)}</a>
				</td>
				<td>${t.trip_date ? frappe.datetime.str_to_user(t.trip_date) : "—"}</td>
				<td>${esc(carrier(t) || "—")}</td>
				<td>${esc(t.vehicle_number || t.vehicle || "—")}</td>
				<td>${html_wh(t.hub_warehouse)} → ${shown || "—"}${more}</td>
				<td class="tr">${t.manifests || 0}</td>
				<td class="tr">${t.shipments || 0}</td>
				<td class="tr">${t.accepted || 0}</td>
				<td class="tr">${t.delivered || 0}</td>
				<td><span class="indicator-pill ${pill[t.status] || "gray"}"><span>${esc(__(t.status || "—"))}</span></span></td>
				<td>${t.creation ? frappe.datetime.str_to_user(t.creation) : "—"}</td>
			</tr>`;
		}).join("");
		$b.html(`
			<div class="lcc-table-wrap"><table class="lcc-table">
				<thead><tr>
					${this._cs_sort_th("trips", __("Trip ID"), "trip")}
					${this._cs_sort_th("trips", __("Trip Date"), "date")}
					${this._cs_sort_th("trips", __("Driver"), "driver")}
					${this._cs_sort_th("trips", __("Vehicle"), "vehicle")}
					${this._cs_sort_th("trips", __("Route"), "stops")}
					${this._cs_sort_th("trips", __("Manifests"), "manifests", "tr")}
					${this._cs_sort_th("trips", __("Shipments"), "shipments", "tr")}
					${this._cs_sort_th("trips", __("Accepted"), "accepted", "tr")}
					${this._cs_sort_th("trips", __("Delivered"), "delivered", "tr")}
					${this._cs_sort_th("trips", __("Trip Status"), "status")}
					${this._cs_sort_th("trips", __("Created"), "created")}
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>
			${this._pager_html("trips", page, list.length)}`);
		this._bind_pager($b, "trips", () => this._trips_render());
	}

	// ══════════════════════════════════════════════════════════════
	// PACKING MODE
	// Oracle WMS Cloud / Manhattan Active WMS-style "Pack Station Work
	// Queue".  Lists every Draft manifest with running packing progress
	// (cartons, packed qty, total weight, last packer) and lets the
	// packer mint cartons or hand the manifest off to dispatch without
	// opening the form.
	// ══════════════════════════════════════════════════════════════

	_pack_init() {
		$("#lcc-content").html(`
			<div class="lcc-pack" id="lcc-pack">
				<div class="lcc-pack-howto">
					<div class="lcc-pack-howto-title">
						<i class="fa fa-cube"></i> ${__("Packed Orders")}
					</div>
					<ol class="lcc-pack-steps">
						<li><b>${__("Pack Box, from the Stock Entry")}</b> — ${__("packing happens on the Stock Entry form itself (Pack Box button), independent of any manifest: enter qty, weight, dimensions, seal & photo per carton. The system mints a unique LPN like BMTNMT26000123-B01. custom_status becomes Partially Packed while qty remains, and Packed once every item is fully boxed — the entries below are exactly the fully Packed ones.")}</li>
						<li><b>${__("Tick and group, here")}</b> — ${__("tick one or more Packed, unlinked Stock Entries below (they must share the same source/destination) and click Create Manifest to group them for dispatch. Grouping is what advances them to Ready For Pickup — there's no separate manual status step.")}</li>
					</ol>
					<div class="lcc-pack-bundle-tip">
						<i class="fa fa-qrcode"></i>
						${__("<b>Bundling multiple manifests under one pickup QR?</b> Once they are on a manifest, switch to <b>Operations</b> → tick the manifests in <b>Unassigned Manifests</b> → click <b>Bundle &amp; Print Pickup QR</b>. The system clubs same-destination shipments into one trip stop with a single consolidated QR — driver scans once per drop.")}
					</div>
				</div>
				<div class="lcc-ops-bar">
					<div class="lcc-ops-bar-actions">
						<button class="btn btn-xs btn-default lcc-pack-refresh-btn">
							<i class="fa fa-refresh"></i> ${__("Refresh")}
						</button>
						<button class="btn btn-xs btn-primary lcc-pack-create-manifest-btn" disabled>
							<i class="fa fa-plus"></i> ${__("Create Manifest")}
						</button>
					</div>
					<div class="lcc-pack-filters" style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;width:100%">
						<input type="search" class="form-control input-xs lcc-pack-search"
							style="width:260px"
							placeholder="${__("Search challan, order, store or warehouse…")}">
						<div class="lcc-pack-f-date" style="width:220px"></div>
						<div class="lcc-pack-f-src" style="width:260px"></div>
						<div class="lcc-pack-f-dst" style="width:260px"></div>
					</div>
					<span class="lcc-muted lcc-ops-bar-hint">
						<i class="fa fa-info-circle"></i>
						${__("Showing packed delivery challans not yet on a manifest. Tick the challans to send and click Create Manifest — each challan can go on its own once it is packed.")}
					</span>
				</div>
				<div class="lcc-pack-body" id="lcc-pack-body">
					<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading packed orders…")}</div>
				</div>
			</div>
		`);
		if (!this._pack_events_bound) {
			const $r = this.$root;
			$r.on("click", ".lcc-pack-refresh-btn",         () => this._pack_load());
			$r.on("input", ".lcc-pack-search", frappe.utils.debounce((e) => {
				this.pack_search = e.target.value || "";
				this._pages = this._pages || {};
				this._pages["packed"] = 1;
				// Only #lcc-pack-body is redrawn — the box lives in the bar
				// above it, so the cursor stays where the typing left it.
				this._pack_render();
			}, 200));
			$r.on("click", ".lcc-pack-create-manifest-btn", () => this._pack_create_manifest());
			$r.on("change", ".lcc-pack-select-all", (e) => {
				$r.find(".lcc-pack-row-check:not(:disabled)").prop("checked", e.currentTarget.checked);
				this._pack_update_create_btn();
			});
			// Each challan is ticked on its own — Create Manifest separates a
			// transfer sent in batches into one transfer per challan.
			$r.on("change", ".lcc-pack-row-check", () => this._pack_update_create_btn());
			this._pack_events_bound = true;
		}
		this._pack_make_filters();
		this._pack_load();
	}

	// Date range + Source / Target warehouse (several at once). The queue is
	// already all in the browser, so these narrow it here, like the search.
	_pack_make_filters() {
		this.pack_filters = this.pack_filters || { date: null, src: [], dst: [] };
		const $r = this.$root;
		const refilter = () => {
			this._pages = this._pages || {};
			this._pages["packed"] = 1;
			this._pack_render();
		};
		const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh);
		const options = (key, txt) => {
			const needle = String(txt || "").toLowerCase();
			const seen = new Set();
			return (this.pack_queue || [])
				.map((se) => se[key])
				.filter((wh) => wh && !seen.has(wh) && seen.add(wh))
				.map((wh) => ({ value: wh, label: label(wh), description: wh !== label(wh) ? wh : "" }))
				.filter((o) => !needle || `${o.label} ${o.value}`.toLowerCase().includes(needle))
				.sort((a, b) => a.label.localeCompare(b.label));
		};
		const make = (parent, df) => {
			const c = frappe.ui.form.make_control({
				parent: $r.find(parent), df, render_input: true, only_input: false,
			});
			// No label above the box: keep the row as tall as the search box.
			c.$wrapper.css({ "margin-bottom": 0 });
			c.$wrapper.find(".control-label, .help-box").remove();
			c.$wrapper.find(".form-group").css("margin-bottom", 0);
			return c;
		};
		this.pack_f_date = make(".lcc-pack-f-date", {
			fieldtype: "DateRange", fieldname: "pack_date", placeholder: __("Date"),
			onchange: () => {
				const v = this.pack_f_date.get_value();
				this.pack_filters.date = (v && v[0] && v[1]) ? v : null;
				refilter();
			},
		});
		this.pack_f_src = make(".lcc-pack-f-src", {
			fieldtype: "MultiSelectList", fieldname: "pack_src", placeholder: __("Source Warehouse"),
			get_data: (txt) => options("from_warehouse", txt),
			onchange: () => { this.pack_filters.src = this.pack_f_src.get_value() || []; refilter(); },
		});
		this.pack_f_dst = make(".lcc-pack-f-dst", {
			fieldtype: "MultiSelectList", fieldname: "pack_dst", placeholder: __("Target Warehouse"),
			get_data: (txt) => options("to_warehouse", txt),
			onchange: () => { this.pack_filters.dst = this.pack_f_dst.get_value() || []; refilter(); },
		});
		// Coming back to this tab keeps what was chosen.
		$r.find(".lcc-pack-search").val(this.pack_search || "");
		if (this.pack_filters.date) this.pack_f_date.set_value(this.pack_filters.date);
		if (this.pack_filters.src.length) this.pack_f_src.set_value(this.pack_filters.src);
		if (this.pack_filters.dst.length) this.pack_f_dst.set_value(this.pack_filters.dst);
	}

	_pack_passes_filters(se) {
		const f = this.pack_filters || {};
		const hd = this._header_dates();
		if (hd.from_date || hd.to_date) {
			const day = String(se.custom_status_since || se.creation || "").slice(0, 10);
			if ((hd.from_date && day < hd.from_date) || (hd.to_date && day > hd.to_date)) return false;
		}
		if (f.src && f.src.length && !f.src.includes(se.from_warehouse)) return false;
		if (f.dst && f.dst.length && !f.dst.includes(se.to_warehouse)) return false;
		if (f.date) {
			// The Date column: when the order reached Packed.
			const day = String(se.custom_status_since || se.creation || "").slice(0, 10);
			if (!day || day < f.date[0] || day > f.date[1]) return false;
		}
		return true;
	}

	async _pack_load() {
		const $b = $("#lcc-pack-body");
		$b.html(`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading packed orders…")}</div>`);
		try {
			const co = this.filters?.fields?.company?.get_value() || frappe.defaults.get_user_default("Company");
			const r = await frappe.call({
				method: "ch_erp15.ch_erp15.custom.stock_entry.get_manifestable_stock_entries",
				args: { company: co },
			});
			this.pack_queue = r.message || [];
			this._pack_render();
		} catch (e) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load packed orders.")}</div>`);
		}
	}

	// What the search box matches: the challan ids the table now shows and
	// the transfer id behind them — the challan is what travels with the
	// goods, but the transfer id is still what somebody may have written
	// down — plus both warehouses, by their raw names AND by the store
	// labels on screen, so typing what is displayed finds the row.
	_pack_matches(se, needle) {
		if (!needle) return true;
		const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh);
		return [se.name, ...(se.delivery_challans || []),
			se.from_warehouse, se.to_warehouse,
			label(se.from_warehouse), label(se.to_warehouse)]
			.some((v) => String(v || "").toLowerCase().includes(needle));
	}

	_pack_render() {
		const $b = $("#lcc-pack-body");
		if (!(this.pack_queue || []).length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No packed delivery challans waiting to be grouped into a manifest.")}</div>`);
			return;
		}
		const needle = String(this.pack_search || "").trim().toLowerCase();
		const queue = this.pack_queue.filter(
			(se) => this._pack_matches(se, needle) && this._pack_passes_filters(se));
		if (!queue.length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-search"></i> ${needle
				? __("No packed order matches “{0}”.", [frappe.utils.escape_html(needle)])
				: __("No packed order matches the filters.")}</div>`);
			return;
		}
		this._cs_bind_sort();
		const label = (wh) => (window.ch_wh_label ? ch_wh_label(wh) : wh) || "";
		const first_dc = (se) => ((se.delivery_challan_rows || []).find((c) => c && c.name) || {});
		const sorted = this._cs_sorted("packed", queue, {
			order: (se) => first_dc(se).name || se.name,
			date: (se) => se.custom_status_since || se.creation || "",
			status: (se) => first_dc(se).packing_status || se.custom_status || "",
			src: (se) => label(se.from_warehouse),
			dst: (se) => label(se.to_warehouse),
			qty: (se) => Number(se.total_qty || 0),
			boxes: (se) => Number(se.box_count || 0),
			weight: (se) => Number(se.weight_kg || 0),
			age: (se) => Number(se.age_minutes != null ? se.age_minutes : (se.age_hours || 0) * 60),
		});
		const page = this._page_slice("packed", sorted);
		const rows = page.rows.map((se) => {
			const nm = frappe.utils.escape_html(se.name);
			const total_qty = Number(se.total_qty || 0);
			const box_count = Number(se.box_count || 0);
			const weight    = Number(se.weight_kg || 0);

			let age_cls = "lcc-sev-low";
			if (se.age_hours != null) {
				if (se.age_hours >= 48) age_cls = "lcc-sev-critical";
				else if (se.age_hours >= 24) age_cls = "lcc-sev-high";
				else if (se.age_hours >= 8) age_cls = "lcc-sev-medium";
			}
			const age = this._fmt_age(se.age_minutes, se.age_hours);

			const since = se.custom_status_since || se.creation;
			// One row per delivery challan. The challan is the document that
			// travels with the carton, so that is the number the loader is
			// holding when they look at this screen — the column is headed
			// "Order ID" because that is what they call it. A transfer sent in
			// batches carries one challan per dispatch; each row has its own
			// packing status, qty, boxes and weight (every box names the
			// challan it was packed against).
			//
			// A transfer's challans leave on one manifest together, so ticking
			// one ticks its siblings (see the row-check handler), and a
			// transfer with a dispatch still being boxed is listed but cannot
			// be ticked until that one is Packed too.
			const ready = se.ready !== false;
			const waiting = (se.waiting_for || []).map((n) => frappe.utils.escape_html(n)).join(", ");
			const challans = (se.delivery_challan_rows || []).filter((c) => c && c.name);
			// Boxes packed before they recorded their challan (older packing)
			// only add up per transfer; show that total once, on the first row.
			const boxes_per_challan = challans.some((c) => Number(c.box_count || 0));
			const lines = challans.length
				? challans.map((c, i) => ({
					challan: c.name,
					cell: frappe.utils.escape_html(c.name),
					title: frappe.utils.escape_html(`${c.name} · ${se.name}`),
					status: c.packing_status || "Not Packed",
					qty: Number(c.qty || 0),
					boxes: boxes_per_challan ? Number(c.box_count || 0) : (i === 0 ? box_count : 0),
					weight: boxes_per_challan ? Number(c.weight_kg || 0) : (i === 0 ? weight : 0),
				}))
				: [{
					cell: `<span class="text-muted">${__("Not raised")}</span>`,
					title: frappe.utils.escape_html(`${__("No delivery challan")} · ${se.name}`),
					status: se.custom_status || "Packed",
					qty: total_qty,
					boxes: box_count,
					weight: weight,
				}];
			const pill = { "Packed": "green", "Partially Packed": "yellow", "Not Packed": "orange" };
			return lines.map((line) => {
				// A challan can go once IT is packed; a transfer with no challan
				// (older data) only when the whole transfer is.
				const can = line.challan ? line.status === "Packed" : ready;
				return `<tr class="${can ? "" : "text-muted"}">
				<td><input type="checkbox" class="lcc-pack-row-check" data-name="${nm}"
					data-challan="${line.challan ? frappe.utils.escape_html(line.challan) : ""}"
					${can ? "" : `disabled title="${__("Not packed yet")}"`}></td>
				<td title="${line.title}">${line.cell}</td>
				<td>${since ? frappe.datetime.str_to_user(since) : "—"}</td>
				<td><span class="indicator-pill ${pill[line.status] || "gray"}"><span>${frappe.utils.escape_html(__(line.status))}</span></span>
					${ready || line.challan ? "" : `<div class="lcc-muted" style="font-size:11px;margin-top:2px">${__("Waiting for {0}", [waiting])}</div>`}</td>
				<td>${window.ch_wh_label_html ? ch_wh_label_html(se.from_warehouse, "—") : frappe.utils.escape_html(se.from_warehouse || "—")}</td>
				<td>${window.ch_wh_label_html ? ch_wh_label_html(se.to_warehouse, "—") : frappe.utils.escape_html(se.to_warehouse || "—")}</td>
				<td class="tr">${line.qty}</td>
				<td class="tr">${line.boxes}</td>
				<td class="tr">${line.weight ? line.weight.toFixed(1) + " kg" : "—"}</td>
				<td><span class="lcc-sev ${age_cls}">${age}</span></td>
			</tr>`;
			}).join("");
		}).join("");

		$b.html(`
			<div class="lcc-table-wrap"><table class="lcc-table lcc-pack-table">
				<thead><tr>
					<th style="width:32px"><input type="checkbox" class="lcc-pack-select-all"></th>
					${this._cs_sort_th("packed", __("Order ID"), "order")}
					${this._cs_sort_th("packed", __("Date"), "date")}
					${this._cs_sort_th("packed", __("Status"), "status")}
					${this._cs_sort_th("packed", __("Source Warehouse"), "src")}
					${this._cs_sort_th("packed", __("Target Warehouse"), "dst")}
					${this._cs_sort_th("packed", __("Qty"), "qty", "tr")}
					${this._cs_sort_th("packed", __("Boxes"), "boxes", "tr")}
					${this._cs_sort_th("packed", __("Weight"), "weight", "tr")}
					${this._cs_sort_th("packed", __("Age"), "age")}
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>
			${this._pager_html("packed", page, queue.length)}
		`);
		this._bind_pager($b, "packed", () => this._pack_render());
		this._pack_update_create_btn();
	}

	_pack_update_create_btn() {
		const $r = this.$root;
		// Counted in challans — the rows ticked — not in transfers.
		const n = $r.find(".lcc-pack-row-check:checked").length;
		const $btn = $r.find(".lcc-pack-create-manifest-btn");
		$btn.prop("disabled", n === 0);
		$btn.html(`<i class="fa fa-plus"></i> ${__("Create Manifest")}${n ? ` (${n})` : ""}`);
		const total = $r.find(".lcc-pack-row-check:not(:disabled)").length;
		$r.find(".lcc-pack-select-all").prop("checked", total > 0 && n === total);
	}

	/* ── Create Manifest — from ticked rows in the Packed Order table ──
	 * Groups the checked, fully Packed, unlinked Stock Entries into a
	 * manifest — ticking here is also what advances them from Packed to
	 * Ready For Pickup (see group_stock_entries_into_manifest). Source/
	 * Destination Warehouse are NOT manually chosen — they're derived
	 * server-side from whichever Stock Entries get ticked. Goes through
	 * ch_erp15's group_stock_entries_into_manifest (loops the same
	 * _ensure_transfer_manifest_for_stock_entry used everywhere else this
	 * session), not ch_logistics' create_manifest — that one requires
	 * docstatus 1, which these Stock Entries never reach (direct Material
	 * Transfer submission is blocked; they stay Draft through their whole
	 * custom_status lifecycle). Route is no longer auto-tagged on the
	 * manifest — it's resolved on the CH Logistics Trip instead, once this
	 * manifest is attached to one.
	 */
	_pack_create_manifest() {
		const $r = this.$root;
		const $ticked = $r.find(".lcc-pack-row-check:checked");
		const challan_count = $ticked.length;
		const challans = $ticked.map((_, el) => $(el).data("challan")).get().filter(Boolean);
		// Older transfers with no challan go whole, by transfer.
		const names = [...new Set($ticked.filter((_, el) => !$(el).data("challan"))
			.map((_, el) => $(el).data("name")).get())];
		if (!challans.length && !names.length) {
			frappe.msgprint(__("Tick at least one delivery challan."));
			return;
		}
		// No exact-route pre-check here — a manifest can now carry a
		// multi-leg/chained route (e.g. Annanagar->Ashok Nagar plus
		// Ashok Nagar->Chennai Hub), which group_stock_entries_into_manifest
		// resolves server-side via zone matching on the manifest's own
		// Stops table. It still refuses (with a clear message) if the
		// ticked entries genuinely don't connect.
		frappe.call({
			method: "ch_erp15.ch_erp15.custom.stock_entry.group_challans_into_manifest",
			args: { challans: JSON.stringify(challans), stock_entries: JSON.stringify(names) },
			freeze: true,
		}).then((r) => {
			const manifest_name = r.message;
			// Submitting the manifest here (Draft → Packed at the manifest
			// level, a separate status from the Stock Entry's own
			// custom_status) is what used to require a second "Move to
			// Logistics" click on a manifest-level table. Chaining it onto
			// manifest creation means one action gets the shipment all the
			// way into Operations → Unassigned Manifests, ready for driver
			// assignment.
			// Status is server-managed: before_submit moves Draft → Packed.
			// Setting it here trips the lifecycle-field forgery guard.
			frappe.db.get_doc("CH Transfer Manifest", manifest_name).then((doc) => {
				return frappe.call({ method: "frappe.client.submit", args: { doc } });
			}).then(() => {
				frappe.show_alert({
					message: __("Manifest {0} created with {1} delivery challan(s) and moved to Operations.", [manifest_name, challan_count]),
					indicator: "green",
				}, 6);
				this._pack_load();
			}).catch((err) => {
				frappe.show_alert({
					message: __("Manifest {0} created, but moving it to Operations failed: {1}", [manifest_name, err && err.message ? err.message : __("see log")]),
					indicator: "orange",
				}, 8);
				this._pack_load();
			});
		});
	}

	// ══════════════════════════════════════════════════════════════
	// OPERATIONS MODE
	// ══════════════════════════════════════════════════════════════

	_ops_init() {
		// trip_date is set once when this page object is first constructed
		// (frappe reuses one instance per session) — if the tab is left open
		// across midnight, or the user just navigates back into Operations
		// on a later day, that stale date silently kept showing yesterday's
		// (or older) board until someone noticed and changed it by hand.
		// Snap forward to today whenever it's fallen behind; leave alone if
		// the user picked today or a future date deliberately.
		if (this.trip_date < frappe.datetime.get_today()) {
			this.trip_date = frappe.datetime.get_today();
		}
		const co = this.filters?.fields?.company?.get_value() || "";
		$("#lcc-content").html(`
			<div class="lcc-ops">
				<div class="lcc-ops-toolbar">
					<div class="lcc-ops-toolbar-left">
						<label class="lcc-ops-label">${__("Date")}:</label>
						<input type="date" class="form-control input-sm lcc-ops-date" value="${this.trip_date}">
						<label class="lcc-ops-label">${__("Days")}:</label>
						<select class="form-control input-sm lcc-ops-days">
							<option value="1" ${this.include_days===1?"selected":""}>1</option>
							<option value="3" ${this.include_days===3?"selected":""}>3</option>
							<option value="7" ${this.include_days===7?"selected":""}>7</option>
						</select>
						<button class="btn btn-sm btn-default lcc-ops-refresh-btn">
							<i class="fa fa-refresh"></i> ${__("Refresh")}
						</button>
						<button class="btn btn-sm btn-default lcc-ops-map-btn">
							<i class="fa fa-map-marker"></i> ${__("Map")}
						</button>
						<span class="lcc-ops-toolbar-divider"></span>
						<button class="btn btn-sm btn-primary lcc-ops-new-trip-btn" title="${__("Create a new trip manually")}">
							<i class="fa fa-plus"></i> ${__("New Trip")}
						</button>
						<button class="btn btn-sm btn-default lcc-ops-autoplan-btn" title="${__("Auto-plan trips from unassigned manifests")}">
							<i class="fa fa-magic"></i> ${__("Auto-plan")}
						</button>
					</div>
					<div class="lcc-ops-kpi-bar" id="lcc-ops-kpi-bar"></div>
				</div>

				<div class="lcc-ops-lifecycle" id="lcc-ops-lifecycle">
					<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading lifecycle…")}</div>
				</div>

				<div class="lcc-ops-board" id="lcc-ops-board">
					<div class="lcc-loading">${__("Loading…")}</div>
				</div>

				<div class="lcc-ops-map-wrap" id="lcc-ops-map-wrap" style="display:none">
					<div class="lcc-ops-map-info" id="lcc-ops-map-info"></div>
					<div id="lcc-ops-map" style="height:440px;border-radius:var(--lcc-radius);border:1px solid var(--lcc-border);"></div>
				</div>

				<div class="lcc-tabs lcc-ops-tabs">
					<button class="lcc-tab active" data-tab="manifests">
						<i class="fa fa-inbox"></i> ${__("Unassigned Manifests")}
						<span class="lcc-tab-badge" id="lcc-cnt-mf">0</span>
					</button>
					<button class="lcc-tab" data-tab="recalls">
						<i class="fa fa-undo"></i> ${__("Recalls")}
						<span class="lcc-tab-badge lcc-tab-badge-warn" id="lcc-cnt-rec">0</span>
					</button>
					<button class="lcc-tab" data-tab="pickup_pending">
						<i class="fa fa-clock-o"></i> ${__("Pending")}
						<span class="lcc-tab-badge lcc-tab-badge-warn" id="lcc-cnt-pend">0</span>
					</button>
					<button class="lcc-tab" data-tab="drivers">
						<i class="fa fa-id-card"></i> ${__("Drivers")}
						<span class="lcc-tab-badge" id="lcc-cnt-drv">0</span>
					</button>
				</div>
				<div class="lcc-ops-bottom" id="lcc-ops-bottom"></div>
			</div>
		`);
		this._ops_bind_events();
		this._ops_load();
	}

	_ops_bind_events() {
		// Delegated handlers live on the persistent $root — bind once, or every
		// visit to the Operations tab stacks another copy (double dialogs,
		// duplicate trip actions/prints). Same guard idiom as _pack_init.
		if (this._ops_events_bound) return;
		this._ops_events_bound = true;
		const $r = this.$root;
		$r.on("change", ".lcc-ops-date",    (e) => { this.trip_date = $(e.currentTarget).val(); this._ops_load(); });
		$r.on("change", ".lcc-ops-days",    (e) => { this.include_days = parseInt($(e.currentTarget).val()) || 1; this._ops_load(); });
		$r.on("click",  ".lcc-ops-refresh-btn", () => this._ops_load());
		$r.on("click",  ".lcc-ops-map-btn", () => this._ops_map_toggle());
		$r.on("click",  ".lcc-ops-new-trip-btn", () => this._dlg_new_trip());
		$r.on("click",  ".lcc-ops-autoplan-btn", () => this._dlg_auto_plan());
		$r.on("click",  ".lcc-empty-new-trip", () => this._dlg_new_trip());
		$r.on("click",  ".lcc-empty-autoplan", () => this._dlg_auto_plan());
		$r.on("click",  ".lcc-empty-show-manifests", () => {
			this.bottom_tab = "manifests";
			this.$root.find(".lcc-ops-tabs .lcc-tab").removeClass("active");
			this.$root.find('.lcc-ops-tabs .lcc-tab[data-tab="manifests"]').addClass("active");
			this._ops_render_bottom();
			const el = document.getElementById("lcc-ops-bottom");
			if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
		});
		$r.on("click",  ".lcc-new-trip-from-sel", () => this._ops_new_trip_from_selection());
		$r.on("click",  ".lcc-mf-cancel", (e) => {
			e.preventDefault(); e.stopPropagation();
			this._ops_cancel_manifest($(e.currentTarget).data("name"));
		});
		$r.on("click",  ".lcc-mf-shipments", (e) => {
			e.preventDefault(); e.stopPropagation();
			this._ops_manifest_shipments($(e.currentTarget).data("name"));
		});
		$r.on("click",  ".lcc-rej-approve", (e) =>
			this._ops_review_rejection($(e.currentTarget).data("name"), true));
		$r.on("click",  ".lcc-rej-ignore", (e) =>
			this._ops_review_rejection($(e.currentTarget).data("name"), false));

		$r.on("click", ".lcc-ops-tabs .lcc-tab", (e) => {
			this.bottom_tab = $(e.currentTarget).data("tab");
			$r.find(".lcc-ops-tabs .lcc-tab").removeClass("active");
			$(e.currentTarget).addClass("active");
			this._ops_render_bottom();
		});

		// Lifecycle stage strip — click a chip to filter the canvas.
		$r.on("click", ".lcc-ls-chip", (e) => {
			const stage = $(e.currentTarget).data("stage");
			this._ops_set_lifecycle_stage(stage);
		});

		$r.on("click",  ".lcc-trip-card",     (e) => this._ops_open_trip($(e.currentTarget).data("name")));
		$r.on("click",  ".lcc-side-close",    () => this._ops_close_side());
		$r.on("change", ".lcc-mf-check",      (e) => {
			const n = $(e.currentTarget).data("name");
			e.currentTarget.checked ? this.selected_manifests.add(n) : this.selected_manifests.delete(n);
			// Keep the header "select all" checkbox in sync with individual rows.
			const $rows = this.$root.find(".lcc-mf-check");
			this.$root.find(".lcc-mf-check-all").prop(
				"checked", $rows.length > 0 && $rows.length === $rows.filter(":checked").length
			);
			this._ops_update_attach();
		});
		$r.on("change", ".lcc-mf-check-all",  (e) => {
			const checked = e.currentTarget.checked;
			this.$root.find(".lcc-mf-check").prop("checked", checked).each((_, el) => {
				const n = $(el).data("name");
				checked ? this.selected_manifests.add(n) : this.selected_manifests.delete(n);
			});
			this._ops_update_attach();
		});
		$r.on("click",  ".lcc-attach-btn",    () => this._ops_attach());
		$r.on("click",  "#lcc-side-assign",   () => this._ops_assign_driver());
		$r.on("click",  "#lcc-side-start",    () => this._ops_start_trip());
		$r.on("click",  "#lcc-side-complete", () => this._ops_trip_action("trip_complete"));
		$r.on("click",  "#lcc-side-resequence", () => this._ops_trip_action("resequence_trip", _OPT));
		$r.on("click",  "#lcc-side-close-trip",()=> this._ops_trip_action("trip_close"));
		$r.on("click",  "#lcc-side-cancel",   () => this._ops_trip_action("trip_unassign"));
		$r.on("click",  "#lcc-side-cancel-trip", () => this._ops_cancel_trip());
		$r.on("click",  ".lcc-side-manual-status",
			(e) => this._ops_manual_status($(e.currentTarget).data("status")));
		$r.on("click",  "#lcc-side-recall-trip", () => this._ops_recall_trip());
		$r.on("click",  ".lcc-side-detach",   (e) => this._ops_detach_manifest($(e.currentTarget).data("name")));
		$r.on("click",  ".lcc-trip-link",     (e) => { e.preventDefault(); this._ops_open_trip($(e.currentTarget).data("name")); });

		// Per-row print actions for the Operations → Manifests panel.
		$r.on("click", ".lcc-mf-open", (e) => {
			// Ctrl / Cmd-click still opens the manifest form in a new tab.
			if (e.ctrlKey || e.metaKey) return;
			// The link carries the form's address, and Frappe's own handler on
			// <body> routes every such link — preventDefault alone does not
			// stop it, so the click must not reach it or the form opens over
			// the breakdown this is meant to show.
			e.preventDefault();
			e.stopPropagation();
			this._ops_show_manifest_items($(e.currentTarget).data("name"));
		});
		$r.on("click", ".lcc-mf-print-box",     (e) => { e.preventDefault(); this._ops_print_manifest($(e.currentTarget).data("name"), "CH Transfer Manifest Label"); });
		$r.on("click", ".lcc-mf-print-receipt", (e) => { e.preventDefault(); this._ops_print_manifest($(e.currentTarget).data("name"), "ePOD Transfer Manifest"); });
		$r.on("click", ".lcc-mf-print-ewb",     (e) => { e.preventDefault(); this._ops_print_ewaybills($(e.currentTarget).data("name")); });

		// Recalls inbox — confirm physical return + reverse stock.
		$r.on("click", ".lcc-rec-confirm",      (e) => { e.preventDefault(); this._ops_confirm_recall_return($(e.currentTarget).data("name")); });

		// Pending tab — take a shipment the driver has not accepted back to Packed Orders.
		$r.on("click", ".lcc-pend-remove", (e) => {
			e.preventDefault();
			const $b = $(e.currentTarget);
			this._ops_remove_pending($b.data("manifest"), $b.data("stock-entry"), $b.data("label"));
		});
	}

	async _ops_load() {
		const co = this.filters?.fields?.company?.get_value() || undefined;
		const args_board = { trip_date: this.trip_date, include_days: this.include_days };
		if (co) args_board.company = co;

		// The exception inbox was dropped from this console, and with it the
		// query behind it: a trip's own exceptions still show on its side
		// panel, read from the board payload it already carries.
		const [b, u, d, rc, lc, pp] = await Promise.all([
			frappe.call({ method: _LCC + "ops_board",              args: args_board }),
			frappe.call({ method: _LCC + "ops_unassigned_manifests", args: { limit: 0 } }),
			frappe.call({ method: _LCC + "ops_drivers_available" }),
			frappe.call({ method: _LCC + "ops_recall_inbox",        args: { limit: 100 } }),
			frappe.call({ method: _LCC + "ops_lifecycle_counts",    args: args_board }),
			frappe.call({ method: _LCC + "ops_pickup_pending",      args: co ? { company: co } : {} }),
		]);

		this.board      = b.message || { buckets: {}, totals: {} };
		this.unassigned = u.message || [];
		this.drivers    = d.message || [];
		this.recalls    = rc.message || [];
		this.pickup_pending = pp.message || [];
		this.lifecycle  = lc.message || { stages: [] };
		this.selected_manifests.clear();

		this._ops_render_lifecycle();
		this._ops_render_board();
		this._ops_render_kpi();
		this._ops_render_bottom();
		if (this.mode === "mf_pickup") { this._reg_render_pickup(); return; }
		if (this.active_trip) this._ops_open_trip(this.active_trip);
	}

	/* ── Lifecycle strip (Manifest → Packing → Trip → In Transit → Delivered) ── */

	_ops_render_lifecycle() {
		const stages = (this.lifecycle && this.lifecycle.stages) || [];
		const active = this.lifecycle_stage || "";
		const $w = $("#lcc-ops-lifecycle").empty();
		if (!stages.length) {
			$w.html(`<div class="lcc-empty">${__("Lifecycle counters unavailable.")}</div>`);
			return;
		}
		const chips = stages.map((s, idx) => {
			const sec = (s.secondary_count != null)
				? `<span class="lcc-ls-sec" title="${__("shipments in this stage")}">${s.secondary_count}</span>`
				: "";
			const arrow = (idx < stages.length - 1)
				? `<i class="fa fa-angle-right lcc-ls-arrow"></i>`
				: "";
			return `
				<button class="lcc-ls-chip lcc-ls-${s.key} ${active === s.key ? "active" : ""}"
				        data-stage="${s.key}" title="${frappe.utils.escape_html(s.hint || "")}">
					<i class="fa ${s.icon || "fa-circle"}"></i>
					<span class="lcc-ls-label">${frappe.utils.escape_html(s.label)}</span>
					<span class="lcc-ls-count">${s.count}</span>
					${sec}
				</button>
				${arrow}
			`;
		}).join("");
		$w.html(`<div class="lcc-ls-rail">${chips}</div>`);
	}

	_ops_set_lifecycle_stage(stage) {
		// Toggle off if the same chip is clicked twice — a familiar pattern from
		// every dispatcher cockpit (SAP TM Freight Order Monitor, Manhattan
		// Active TMS): clicking the active stage clears the filter.
		this.lifecycle_stage = (this.lifecycle_stage === stage) ? null : stage;
		this._ops_render_lifecycle();

		// Map stage → bottom-tab + Kanban column behaviour.
		//   draft / packed  → focus the Unassigned Manifests panel
		//   planned         → highlight Draft+Assigned columns
		//   in_transit      → highlight Started column
		//   delivered       → highlight Completed+Closed columns
		const board_focus_map = {
			planned:    ["Draft", "Assigned"],
			in_transit: ["Started"],
			delivered:  ["Completed", "Closed"],
		};
		this._ops_board_focus = board_focus_map[this.lifecycle_stage] || null;
		this._ops_manifest_status_filter = (
			this.lifecycle_stage === "draft"  ? "Draft"  :
			this.lifecycle_stage === "packed" ? "Packed" : null
		);

		// Re-render the trip board so the column highlight matches.
		this._ops_render_board();

		if (this.lifecycle_stage === "draft" || this.lifecycle_stage === "packed") {
			this.bottom_tab = "manifests";
			this.$root.find(".lcc-ops-tabs .lcc-tab").removeClass("active");
			this.$root.find('.lcc-ops-tabs .lcc-tab[data-tab="manifests"]').addClass("active");
			this._ops_render_bottom();
			const el = document.getElementById("lcc-ops-bottom");
			if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
		} else {
			this._ops_render_bottom();
		}
	}

	/* ── KPI bar ──────────────────────────────────────────────── */

	_ops_render_kpi() {
		const t = this.board.totals || {};
		const total = Object.values(t).reduce((a, b) => a + b, 0);
		$("#lcc-ops-kpi-bar").html(`
			<span class="lcc-ops-kpi-total"><b>${total}</b> ${__("trips")}</span>
			<span class="lcc-ops-kpip lcc-kpip-started">${t.Started || 0} ${__("active")}</span>
			<span class="lcc-ops-kpip lcc-kpip-assigned">${t.Assigned || 0} ${__("assigned")}</span>
			<span class="lcc-ops-kpip lcc-kpip-draft">${t.Draft || 0} ${__("draft")}</span>
		`);
	}

	/* ── Trip board ───────────────────────────────────────────── */

	_ops_render_board() {
		const ORDER = ["Draft","Assigned","Started","Completed","Closed","Cancelled"];
		const buckets = this.board.buckets || {};
		const $board = $("#lcc-ops-board").empty();

		if (!ORDER.some((s) => (buckets[s] || []).length > 0)) {
			const unassigned_n = (this.unassigned || []).length;
			const hint = unassigned_n > 0
				? `<div class="lcc-empty-hint">
						<i class="fa fa-info-circle"></i>
						${__("{0} unassigned manifest(s) are waiting to be dispatched.", [`<b>${unassigned_n}</b>`])}
						<button class="lcc-link-btn lcc-empty-show-manifests">${__("View manifests")} <i class="fa fa-arrow-down"></i></button>
					</div>`
				: "";
			$board.html(`
				<div class="lcc-empty lcc-empty-state">
					<i class="fa fa-truck"></i>
					<div class="lcc-empty-title">${__("No trips scheduled for this period")}</div>
					<div class="lcc-empty-sub">${__("Start by creating a trip manually, or auto-plan trips from unassigned manifests.")}</div>
					<div class="lcc-empty-actions">
						<button class="btn btn-primary lcc-empty-new-trip">
							<i class="fa fa-plus"></i> ${__("Create Trip")}
						</button>
						<button class="btn btn-default lcc-empty-autoplan">
							<i class="fa fa-magic"></i> ${__("Auto-plan from Manifests")}
						</button>
					</div>
					${hint}
				</div>`);
			return;
		}
		ORDER.forEach((status) => {
			const trips = buckets[status] || [];
			if (!trips.length && ["Closed","Cancelled"].includes(status)) return;
			const focus = this._ops_board_focus;
			const dim = focus && !focus.includes(status) ? " is-dim" : "";
			const hi  = focus &&  focus.includes(status) ? " is-focus" : "";
			// A lane with 78 trips on it made the board a scrolling wall, so
			// each lane pages like the tables do — the count in the header is
			// still the whole lane.
			const key = `board:${status}`;
			const page = this._page_slice(key, trips);
			const $col = $(`
				<div class="lcc-ops-col${dim}${hi}">
					<div class="lcc-ops-col-head lcc-ops-s-${status.toLowerCase().replace(/ /g,"-")}">
						${__(status)} <span class="lcc-ops-col-cnt">${trips.length}</span>
					</div>
					<div class="lcc-ops-col-body"></div>
					${this._pager_html(key, page, trips.length)}
				</div>`).appendTo($board);
			page.rows.forEach((t) => $col.find(".lcc-ops-col-body").append(this._trip_card(t)));
			this._bind_pager($col, key, () => this._ops_render_board());
		});
	}

	_trip_card(t) {
		const dir = t.direction === "Reverse" ? "↩" : t.direction === "Mixed" ? "↔" : "→";
		const start = t.planned_start ? frappe.datetime.str_to_user(t.planned_start) : "—";
		const exc_badge = (t.open_exceptions || 0) > 0
			? `<span class="lcc-card-exc ${t.critical_exceptions ? "is-crit" : ""}"><i class="fa fa-exclamation-triangle"></i> ${t.open_exceptions}</span>` : "";
		return `<div class="lcc-trip-card" data-name="${frappe.utils.escape_html(t.name)}">
			<div class="lcc-card-top">
				<span class="lcc-card-name">${dir} ${frappe.utils.escape_html(t.name)}</span>
				${exc_badge}
			</div>
			<div class="lcc-card-meta">
				<div><i class="fa fa-user"></i> ${frappe.utils.escape_html(t.driver_name || __("Unassigned"))}</div>
				<div><i class="fa fa-truck"></i> ${frappe.utils.escape_html(t.vehicle_number || "—")}</div>
				<div><i class="fa fa-clock-o"></i> ${start}</div>
				<div><i class="fa fa-cube"></i> ${t.total_shipments || 0} ${__("shipments")}</div>
			</div>
		</div>`;
	}

	/* ── Bottom tabs ──────────────────────────────────────────── */

	_ops_render_bottom() {
		const rec_cnt = (this.recalls || []).length;
		const pend_cnt = (this.pickup_pending || []).length;
		$("#lcc-cnt-mf").text(this.unassigned.length);
		$("#lcc-cnt-rec").text(rec_cnt).toggleClass("has-items", rec_cnt > 0);
		$("#lcc-cnt-pend").text(pend_cnt).toggleClass("has-items", pend_cnt > 0);
		$("#lcc-cnt-drv").text(this.drivers.length);

		const $b = $("#lcc-ops-bottom").empty();
		if (this.bottom_tab === "manifests")  return this._ops_render_manifests($b);
		if (this.bottom_tab === "recalls")    return this._ops_render_recalls($b);
		if (this.bottom_tab === "pickup_pending") return this._ops_render_pickup_pending($b);
		if (this.bottom_tab === "drivers")    return this._ops_render_drivers($b);
	}

	// ── Paging ────────────────────────────────────────────────────
	// Twenty rows at a time. The lists these serve have no server cap any
	// more, so without paging a dispatcher scrolled past hundreds of rows —
	// and with a cap, anything packed after it simply never appeared.
	// Twenty rows suit a full-width table; a Kanban lane is a narrow column,
	// so ten cards is as much as fits without scrolling the board.
	_page_size(key) {
		if (key === "packed" || key === "register" || key === "trips") return 100;   // 100 a page
		return String(key).startsWith("board:") ? 10 : 20;
	}

	_page_slice(key, list) {
		this._pages = this._pages || {};
		const size = this._page_size(key);
		const pages = Math.max(Math.ceil(list.length / size), 1);
		const page = Math.min(Math.max(this._pages[key] || 1, 1), pages);
		this._pages[key] = page;
		return { page, pages, size, rows: list.slice((page - 1) * size, page * size) };
	}

	// Numbered pager: First · Previous · 1 2 3 4 5 … 271 · Next · Last, plus a
	// "Go to page" box. Shared by the in-browser tables (_pager_html) and the
	// server-paged Transfer Request Approved / Stock Outward list.
	_numbered_pager_html(key, page, pages, from, to, total) {
		const btn = (label, go, opts = {}) => `<button class="lcc-np-btn ${opts.active ? "active" : ""}"
			${opts.disabled ? "disabled" : `data-go="${go}"`}>${label}</button>`;
		// A window of five around the current page, with the first and last
		// always reachable and "…" for the gaps.
		let lo = Math.max(1, page - 2);
		let hi = Math.min(pages, lo + 4);
		lo = Math.max(1, hi - 4);
		const nums = [];
		if (lo > 1) {
			nums.push(btn("1", 1));
			if (lo > 2) nums.push(`<span class="lcc-np-gap">…</span>`);
		}
		for (let n = lo; n <= hi; n++) nums.push(btn(String(n), n, { active: n === page }));
		if (hi < pages) {
			if (hi < pages - 1) nums.push(`<span class="lcc-np-gap">…</span>`);
			nums.push(btn(String(pages), pages));
		}
		return `<div class="lcc-np" data-pager="${key}" data-pages="${pages}">
			<span class="lcc-muted lcc-np-info">${__("{0}–{1} of {2}", [from, to, total])}</span>
			<div class="lcc-np-group">
				${btn(__("First"), 1, { disabled: page <= 1 })}
				${btn(__("Previous"), page - 1, { disabled: page <= 1 })}
				${nums.join("")}
				${btn(__("Next"), page + 1, { disabled: page >= pages })}
				${btn(__("Last"), pages, { disabled: page >= pages })}
			</div>
			<span class="lcc-np-jump">
				${__("Go to page")}
				<input type="number" class="form-control input-xs lcc-np-input" min="1" max="${pages}" value="${page}">
				<button class="btn btn-xs btn-default lcc-np-go">${__("Go")}</button>
			</span>
		</div>`;
	}

	// The page number a pager click or "Go to page" asks for, kept inside 1…pages.
	_pager_target($el) {
		const $np = $el.closest(".lcc-np");
		const pages = parseInt($np.data("pages")) || 1;
		const raw = $el.is("[data-go]") ? $el.data("go") : $np.find(".lcc-np-input").val();
		const n = parseInt(raw);
		return Number.isFinite(n) ? Math.min(Math.max(n, 1), pages) : null;
	}

	_pager_html(key, info, total) {
		if (total <= info.size) return "";
		const from = (info.page - 1) * info.size + 1;
		const to = Math.min(info.page * info.size, total);
		return this._numbered_pager_html(key, info.page, info.pages, from, to, total);
	}

	_bind_pager($scope, key, rerender) {
		const go = (e) => {
			const n = this._pager_target($(e.currentTarget));
			if (!n) return;
			this._pages[key] = n;
			rerender();
		};
		$scope.off(".lccpager")
			.on("click.lccpager", `[data-pager="${key}"] [data-go], [data-pager="${key}"] .lcc-np-go`, go)
			.on("keydown.lccpager", `[data-pager="${key}"] .lcc-np-input`, (e) => {
				if (e.key === "Enter") go(e);
			});
	}

	_ops_render_manifests($b) {
		// Optional lifecycle filter: when a stage chip (draft / packed) is
		// active, narrow the panel to just that status. Otherwise show
		// everything attachable, like before.
		const want = this._ops_manifest_status_filter;
		let list = want
			? (this.unassigned || []).filter((m) => (m.status || "").toLowerCase() === want.toLowerCase())
			: (this.unassigned || []);
		// The Manifest tab's filter bar (Operations has none).
		if (this.mode === "mf_pickup") {
			const hd = this._header_dates();
			list = this._flt_apply("manifest", list).filter((m) => {
				const day = String(m.creation || "").slice(0, 10);
				return (!hd.from_date || day >= hd.from_date) && (!hd.to_date || day <= hd.to_date);
			});
			if (!list.length && (this.unassigned || []).length) {
				$b.html(`<div class="lcc-empty"><i class="fa fa-search"></i> ${__("No manifest matches the filters.")}</div>`);
				return;
			}
		}
		if (!list.length) {
			const empty_msg = want
				? __("No {0} manifests right now.", [want])
				: __("No manifests waiting to be attached. (Only submitted Packed manifests appear here — Drafts must be submitted first; anything already in motion is hidden.)");
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${empty_msg}</div>`);
			return;
		}
		// Status pill colours — mirror form indicator map.
		const STATUS_COLOR = { "Draft": "gray", "Packed": "blue" };
		// "Packed" is the manifest's real status (and the filter value below
		// still uses it), but at this level it reads as "Manifest Created"
		// instead — "Packed" already means something else on the Stock
		// Entry side, and re-using it here for a submitted manifest was
		// confusing the two.
		const STATUS_LABEL = { "Packed": __("Manifest Created") };
		const page = this._page_slice("manifests", list);
		const rows = page.rows.map((m) => {
			const color = STATUS_COLOR[m.status] || "gray";
			const status = frappe.utils.escape_html(STATUS_LABEL[m.status] || m.status || "—");
			const nm = encodeURIComponent(m.name);
			return `<tr>
			<td><input type="checkbox" class="lcc-mf-check" data-name="${m.name}"></td>
			<td style="white-space:nowrap"><a href="/app/ch-transfer-manifest/${nm}" class="lcc-mf-open" data-opens-in-place="1" data-name="${frappe.utils.escape_html(m.name)}">${frappe.utils.escape_html(m.name)}</a></td>
			<td style="white-space:nowrap"><span class="indicator-pill ${color}" style="white-space:nowrap">${status}</span></td>
			<td style="white-space:nowrap">${frappe.utils.escape_html(m.direction || "—")}</td>
			<td style="white-space:nowrap"><span class="lcc-prio lcc-prio-${(m.shipment_priority || "Normal").toLowerCase()}">${m.shipment_priority || "Normal"}</span></td>
			<td>${window.ch_wh_label_html ? ch_wh_label_html(m.source_warehouse, "—") : frappe.utils.escape_html(m.source_warehouse || "—")}</td>
			<td>${lcc_destinations_html(m)}</td>
			<td class="tr">${m.dc_count || 0}</td>
			<td class="tr">${m.box_count || 0}</td>
			<td style="white-space:nowrap">${frappe.datetime.str_to_user(m.creation)}</td>
			<td class="text-right" style="white-space:nowrap">
				<button class="btn btn-xs btn-default lcc-mf-shipments" data-name="${frappe.utils.escape_html(m.name)}">
					${__("Shipments")}</button>
				<button class="btn btn-xs btn-danger lcc-mf-cancel" data-name="${frappe.utils.escape_html(m.name)}">
					${__("Cancel")}</button>
			</td>
		</tr>`;
		}).join("");

		$b.html(`
			<div class="lcc-ops-bar">
				<button class="btn btn-sm btn-primary lcc-attach-btn" disabled>
					<i class="fa fa-link"></i> ${__("Attach to Trip")} (<span class="lcc-attach-n">0</span>)
				</button>
				<button class="btn btn-sm btn-default lcc-new-trip-from-sel" disabled>
					<i class="fa fa-plus"></i> ${__("Create Trip from Selected")}
				</button>
				<span class="lcc-ops-bar-spacer"></span>
				<span class="lcc-muted lcc-ops-bar-hint">${__("Only submitted (Packed) manifests are listed — submit Draft manifests to see them here. Open a manifest to print its box label, transfer receipt, or e-Way Bill before dispatch.")}</span>
			</div>
			<div class="lcc-table-wrap"><table class="lcc-table">
				<thead><tr>
					<th style="width:32px"><input type="checkbox" class="lcc-mf-check-all"></th>
					<th>${__("Manifest")}</th>
					<th>${__("Status")}</th>
					<th>${__("Dir")}</th><th>${__("Priority")}</th>
					<th>${__("From")}</th><th>${__("To")}</th>
					<th class="tr">${__("No. of DCs")}</th>
					<th class="tr">${__("Boxes")}</th><th>${__("Created")}</th>
					<th class="text-right">${__("Actions")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>
			${this._pager_html("manifests", page, list.length)}`);
		this._bind_pager($b, "manifests", () => this._ops_render_manifests($b));
	}

	/* ── Manifest drill-down ──────────────────────────────────────
	 * Clicking a Manifest ID in the Operations table used to jump straight
	 * to the full desk form. Shows the underlying Stock Entry / Item / Qty
	 * / Status breakdown in a lightweight dialog instead. Clicking an Order
	 * Id inside that dialog drills one level deeper into per-IMEI detail
	 * (_ops_show_order_serials) — status there is read fresh from the Stock
	 * Entry each time, and prefers whatever serial list is most current
	 * (see get_stock_entry_serial_detail), so a delivery person's scan at
	 * receipt shows up on next open without any separate refresh step.
	 */
	_status_color(status) {
		const STATUS_COLOR = {
			"Draft": "gray", "Pending With Goods": "orange", "Partially Packed": "yellow",
			"Packed": "teal", "Ready For Pickup": "teal", "Assigned": "blue",
			"Ready For Receive": "cyan", "In Transit": "yellow", "Receive At Transit": "purple",
			"Partially Transferred": "orange", "Transferred": "green", "Force Closed": "gray",
			"Rejected": "red",
		};
		return STATUS_COLOR[status] || "gray";
	}

	// Same relabel as the Stock Entry list view — the underlying value still
	// drives pickup/manifest logic under the hood, but reads with driver-
	// facing wording everywhere it's shown to a user, matching the Delivery
	// App's Pickup Pending / Delivery Pending / Delivered tabs.
	_status_label(status) {
		const RELABEL = {
			"Ready For Pickup": __("Manifest Created"),
			"Assigned": __("Pickup Pending"),
			"In Transit": __("Delivery Pending"),
			"Transferred": __("Delivered"),
		};
		return RELABEL[status] || __(status || "—");
	}

	async _ops_show_manifest_items(name) {
		// 3-stage drill-down: Stock Entry list (this dialog's initial view)
		// -> click one -> its Items -> click an item -> IMEIs
		// (_ops_show_order_serials, unchanged). Each stage replaces the
		// same dialog's content rather than opening a new dialog, with a
		// "Back" link to step out one level.
		const d = new frappe.ui.Dialog({
			title: __("Manifest {0}", [name]),
			size: "large",
			fields: [{ fieldname: "items_html", fieldtype: "HTML" }],
		});
		const $wrap = () => d.fields_dict.items_html.$wrapper;
		const esc = frappe.utils.escape_html;
		const loc = (wh) => window.ch_wh_label_html ? ch_wh_label_html(wh, "—") : esc(wh || "—");
		const date = (val) => val ? frappe.datetime.str_to_user(val) : "—";
		// The challan is what physically travels with the carton, so it is
		// the number to check a box against and the one this table leads
		// with. Clicking it opens what is inside, the way the transfer id
		// used to — a transfer sent in batches raises one challan per
		// dispatch, and every one of them opens the same shipment.
		//
		// A shipment with no challan still has to be openable, so it falls
		// back to naming its transfer rather than leaving a dead row.
		const challan = (se) => {
			const list = (se.delivery_challans || []).filter(Boolean);
			const open = (label) =>
				`<a href="#" class="lcc-se-open" data-se="${esc(se.stock_entry)}">${esc(label)}</a>`;
			if (!list.length) {
				return `${open(se.stock_entry)} <span class="text-muted small">${
					__("challan not raised")}</span>`;
			}
			return list.map(open).join("<br>");
		};

		$wrap().html(`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading…")}</div>`);
		d.show();

		let se_rows = [];
		let item_rows = [];

		const render_stock_entries = () => {
			d.set_title(__("Manifest {0}", [name]));
			if (!se_rows.length) {
				$wrap().html(`<div class="lcc-empty">${__("No shipments found on this manifest.")}</div>`);
				return;
			}
			const body = se_rows.map((se) => `<tr>
					<td>${challan(se)}</td>
					<td class="tr">${se.qty != null ? se.qty : "—"}</td>
					<td>${date(se.posting_date)}</td>
					<td>${loc(se.from_warehouse)}</td>
					<td>${loc(se.to_warehouse)}</td>
					<td><span class="indicator-pill ${this._status_color(se.status)}"><span>${esc(this._status_label(se.status))}</span></span></td>
				</tr>`).join("");
			$wrap().html(`
				<div class="lcc-table-wrap"><table class="lcc-table">
					<thead><tr>
						<th>${__("Delivery Challan")}</th>
						<th class="tr">${__("Qty")}</th>
						<th>${__("Date")}</th>
						<th>${__("From")}</th>
						<th>${__("To")}</th>
						<th>${__("Status")}</th>
					</tr></thead>
					<tbody>${body}</tbody>
				</table></div>`);
		};

		const render_items = (se) => {
			const clicked = (se.delivery_challans || []).filter(Boolean).join(", ")
				|| se.stock_entry;
			d.set_title(__("{0} — Items", [clicked]));
			const rows = item_rows.filter((r) => r.stock_entry === se.stock_entry);
			const back = `<div class="lcc-back" style="margin-bottom:8px">
				<a href="#" class="lcc-back-to-se"><i class="fa fa-arrow-left"></i> ${__("Back to Shipments")}</a>
			</div>`;
			if (!rows.length) {
				$wrap().html(`${back}<div class="lcc-empty">${__("No item detail found for this shipment.")}</div>`);
				return;
			}
			const body = rows.map((row) => `<tr>
					<td>
						<a href="#" class="lcc-item-open" data-order="${esc(se.stock_entry)}" data-label="${esc(clicked)}" data-item="${esc(row.item_code || "")}">
							${esc(row.item_name || row.item_code || "—")}
						</a>
					</td>
					<td class="tr">${row.qty != null ? row.qty : "—"}${row.uom ? " " + esc(row.uom) : ""}</td>
					<td>${date(se.posting_date)}</td>
					<td>${loc(se.from_warehouse)}</td>
					<td>${loc(se.to_warehouse)}</td>
					<td><span class="indicator-pill ${this._status_color(row.status)}"><span>${esc(this._status_label(row.status))}</span></span></td>
				</tr>`).join("");
			$wrap().html(`${back}
				<div class="lcc-table-wrap"><table class="lcc-table">
					<thead><tr>
						<th>${__("Item Name")}</th>
						<th class="tr">${__("Qty")}</th>
						<th>${__("Date")}</th>
						<th>${__("From")}</th>
						<th>${__("To")}</th>
						<th>${__("Status")}</th>
					</tr></thead>
					<tbody>${body}</tbody>
				</table></div>`);
		};

		$wrap().on("click", ".lcc-se-open", (e) => {
			e.preventDefault();
			const se_name = $(e.currentTarget).data("se");
			const se = se_rows.find((r) => r.stock_entry === se_name);
			if (se) render_items(se);
		});
		$wrap().on("click", ".lcc-back-to-se", (e) => {
			e.preventDefault();
			render_stock_entries();
		});
		d.$wrapper.on("click", ".lcc-item-open", (e) => {
			e.preventDefault();
			const $t = $(e.currentTarget);
			this._ops_show_order_serials($t.data("order"), $t.data("item"), $t.data("label"));
		});

		try {
			const [se_r, item_r] = await Promise.all([
				frappe.call({ method: _LCC + "get_manifest_stock_entries_bulk", args: { manifests: [name] } }),
				frappe.call({ method: _LCC + "get_manifest_items_bulk", args: { manifests: [name] } }),
			]);
			se_rows = (se_r.message && se_r.message[name]) || [];
			item_rows = (item_r.message && item_r.message[name]) || [];
			render_stock_entries();
		} catch (e) {
			$wrap().html(
				`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load manifest items.")}</div>`
			);
		}
	}

	async _ops_show_order_serials(order_id, item_code, label) {
		const d = new frappe.ui.Dialog({
			// Named by its Delivery Challan when opened from one, so all three
			// levels of the manifest drill-down carry the same number.
			title: __("Order {0}", [label || order_id]),
			size: "large",
			fields: [{ fieldname: "serials_html", fieldtype: "HTML" }],
		});
		d.fields_dict.serials_html.$wrapper.html(
			`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading…")}</div>`
		);
		d.show();
		try {
			const r = await frappe.call({
				method: _LCC + "get_stock_entry_serial_detail",
				args: { stock_entry: order_id, item_code: item_code || undefined },
			});
			const rows = r.message || [];
			if (!rows.length) {
				d.fields_dict.serials_html.$wrapper.html(
					`<div class="lcc-empty">${__("No IMEI detail found for this order.")}</div>`
				);
				return;
			}
			const body = rows.map((row) => `<tr>
					<td>${frappe.utils.escape_html(row.serial_no || "—")}</td>
					<td>${frappe.utils.escape_html(row.item_name || row.item_code || "—")}</td>
					<td class="tr">${row.qty != null ? row.qty : "—"}</td>
					<td><span class="indicator-pill ${this._status_color(row.status)}"><span>${frappe.utils.escape_html(this._status_label(row.status))}</span></span></td>
				</tr>`).join("");
			d.fields_dict.serials_html.$wrapper.html(`
				<div class="lcc-table-wrap"><table class="lcc-table">
					<thead><tr>
						<th>${__("IMEI")}</th>
						<th>${__("Item Name")}</th>
						<th class="tr">${__("Qty")}</th>
						<th>${__("Status")}</th>
					</tr></thead>
					<tbody>${body}</tbody>
				</table></div>
			`);
		} catch (e) {
			d.fields_dict.serials_html.$wrapper.html(
				`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load IMEI detail.")}</div>`
			);
		}
	}

	/* ── Recalls inbox ────────────────────────────────────────────
	 * Dispatcher worklist for in-flight transfer recalls (status =
	 * "Recall Initiated").  Equivalent to SAP TM's Returns Cockpit /
	 * Manhattan TMS's Returns Inbox: list every recalled manifest with
	 * its original trip + driver + vehicle + age clock, and let the
	 * dispatcher click "Confirm Return" once the goods physically arrive
	 * back at the source warehouse (which reverses the Stock Entries).
	 */
	// ── Undoing a manifest from here as well as from its own form ───────
	//
	// The same two actions the CH Transfer Manifest form offers under
	// Actions, on the same endpoints — dispatch works from this list, so
	// making them come here to find a manifest and then open it was the
	// long way round. Both undo the GROUPING only: the boxes stay packed,
	// the stock stays in transit, and the shipments return to Packed Orders.
	_ops_cancel_manifest(manifest) {
		const d = new frappe.ui.Dialog({
			title: __("Cancel manifest {0}", [manifest]),
			fields: [
				{
					fieldtype: "HTML",
					options: `<div class="alert alert-warning">${__(
						"Every shipment on this manifest goes back to Packed Orders, ready to be put on a different one. "
						+ "Nothing is unpacked and no stock moves — the goods stay in the transit warehouse. "
						+ "To send the goods back to the source instead, open the manifest and use Cancel &amp; Return Stock."
					)}</div>`,
				},
				...ch_mf_reason_fields(CH_MF_CANCEL_REASONS),
			],
			primary_action_label: __("Cancel Manifest"),
			primary_action: (vals) => {
				d.hide();
				frappe.call({
					method: _TMA + "release_manifest",
					args: { manifest, reason: ch_mf_reason_text(vals) },
					freeze: true,
					freeze_message: __("Returning shipments to Packed Orders…"),
				}).then((r) => {
					frappe.show_alert({
						message: (r && r.message && r.message.message) || __("Manifest cancelled"),
						indicator: "orange",
					}, 6);
					this._ops_load();
				});
			},
		});
		d.show();
	}

	_ops_manifest_shipments(manifest) {
		frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "CH Transfer Manifest Item",
				filters: { parent: manifest },
				fields: ["name", "stock_entry", "from_warehouse", "to_warehouse", "total_qty"],
				parent: "CH Transfer Manifest",
				limit_page_length: 0,
			},
		}).then((r) => {
			const rows = (r && r.message) || [];
			const esc = frappe.utils.escape_html;
			const d = new frappe.ui.Dialog({
				title: __("Shipments on {0}", [manifest]),
				size: "large",
				fields: [{ fieldtype: "HTML", fieldname: "list" }],
				// More packed shipments can join until the manifest is on a
				// trip or picked up — the server holds the same line.
				primary_action_label: __("Add Shipments"),
				primary_action: () => {
					d.hide();
					this._ops_manifest_add_shipments(manifest);
				},
			});
			d.fields_dict.list.$wrapper.html(`
				<p class="text-muted small">${__(
					"Removing a shipment takes it off this manifest and puts it back in Packed Orders. "
					+ "The last shipment cannot be removed — cancel the manifest instead."
				)}</p>
				<table class="table table-bordered table-condensed">
					<thead><tr><th>${__("Transfer")}</th><th>${__("Route")}</th>
						<th class="text-right">${__("Qty")}</th><th></th></tr></thead>
					<tbody>${rows.map((x) => `<tr>
						<td><a href="/app/stock-entry/${encodeURIComponent(x.stock_entry)}" target="_blank">${esc(x.stock_entry)}</a></td>
						<td class="text-muted small">${esc(x.from_warehouse || "—")} → ${esc(x.to_warehouse || "—")}</td>
						<td class="text-right">${flt(x.total_qty) || 0}</td>
						<td class="text-right"><button class="btn btn-xs btn-danger ch-mf-drop"
							data-se="${esc(x.stock_entry)}" ${rows.length < 2 ? "disabled" : ""}>
							${__("Remove")}</button></td>
					</tr>`).join("")}</tbody>
				</table>`);
			d.$wrapper.find(".ch-mf-drop").on("click", (e) => {
				const stock_entry = $(e.currentTarget).data("se");
				frappe.prompt(
					ch_mf_reason_fields(CH_MF_REMOVE_REASONS),
					(vals) => {
						d.hide();
						frappe.call({
							method: _TMA + "remove_transfer_from_manifest",
							args: { manifest, stock_entry, reason: ch_mf_reason_text(vals) },
							freeze: true,
							freeze_message: __("Removing the shipment…"),
						}).then((res) => {
							frappe.show_alert({
								message: (res && res.message && res.message.message) || __("Shipment removed"),
								indicator: "orange",
							}, 6);
							this._ops_load();
						});
					},
					__("Remove {0}", [stock_entry]),
					__("Remove")
				);
			});
			d.show();
		});
	}

	// Packed shipments that can still join a manifest: fully packed, on no
	// manifest, leaving from a warehouse it already picks up at. One line per
	// delivery challan, as in Packed Orders.
	_ops_manifest_add_shipments(manifest) {
		frappe.call({
			method: _TMA + "addable_transfers",
			args: { manifest },
		}).then((r) => {
			const rows = (r && r.message) || [];
			const esc = frappe.utils.escape_html;
			const wh = (w) => (window.ch_wh_label ? ch_wh_label(w) : w) || "—";
			if (!rows.length) {
				frappe.msgprint({
					title: __("Nothing to add"),
					indicator: "orange",
					message: __("No packed shipment is waiting at a pickup point of {0}. Pack the challan and Mark as Packed first.", [manifest]),
				});
				return;
			}
			const lines = rows.flatMap((se) => {
				const challans = (se.delivery_challan_rows || []).filter((c) => c && c.name);
				return (challans.length ? challans : [{ name: "", qty: se.total_qty, box_count: se.box_count }])
					.map((c) => `<tr>
						<td><input type="checkbox" class="ch-mf-add" data-se="${esc(se.name)}"></td>
						<td>${esc(c.name || "—")}<div class="text-muted small">${esc(se.name)}</div></td>
						<td class="small">${esc(wh(se.from_warehouse))} → ${esc(wh(se.to_warehouse))}</td>
						<td class="text-right">${flt(c.qty) || 0}</td>
						<td class="text-right">${cint(c.box_count) || 0}</td>
					</tr>`);
			}).join("");
			const d = new frappe.ui.Dialog({
				title: __("Add Shipments to {0}", [manifest]),
				size: "large",
				fields: [{ fieldtype: "HTML", fieldname: "list" }],
				primary_action_label: __("Add"),
				primary_action: () => {
					const picked = [...new Set(d.$wrapper.find(".ch-mf-add:checked")
						.map((_, el) => $(el).data("se")).get())];
					if (!picked.length) {
						frappe.msgprint(__("Tick at least one delivery challan."));
						return;
					}
					frappe.call({
						method: _TMA + "add_transfers_to_manifest",
						args: { manifest, stock_entries: JSON.stringify(picked) },
						freeze: true,
						freeze_message: __("Adding shipments…"),
					}).then((res) => {
						d.hide();
						frappe.show_alert({
							message: (res && res.message && res.message.message) || __("Shipments added"),
							indicator: "green",
						}, 6);
						this._ops_load();
					});
				},
			});
			d.fields_dict.list.$wrapper.html(`
				<p class="text-muted small">${__(
					"Packed delivery challans not on any manifest, leaving from a pickup point of this manifest. "
					+ "A new destination becomes a new drop on the route.")}</p>
				<table class="table table-bordered table-condensed">
					<thead><tr><th></th><th>${__("Delivery Challan")}</th><th>${__("Route")}</th>
						<th class="text-right">${__("Qty")}</th><th class="text-right">${__("Boxes")}</th></tr></thead>
					<tbody>${lines}</tbody>
				</table>`);
			// A transfer's challans travel together, so they tick together.
			d.$wrapper.on("change", ".ch-mf-add", (e) => {
				const se = $(e.currentTarget).data("se");
				d.$wrapper.find(".ch-mf-add").filter((_, el) => $(el).data("se") === se)
					.prop("checked", e.currentTarget.checked);
			});
			d.show();
		});
	}

	// ── Pending: given to a driver, not yet accepted ─────────────────────
	//
	// Each driver's Pickup Pending list from the Delivery App, one row per
	// shipment. A row drops off once the driver taps Accept Shipment.
	_ops_render_pickup_pending($b) {
		const rows = this.pickup_pending || [];
		if (!rows.length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${
				__("Every assigned shipment has been accepted by its driver.")}</div>`);
			return;
		}
		const esc = frappe.utils.escape_html;
		const place = (wh) => (window.ch_wh_label_html ? ch_wh_label_html(wh, "—") : esc(wh || "—"));
		const by_driver = new Map();
		for (const r of rows) {
			const key = r.driver_name || r.driver || __("No driver");
			if (!by_driver.has(key)) by_driver.set(key, []);
			by_driver.get(key).push(r);
		}
		const groups = [...by_driver.entries()].map(([driver, list]) => {
			const body = list.map((r) => `<tr>
				<td>${r.delivery_challan
					? esc(r.delivery_challan)
					: `<a href="/app/stock-entry/${encodeURIComponent(r.stock_entry)}" target="_blank">${esc(r.stock_entry)}</a>`}</td>
				<td>${place(r.from_warehouse)} → ${place(r.to_warehouse)}</td>
				<td>${r.trip ? `<i class="fa fa-truck"></i> ${esc(r.trip)}` : "—"}</td>
				<td class="text-right">${flt(r.total_qty)}</td>
				<td class="lcc-muted">${r.assigned_on ? frappe.datetime.str_to_user(r.assigned_on) : "—"}</td>
				<td class="text-right">
					<button class="btn btn-xs btn-default lcc-pend-remove" data-manifest="${esc(r.manifest)}"
						data-stock-entry="${esc(r.stock_entry)}" data-label="${esc(r.delivery_challan || r.stock_entry)}">
						<i class="fa fa-undo"></i> ${__("Remove")}</button>
				</td>
			</tr>`).join("");
			return `
				<div class="lcc-ops-bar" style="margin-top:10px">
					<b><i class="fa fa-user"></i> ${esc(driver)}</b>
					<span class="lcc-tab-badge lcc-tab-badge-warn has-items">${list.length}</span>
				</div>
				<div class="lcc-table-wrap"><table class="lcc-table">
					<thead><tr>
						<th>${__("Delivery Challan")}</th>
						<th>${__("From → To")}</th>
						<th>${__("Trip")}</th>
						<th class="text-right">${__("Qty")}</th>
						<th>${__("Assigned")}</th>
						<th></th>
					</tr></thead>
					<tbody>${body}</tbody>
				</table></div>`;
		}).join("");
		$b.html(`
			<div class="lcc-ops-bar">
				<span class="lcc-muted">${__(
					"Shipments each driver still has to accept in the Delivery App. "
					+ "A shipment leaves this list as soon as the driver taps Accept Shipment.")}</span>
			</div>
			${groups}`);
	}

	_ops_remove_pending(manifest, stock_entry, label) {
		const d = new frappe.ui.Dialog({
			title: __("Remove {0}", [label]),
			fields: [
				{
					fieldtype: "HTML",
					options: `<div class="alert alert-warning">${__(
						"The shipment comes off the driver's list and goes back to Packed Orders, "
						+ "ready to go on another manifest. Nothing is packed again.")}</div>`,
				},
				...ch_mf_reason_fields(CH_MF_REMOVE_REASONS),
			],
			primary_action_label: __("Remove"),
			primary_action: (vals) => {
				d.hide();
				frappe.dom.freeze(__("Removing…"));
				frappe.call({
					method: _TMA + "remove_pending_shipment",
					args: { manifest, stock_entry, reason: ch_mf_reason_text(vals) },
				}).then((r) => {
					frappe.dom.unfreeze();
					frappe.show_alert({
						message: (r && r.message && r.message.message) || __("Back in Packed Orders"),
						indicator: "orange",
					}, 6);
					if (this.mode === "mf_transit") this._reg_load();
					else this._ops_load();
				}).catch(() => frappe.dom.unfreeze());
			},
		});
		d.show();
	}

	// ── Rejected: a driver said no, and somebody has to answer ──────────
	//
	// The goods are already back at the source warehouse by the time a row
	// lands here (the rejection reverses the transit movement), so both
	// answers move stock again — see rejection_api._put_the_goods_back.
	_ops_render_rejections($b) {
		const rows = this.rejections || [];
		if (!rows.length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${
				__("No shipments refused. A driver's rejection waits here until you accept it or send it back to them.")}</div>`);
			return;
		}
		const esc = frappe.utils.escape_html;
		const body = rows.map((r) => {
			const photos = [r.proof_image_1, r.proof_image_2].filter(Boolean).map((src) =>
				`<a href="${esc(src)}" target="_blank" title="${__("Open proof photo")}">
					<img src="${esc(src)}" style="width:38px;height:38px;object-fit:cover;border-radius:4px;
						border:1px solid var(--lcc-border);margin-right:4px"></a>`).join("");
			return `<tr>
				<td><a href="/app/ch-transfer-manifest/${encodeURIComponent(r.manifest)}" target="_blank">${esc(r.manifest)}</a>
					<div class="lcc-muted">${esc(r.source || "—")} → ${esc(r.destination || "—")}</div></td>
				<td>${esc(r.driver_name || r.driver || "—")}
					${r.trip ? `<div class="lcc-muted"><i class="fa fa-truck"></i> ${esc(r.trip)}</div>` : ""}</td>
				<td><span class="lcc-pill lcc-sev-high">${esc(r.rejection_reason || "—")}</span>
					${r.remarks ? `<div class="lcc-muted">${esc(r.remarks)}</div>` : ""}</td>
				<td>${photos || `<span class="text-muted">—</span>`}</td>
				<td class="lcc-muted">${r.rejected_on ? frappe.datetime.str_to_user(r.rejected_on) : "—"}</td>
				<td class="text-right">
					<button class="btn btn-xs btn-success lcc-rej-approve" data-name="${esc(r.name)}">
						<i class="fa fa-check"></i> ${__("Approve")}</button>
					<button class="btn btn-xs btn-default lcc-rej-ignore" data-name="${esc(r.name)}">
						<i class="fa fa-undo"></i> ${__("Ignore")}</button>
				</td>
			</tr>`;
		}).join("");
		$b.html(`
			<div class="lcc-ops-bar">
				<span class="lcc-muted">${__(
					"A driver refused these shipments and the goods went back to the source warehouse. "
					+ "Approve sends the shipment back to Unassigned Manifests as Packed for a different trip; "
					+ "Ignore puts it back on the same driver's list. Either way the stock moves into transit again.")}</span>
			</div>
			<div class="lcc-table-wrap"><table class="lcc-table">
				<thead><tr>
					<th>${__("Manifest")}</th>
					<th>${__("Driver")}</th>
					<th>${__("Reason")}</th>
					<th>${__("Proof")}</th>
					<th>${__("Rejected")}</th>
					<th class="text-right">${__("Decision")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table></div>`);
	}

	_ops_review_rejection(rejection, approve) {
		const d = new frappe.ui.Dialog({
			title: approve ? __("Approve rejection") : __("Send it back to the driver"),
			fields: [
				{
					fieldtype: "HTML",
					options: `<div class="alert alert-${approve ? "warning" : "info"}">${
						approve
							? __("The shipment goes back to Unassigned Manifests as Packed, with the driver and trip cleared, and its stock moves into the transit warehouse again — ready to go out on a different trip.")
							: __("The same shipment goes back to this driver's Pickup Pending list with your note on it, and its stock moves into the transit warehouse again.")
					}</div>`,
				},
				{
					fieldtype: "Small Text", fieldname: "notes",
					label: approve ? __("Note for the record") : __("What the driver should know"),
					reqd: approve ? 0 : 1,
				},
			],
			primary_action_label: approve ? __("Approve") : __("Send back"),
			primary_action: (vals) => {
				d.hide();
				frappe.dom.freeze(__("Moving the stock…"));
				frappe.call({
					method: _REJ + (approve ? "rejection_approve" : "rejection_ignore"),
					args: { rejection, notes: vals.notes },
				}).then((r) => {
					frappe.dom.unfreeze();
					const out = (r && r.message) || {};
					frappe.show_alert({
						message: approve
							? __("{0} is back in Unassigned Manifests", [out.manifest || ""])
							: __("{0} is back on the driver's list", [out.manifest || ""]),
						indicator: approve ? "orange" : "blue",
					}, 6);
					this._ops_load();
				}).catch(() => frappe.dom.unfreeze());
			},
		});
		d.show();
	}

	_ops_render_recalls($b) {
		if (!(this.recalls || []).length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No open recalls. Recalled manifests appear here until physical return is confirmed at the source warehouse.")}</div>`);
			return;
		}
		const rows = this.recalls.map((r) => {
			const age = (r.recall_age_hours == null) ? "—" : `${r.recall_age_hours}h`;
			let age_cls = "lcc-sev-low";
			if (r.recall_age_hours != null) {
				if (r.recall_age_hours >= 24) age_cls = "lcc-sev-critical";
				else if (r.recall_age_hours >= 8) age_cls = "lcc-sev-high";
				else if (r.recall_age_hours >= 2) age_cls = "lcc-sev-medium";
			}
			const nm = encodeURIComponent(r.name);
			const trip_html = r.trip
				? `<a href="#" class="lcc-trip-link" data-name="${frappe.utils.escape_html(r.trip)}">${frappe.utils.escape_html(r.trip)}</a>
				   <div class="lcc-muted">${frappe.utils.escape_html(r.trip_status || "")}</div>`
				: `<span class="text-muted">—</span>`;
			const driver_html = r.driver_name
				? `${frappe.utils.escape_html(r.driver_name)}${r.driver_phone ? ` · <a href="tel:${frappe.utils.escape_html(r.driver_phone)}">${frappe.utils.escape_html(r.driver_phone)}</a>` : ""}
				   ${r.vehicle_number ? `<div class="lcc-muted"><i class="fa fa-truck"></i> ${frappe.utils.escape_html(r.vehicle_number)}</div>` : ""}`
				: `<span class="text-muted">—</span>`;
			return `<tr>
				<td><a href="/app/ch-transfer-manifest/${nm}" target="_blank">${frappe.utils.escape_html(r.name)}</a></td>
				<td>${trip_html}</td>
				<td>${driver_html}</td>
				<td>${window.ch_wh_label_html ? ch_wh_label_html(r.source_warehouse, "—") : frappe.utils.escape_html(r.source_warehouse || "—")} ← ${window.ch_wh_label_html ? ch_wh_label_html(r.destination_warehouse, "—") : frappe.utils.escape_html(r.destination_warehouse || "—")}</td>
				<td class="lcc-exc-remarks">${frappe.utils.escape_html(r.recall_reason || "—")}</td>
				<td><span class="lcc-sev ${age_cls}">${age}</span><div class="lcc-muted">${r.recall_initiated_at ? frappe.datetime.str_to_user(r.recall_initiated_at) : ""}</div></td>
				<td>
					<button class="btn btn-xs btn-success lcc-rec-confirm" data-name="${r.name}"
						title="${__("Confirm return — reverses stock entries to source warehouse")}">
						<i class="fa fa-check"></i> ${__("Confirm Return")}
					</button>
				</td>
			</tr>`;
		}).join("");

		$b.html(`
			<div class="lcc-ops-bar">
				<span class="lcc-muted lcc-ops-bar-hint">
					<i class="fa fa-info-circle"></i>
					${__("Recalled manifests stay in this inbox until source warehouse confirms physical return. Confirming return reverses the underlying Stock Entries.")}
				</span>
			</div>
			<div class="lcc-table-wrap"><table class="lcc-table">
				<thead><tr>
					<th>${__("Manifest")}</th>
					<th>${__("Original Trip")}</th>
					<th>${__("Driver / Vehicle")}</th>
					<th>${__("Returning To ← From")}</th>
					<th>${__("Reason")}</th>
					<th>${__("Recall Age")}</th>
					<th style="width:160px">${__("Action")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>`);
	}

	_ops_render_drivers($b) {
		if (!this.drivers.length) {
			$b.html(`<div class="lcc-empty">${__("No drivers found.")}</div>`);
			return;
		}
		const trip_status_colors = {
			Draft: "grey", Assigned: "blue", Started: "orange",
			Completed: "green", Closed: "darkgrey", Cancelled: "red",
		};
		const rows = this.drivers.map((d) => `<tr>
			<td><strong>${frappe.utils.escape_html(d.full_name || d.name)}</strong></td>
			<td>${frappe.utils.escape_html(d.cell_number || "—")}</td>
			<td><span class="lcc-avail lcc-avail-${(d.availability_status || "Available").replace(/ /g,"-").toLowerCase()}">${d.availability_status || "—"}</span></td>
			<td>${d.current_trip
				? `<a href="#" class="lcc-trip-link" data-name="${d.current_trip}">${d.current_trip}</a>`
					+ (d.current_trip_status
						? ` <span class="indicator-pill ${trip_status_colors[d.current_trip_status] || "grey"}" style="margin-left:4px">${frappe.utils.escape_html(d.current_trip_status)}</span>`
						: "")
				: "—"}</td>
			<td>${frappe.utils.escape_html(d.status || "")}</td>
		</tr>`).join("");

		$b.html(`<div class="lcc-table-wrap"><table class="lcc-table">
			<thead><tr>
				<th>${__("Driver")}</th><th>${__("Phone")}</th>
				<th>${__("Availability")}</th><th>${__("Current Trip")}</th><th>${__("HR Status")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table></div>`);
	}

	_ops_update_attach() {
		const n = this.selected_manifests.size;
		this.$root.find(".lcc-attach-btn").prop("disabled", n === 0).find(".lcc-attach-n").text(n);
		this.$root.find(".lcc-new-trip-from-sel").prop("disabled", n === 0);
		// Bundling needs at least 2 manifests to be meaningful (one
		// manifest already prints its own per-shipment QR via the
		// per-row Print Box action).
	}

	/* ── Per-row print helpers (Operations → Manifests) ─────────
	 * Pattern mirrors market-standard TMS cockpits (Oracle TM, SAP TM):
	 * the dispatcher prints box labels, transfer receipts and e-Way Bills
	 * inline from the worklist row without opening the document form.
	 */
	_ops_print_manifest(name, format) {
		if (!name) return;
		const params = new URLSearchParams({
			doctype: "CH Transfer Manifest",
			name: name,
			format: format,
			trigger_print: "1",
			_lang: (frappe.boot && frappe.boot.lang) || "en",
		});
		window.open("/printview?" + params.toString(), "_blank");
	}

	_ops_print_ewaybills(name) {
		if (!name) return;
		const ewb_api = "ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest.";
		frappe.call({
			method: ewb_api + "refresh_ewaybill_summary",
			args: { manifest: name },
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
						<thead><tr>
							<th>${__("Stock Entry")}</th>
							<th>${__("e-Way Bill")}</th>
							<th>${__("Status")}</th>
							<th>${__("Valid Till")}</th>
							<th>${__("Vehicle")}</th>
							<th>${__("Print")}</th>
						</tr></thead>
						<tbody>
							${rows.map(rr => `
								<tr>
									<td><a href="/app/stock-entry/${encodeURIComponent(rr.stock_entry)}" target="_blank">${frappe.utils.escape_html(rr.stock_entry)}</a></td>
									<td>${rr.ewaybill ? `<code>${frappe.utils.escape_html(rr.ewaybill)}</code>` : `<span class="text-muted">—</span>`}</td>
									<td>${rr.status ? `<span class="indicator-pill ${rr.ewaybill ? "green" : "orange"}">${frappe.utils.escape_html(rr.status)}</span>` : ""}</td>
									<td>${rr.ewaybill_validity || ""}</td>
									<td>${rr.vehicle_no ? `<code>${frappe.utils.escape_html(rr.vehicle_no)}</code>` : ""}</td>
									<td>${rr.ewaybill
										? `<a class="btn btn-xs btn-default" href="/app/stock-entry/${encodeURIComponent(rr.stock_entry)}?print=1" target="_blank">${__("Print")}</a>`
										: ""}</td>
								</tr>`).join("")}
						</tbody>
					</table>
					<div class="text-muted" style="margin-top:8px">
						${__("Driver must carry one printout per Stock Entry. Click 'Print All' to open all in new tabs (allow pop-ups).")}
					</div>
				`;
				const d = new frappe.ui.Dialog({
					title: __("e-Way Bills — {0}", [name]),
					size: "large",
					fields: [{ fieldtype: "HTML", fieldname: "ewb_table", options: html }],
					primary_action_label: __("Print All"),
					primary_action: () => {
						rows.forEach(rr => {
							if (rr.ewaybill) {
								window.open(`/app/stock-entry/${encodeURIComponent(rr.stock_entry)}?print=1`, "_blank");
							}
						});
						d.hide();
					},
				});
				d.show();
			},
		});
	}

	/* ── Recalls: confirm physical return ─────────────────────────
	 * Wraps transfer_manifest_api.confirm_return — same dialog the
	 * manifest form shows, but inlined into the dispatcher console so
	 * source-warehouse staff don't need to open each manifest.
	 */
	async _ops_confirm_recall_return(name) {
		if (!name) return;
		const api = "ch_logistics.api.transfer_manifest_api.";
		const row = (this.recalls || []).find((r) => r.name === name) || {};
		const src = window.ch_wh_label_html ? ch_wh_label_html(row.source_warehouse, "—") : frappe.utils.escape_html(row.source_warehouse || "—");
		const response = await frappe.call({
			method: api + "get_return_requirements",
			args: { manifest: name },
			freeze: true,
			freeze_message: __("Loading return requirements..."),
		});
		const requirements = response.message || {};
		const expected = new Set(requirements.serials || []);
		const scanned = new Set();
		const countRows = (requirements.items || []).filter(
			(row) => !(row.serials || []).length && Number(row.qty || 0) > 0
		);
		const countFields = countRows.map((row, index) => ({
			fieldname: `return_qty_${index}`,
			fieldtype: "Float",
			label: __("Counted Qty — {0}", [row.item_code || ""]),
			description: __("Stock Entry {0}; expected {1}", [row.stock_entry || "", row.qty || 0]),
			reqd: 1,
		}));
		const d = new frappe.ui.Dialog({
			title: __("Confirm Return — {0}", [name]),
			size: "large",
			fields: [
				{
					fieldname: "info_html",
					fieldtype: "HTML",
					options: `<div class="alert alert-info" style="padding:12px;border-radius:6px;background:#d1ecf1;border:1px solid #bee5eb;">
						<strong>${__("Return Checklist")}:</strong>
						<ul style="margin:8px 0 0 0;padding-left:18px">
							<li>${__("All items have been physically returned to")} <strong>${src}</strong></li>
							<li>${__("Every serialized item must be scanned below")}</li>
							<li>${__("A photo has been taken of the returned items")}</li>
						</ul>
						<div><b>${__("Expected Qty")}:</b> ${requirements.total_qty || 0} · <b>${__("Expected IMEIs")}:</b> ${expected.size}</div>
						<div style="margin-top:8px"><b>${__("Recall Reason")}:</b> ${frappe.utils.escape_html(row.recall_reason || "—")}</div>
					</div>`,
				},
				{
					fieldname: "scan_imei",
					fieldtype: "Data",
					label: __("Scan Returned IMEI / Serial and press Enter"),
					hidden: expected.size === 0,
				},
				{
					fieldname: "scan_progress",
					fieldtype: "HTML",
					hidden: expected.size === 0,
				},
				...(countFields.length ? [{
					fieldname: "count_section",
					fieldtype: "Section Break",
					label: __("Count Non-Serialized Items"),
					description: __("Enter the physically counted quantity; an exact match is required."),
				}] : []),
				...countFields,
				{
					fieldname: "return_photo",
					fieldtype: "Attach Image",
					label: __("Return Photo (Required)"),
					description: __("Photo of all items returned to source warehouse"),
					reqd: 1,
				},
			],
			primary_action_label: __("Confirm Return & Reverse Stock"),
			primary_action: (values) => {
				const missing = [...expected].filter((value) => !scanned.has(value));
				if (missing.length) {
					frappe.msgprint({
						title: __("Return Scan Incomplete"),
						indicator: "red",
						message: __("Scan all returned IMEIs. Missing: {0}", [missing.join(", ")]),
					});
					return;
				}
				const returnedQuantities = countRows.map((item, index) => ({
					stock_entry: item.stock_entry,
					row_name: item.row_name,
					qty: values[`return_qty_${index}`],
				}));
				if (countRows.some(
					(item, index) => Number(values[`return_qty_${index}`]) !== Number(item.qty)
				)) {
					frappe.msgprint({
						title: __("Return Count Mismatch"),
						indicator: "red",
						message: __("The counted quantities must match the expected return before stock can be reversed."),
					});
					return;
				}
				frappe.confirm(
					__("Confirm that all items have been returned? This will reverse the stock entries and cannot be undone."),
					() => {
						d.hide();
						frappe.call({
							method: api + "confirm_return",
							args: {
								manifest: name,
								return_photo: values.return_photo,
								returned_serials: [...scanned],
								returned_quantities: returnedQuantities,
							},
							freeze: true,
							freeze_message: __("Reversing stock entries..."),
							callback: (r) => {
								if (r.message) {
									const reversed = (r.message.reversed_stock_entries || []).join(", ");
									frappe.show_alert({
										message: __("Return confirmed. Stock reversed: {0}", [reversed || "N/A"]),
										indicator: "green",
									}, 7);
									this._ops_load();
								}
							},
						});
					}
				);
			},
		});
		d.show();
		const renderProgress = () => {
			if (!expected.size) return;
			const missing = [...expected].filter((value) => !scanned.has(value));
			d.fields_dict.scan_progress.$wrapper.html(`
				<div class="alert ${missing.length ? "alert-warning" : "alert-success"}">
					<strong>${__("Scanned {0} of {1}", [scanned.size, expected.size])}</strong>
					<div class="small">${missing.length ? __("Remaining: {0}", [missing.length]) : __("All expected IMEIs reconciled")}</div>
				</div>
			`);
		};
		renderProgress();
		if (expected.size) {
			const $input = d.fields_dict.scan_imei.$input;
			$input.on("keydown.returnScan", (event) => {
				if (event.key !== "Enter") return;
				event.preventDefault();
				const value = String($input.val() || "").trim();
				$input.val("");
				if (!value) return;
				if (!expected.has(value)) {
					frappe.show_alert({
						message: __("Scanned IMEI is wrong. Please scan the correct item."),
						indicator: "red",
					}, 5);
					return;
				}
				if (scanned.has(value)) {
					frappe.show_alert({ message: __("IMEI {0} was already scanned", [value]), indicator: "orange" }, 4);
					return;
				}
				scanned.add(value);
				renderProgress();
			});
			setTimeout(() => $input.trigger("focus"), 100);
		}
	}

	/* ── Side panel ───────────────────────────────────────────── */

	async _ops_open_trip(name) {
		this.active_trip = name;
		const $s = $("#lcc-side").addClass("open");
		$s.html(`<div class="lcc-loading">${__("Loading…")}</div>`);
		const r = await frappe.call({ method: _LCC + "get_trip_detail", args: { trip: name } });
		const t = r.message;
		if (!t) { $s.html(`<div class="lcc-empty">${__("Trip not found.")}</div>`); return; }
		$s.html(this._ops_side_html(t));
	}

	_ops_close_side() {
		$("#lcc-side").removeClass("open").empty();
		this.active_trip = null;
	}

	_ops_side_html(t) {
		// Once a driver is on the trip, use Unassign first rather than
		// re-running Assign Driver over an existing assignment.
		const can_assign   = t.status === "Draft";
		const can_start    = t.status === "Assigned";
		const can_complete = t.status === "Started";
		const can_close    = t.status === "Completed";
		const can_unassign = t.status === "Assigned";
		const can_cancel   = ["Draft", "Assigned"].includes(t.status);
		const can_recall_trip = t.status === "Started";
		// Not once the trip has started: the driver is on the road with that
		// manifest in the vehicle, so taking it off the trip in the console
		// would leave the paperwork saying one thing and the van another.
		// From Started onwards the honest route is Abort & Recall, which
		// keeps the trip open until the goods are physically back.
		const can_detach = ["Draft", "Assigned"].includes(t.status);
		const can_resequence = ["Assigned", "Started"].includes(t.status);
		// Server-resolved capability (CH Logistics Settings → Role Matrix);
		// cosmetic only — trip_close re-checks head_override server-side.

		// Group by stop_roles (location-derived by the backend's
		// stop_roles.annotate — matches each manifest to a stop by actual
		// warehouse, not a number) rather than the legacy stop_sequence
		// pointer. stop_sequence is a raw number that goes stale the moment
		// a resequence changes which warehouse holds that number — it was
		// only ever "safe" here because resequencing used to silently fail
		// to persist a new order at all (now fixed, which is exactly what
		// made this surface: a manifest could show attached to whatever
		// unrelated stop now happened to hold its old number).
		const mf_by_stop = {};
		(t.manifests || []).forEach((m) => {
			const roles = m.stop_roles || {};
			const seqs = Object.keys(roles);
			if (!seqs.length) {
				// No location match resolved (e.g. stop coords/warehouse
				// missing) — fall back to the legacy pointer rather than
				// dropping the manifest from the panel entirely.
				const k = m.stop_sequence || 0;
				(mf_by_stop[k] = mf_by_stop[k] || []).push(m);
				return;
			}
			seqs.forEach((seqStr) => {
				const k = parseInt(seqStr, 10);
				(mf_by_stop[k] = mf_by_stop[k] || []).push(m);
			});
		});

		// Only show stops that actually have a manifest attached — a route
		// can carry many planned stops before manifests get assigned to
		// them, but this panel is about what's actually moving, not the
		// full potential route.
		// t.stops comes back in child-table row order, which is NOT
		// guaranteed to match the sequence field — resequencing recomputes
		// sequence numbers but doesn't reorder the underlying rows, so
		// without this sort the panel silently kept showing the old visiting
		// order even after a successful re-sequence.
		const stops_html = (t.stops || [])
			.slice()
			.sort((a, b) => (a.sequence || 0) - (b.sequence || 0))
			.filter((s) => (mf_by_stop[s.sequence] || []).length).map((s) => {
			const mfs = mf_by_stop[s.sequence].map((m) => `
				<div class="lcc-side-mf">
					<a href="/app/ch-transfer-manifest/${m.name}" target="_blank">${m.name}</a>
					<span class="lcc-muted">${m.status || ""} · ${m.total_qty || 0}q</span>
					${can_detach ? `<button class="btn btn-xs btn-default lcc-side-detach" data-name="${m.name}"><i class="fa fa-unlink"></i></button>` : ""}
				</div>`).join("");
			// A drop reads as the leg it is — hub → store — so a trip with
			// three drops off one hub is legible at a glance instead of
			// three bare store names the reader has to pair up themselves.
			const from_hub = (s.stop_type || "").includes("Drop") && t.hub_warehouse
				&& s.warehouse !== t.hub_warehouse
				? `${window.ch_wh_label_html ? ch_wh_label_html(t.hub_warehouse, "") : frappe.utils.escape_html(t.hub_warehouse)} → `
				: "";
			return `<div class="lcc-side-stop">
				<div class="lcc-side-stop-head">
					<b>#${s.sequence}</b> ${from_hub}${window.ch_wh_label_html ? ch_wh_label_html(s.warehouse, "") : frappe.utils.escape_html(s.warehouse || "")}
					<span class="lcc-sev lcc-sev-${(s.status || "").toLowerCase().replace(/ /g,"-")}">${s.status}</span>
				</div>
				<div class="lcc-side-stop-meta">${s.stop_type || ""} · ETA ${s.eta ? frappe.datetime.str_to_user(s.eta) : "—"}</div>
				<div class="lcc-side-mfs">${mfs}</div>
			</div>`;
		}).join("") || `<div class="lcc-empty">${__("No manifest-connected stops on this route.")}</div>`;

		const excs_html = (t.exceptions || []).length
			? `<div class="lcc-side-sec">${__("Exceptions")}</div>` +
			  t.exceptions.map((e) => `
				<div class="lcc-side-exc">
					<span class="lcc-sev lcc-sev-${(e.severity||"medium").toLowerCase()}">${e.severity}</span>
					<b>${frappe.utils.escape_html(e.exception_type || "")}</b>
					<span class="lcc-muted">#${e.stop_sequence || "—"} · ${e.resolution_status}</span>
					<div>${frappe.utils.escape_html(e.remarks || "")}</div>
				</div>`).join("")
			: "";

		return `
			<div class="lcc-side-head">
				<div>
					<h4>${frappe.utils.escape_html(t.name)}
						<span class="lcc-side-status lcc-ops-s-${(t.status||"").toLowerCase()}">${t.status}</span>
					</h4>
					<div class="lcc-muted">${t.trip_date} · ${t.direction} · ${frappe.utils.escape_html(t.route || "—")}</div>
				</div>
				<button class="btn btn-sm btn-default lcc-side-close">✕</button>
			</div>
			<div class="lcc-side-info">
				<div><b>${__("Driver")}:</b> ${frappe.utils.escape_html(t.driver_name || __("Unassigned"))}${t.driver_phone ? ` · ${t.driver_phone}` : ""}</div>
				<div><b>${__("Vehicle")}:</b> ${frappe.utils.escape_html(t.vehicle_number || "—")}</div>
				<div><b>${__("Hub")}:</b> ${window.ch_wh_label_html ? ch_wh_label_html(t.hub_warehouse, "—") : frappe.utils.escape_html(t.hub_warehouse || "—")}</div>
				<div><b>${__("Shipments")}:</b> ${t.total_shipments || 0}</div>
				<div><b>${__("Created")}:</b> ${frappe.utils.escape_html(t.created_by || "—")}${t.created_on ? ` · ${frappe.datetime.str_to_user(t.created_on)}` : ""}</div>
				${t.cancelled_by ? `<div class="lcc-cancel-info"><b>${__("Cancelled")}:</b> ${frappe.utils.escape_html(t.cancelled_by)}${t.cancelled_on ? ` · ${frappe.datetime.str_to_user(t.cancelled_on)}` : ""}${t.cancellation_reason ? `<br><span class="lcc-muted">${frappe.utils.escape_html(t.cancellation_reason)}</span>` : ""}</div>` : ""}
			</div>
			${this._manual_status_html(t)}
			<div class="lcc-side-actions">
				${can_assign   ? `<button class="btn btn-sm btn-default" id="lcc-side-assign"><i class="fa fa-user-plus"></i> ${__("Assign Driver")}</button>` : ""}
				${can_start    ? `<button class="btn btn-sm btn-warning" id="lcc-side-start" disabled title="${__("Starting a trip from here is currently disabled")}"><i class="fa fa-play"></i> ${__("Start")}</button>` : ""}
				${can_resequence ? `<button class="btn btn-sm btn-default" id="lcc-side-resequence"><i class="fa fa-random"></i> ${__("Re-sequence")}</button>` : ""}
				${can_complete ? `<button class="btn btn-sm btn-success" id="lcc-side-complete" disabled title="${__("Completing a trip from here is currently disabled")}"><i class="fa fa-check"></i> ${__("Complete")}</button>` : ""}
				${can_close    ? `<button class="btn btn-sm btn-primary" id="lcc-side-close-trip"><i class="fa fa-archive"></i> ${__("Close")}</button>` : ""}
				${can_unassign ? `<button class="btn btn-sm btn-default" id="lcc-side-cancel"><i class="fa fa-user-times"></i> ${__("Unassign")}</button>` : ""}
				${can_cancel ? `<button class="btn btn-sm btn-danger" id="lcc-side-cancel-trip"><i class="fa fa-ban"></i> ${__("Cancel Trip")}</button>` : ""}
				${can_recall_trip ? `<button class="btn btn-sm btn-danger" id="lcc-side-recall-trip"><i class="fa fa-undo"></i> ${__("Abort & Recall Trip")}</button>` : ""}
			</div>
			<div class="lcc-side-sec">${__("Stops")}</div>
			${stops_html}
			${excs_html}
		`;
	}

	// A courier or third-party trip has no driver app to move it, so ops
	// records each step as the carrier reports back. Delivered is the one that
	// matters: it hands every shipment to the receiving store, which then
	// scans the serials in POS — that scan, not this click, posts the stock.
	_manual_status_html(t) {
		const mode = t.transport_mode || "Own";
		if (mode === "Own") return "";
		const next = {
			"Draft": ["Assigned"],
			"Assigned": ["Picked Up"],
			"Picked Up": ["Delivery Pending", "Delivered"],
			"Delivery Pending": ["Delivered"],
		}[t.status] || [];
		const carrier = t.courier_partner || t.carrier_name || __("not named");
		return `
			<div class="lcc-side-sec">${__("Carried by {0}", [frappe.utils.escape_html(carrier)])}${
				t.tracking_number ? ` · ${frappe.utils.escape_html(t.tracking_number)}` : ""}</div>
			<div class="lcc-side-actions">
				${next.map((s) => `
					<button class="btn btn-sm ${s === "Delivered" ? "btn-success" : "btn-default"}
							lcc-side-manual-status" data-status="${s}">
						${s === "Delivered" ? `<i class="fa fa-check"></i> ` : ""}${__("Mark {0}", [__(s)])}
					</button>`).join("")}
				${next.length ? "" : `<span class="lcc-muted">${
					t.status === "Delivered"
						? __("Waiting for the store to scan it in")
						: __("Nothing left to record")}</span>`}
			</div>`;
	}

	_ops_manual_status(status) {
		if (!this.active_trip) return;
		const trip = this.active_trip;
		const run = () => frappe.call({
			method: "ch_logistics.api.logistics_api.trip_set_manual_status",
			args: { trip, status }, freeze: true,
		}).then(() => {
			frappe.show_alert({ message: __("Trip {0} — {1}", [trip, __(status)]),
				indicator: "green" });
			this._ops_load();
			this._ops_open_trip(trip);
		});
		if (status !== "Delivered") return run();
		// Delivered is the step that moves goods: it hands every shipment to
		// the receiving store, and there is no way back from it. Ops records
		// it by hand because the courier telephones — but the store still
		// proves it got the goods, exactly as it does for a driver delivery.
		this._ops_deliver_dialog(trip);
	}

	// A stop is what a receiver signs for, so each one is confirmed on its
	// own: its store reads out its own code. Stops are taken in order, and
	// the trip only moves once the last one is in.
	_ops_deliver_dialog(trip) {
		frappe.call({ method: _LCC + "trip_delivery_checklist", args: { trip }, freeze: true })
			.then((r) => {
				const info = r.message;
				if (!info) return;
				this._ops_deliver_next_stop(trip, info);
			});
	}

	_ops_deliver_next_stop(trip, info, dialog) {
		const stop = (info.stops || []).find((s) => !s.delivered);
		if (!stop) {
			if (dialog) dialog.hide();
			frappe.msgprint(__("Every stop on this trip has been confirmed."));
			this._ops_load();
			this._ops_open_trip(trip);
			return;
		}
		if (dialog) dialog.hide();
		this._ops_deliver_stop_form(trip, info, stop);
	}

	_ops_deliver_stop_form(trip, info, stop) {
		const esc = frappe.utils.escape_html;
		const who = stop.store || stop.warehouse || __("this stop");
		const people = stop.receivers || [];

		const fields = [{
			fieldname: "stops_html", fieldtype: "HTML",
			options: `<div class="lcc-side-sec">${__("Stops")}</div>
				<div style="margin-bottom:10px">${(info.stops || []).map((s) => `
					<div style="display:flex;justify-content:space-between;gap:10px;padding:4px 0;
							border-bottom:1px solid var(--border-color)${
								s.sequence === stop.sequence ? ";font-weight:600" : ""}">
						<span>#${s.sequence} ${esc(s.store || s.warehouse || "")}
							<span class="lcc-muted">${esc((s.manifests || []).join(", "))}</span></span>
						<span class="lcc-muted">${s.delivered
							? __("Confirmed")
							: __("{0} unit(s)", [s.qty || 0])}</span>
					</div>`).join("")}</div>`,
		}];

		// Whoever signs for the box is who the code should reach, so the
		// store's own POS Executives are offered; with none on the roster it
		// falls back to the store's contacts.
		if (people.length) {
			fields.push({
				fieldname: "receiver", fieldtype: "Select",
				label: __("Received by — {0}", [who]),
				options: people.map((p) => ({
					value: p.name,
					label: p.has_email || p.has_mobile
						? p.executive_name
						: __("{0} (no contact on file)", [p.executive_name]),
				})),
				default: people.length === 1 ? people[0].name : "",
				reqd: 1,
			});
		} else {
			// Nobody on the store's roster: the name is typed, as the driver
			// app does, so a courier delivery still says who took the goods.
			fields.push({
				fieldname: "receiver_name", fieldtype: "Data", reqd: 1,
				label: __("Receiver Name — {0}", [who]),
				description: __("No POS Executive is on file for this store. Type the name of the person who received the goods."),
			});
		}
		fields.push({
			fieldname: "send_otp", fieldtype: "Button",
			label: __("Send OTP to {0}", [who]),
			click: () => this._ops_send_stop_otp(trip, stop, dialog),
		});
		fields.push({ fieldname: "otp_sent_html", fieldtype: "HTML" });
		fields.push({
			fieldname: "otp", fieldtype: "Data", label: __("OTP — {0}", [who]),
			description: (stop.manifests || []).join(", "),
		});
		fields.push({
			// The day, picked by hand. Ops knows which day the courier handed
			// it over, never the minute, so no clock is asked for.
			fieldname: "delivered_at", fieldtype: "Date", label: __("Delivery Date"),
			default: frappe.datetime.get_today(), reqd: 1,
			// A delivery is recorded after it happened: today and earlier can
			// be entered, later cannot. A plain Date object — the option goes
			// straight to the datepicker.
			max_date: new Date(),
			description: __("The day the courier handed it over. A future date cannot be entered."),
		});

		const dialog = new frappe.ui.Dialog({
			title: __("Deliver Stop #{0} — {1}", [stop.sequence, who]),
			size: "large",
			fields,
			primary_action_label: __("Confirm Stop #{0}", [stop.sequence]),
			primary_action: (values) => {
				const delivered_at = values.delivered_at;
				if (delivered_at && delivered_at > frappe.datetime.get_today()) {
					frappe.msgprint(__("A delivery cannot be recorded in the future."));
					return;
				}
				const otp = (values.otp || "").trim();
				if (!otp) {
					frappe.msgprint(__("Enter the OTP {0} read out.", [who]));
					return;
				}
				frappe.call({
					method: _LCC + "trip_deliver_stop",
					args: { trip, sequence: stop.sequence, otp, delivered_at,
						receiver: values.receiver || null,
						receiver_name: (values.receiver_name || "").trim() || null },
					freeze: true, freeze_message: __("Handing over..."),
				}).then((r) => {
					const out = r.message || {};
					if (!out.ok) {
						// The dialog stays open: only this stop's code is
						// wrong, and the attempt is already on the audit log.
						frappe.msgprint({
							title: __("Not delivered"), indicator: "red",
							message: esc(out.message || __("The OTP could not be confirmed.")),
						});
						return;
					}
					frappe.show_alert({
						message: __("Stop #{0} — {1}", [stop.sequence, __("Delivered")]),
						indicator: "green",
					});
					this._ops_load();
					if ((out.pending || []).length) {
						// Straight on to the next store, which gets its own
						// code — one stop's OTP never covers another.
						this._ops_deliver_next_stop(trip, out.state, dialog);
						return;
					}
					dialog.hide();
					frappe.show_alert({ message: __("Trip {0} — {1}", [trip, __("Delivered")]),
						indicator: "green" });
					this._ops_open_trip(trip);
				});
			},
		});
		dialog.show();
		this._ops_send_stop_otp(trip, stop, dialog);
	}

	_ops_send_stop_otp(trip, stop, dialog) {
		const esc = frappe.utils.escape_html;
		frappe.call({
			method: _LCC + "trip_request_stop_otp",
			args: { trip, sequence: stop.sequence, receiver: dialog.get_value("receiver") || null },
			freeze: true,
		}).then((r) => {
			const sent = r.message || {};
			const field = dialog.fields_dict.otp_sent_html;
			if (!field) return;
			const to = (sent.masked_emails || []).concat(sent.masked_mobiles || []);
			const who = esc(sent.store || stop.warehouse || "");
			// A store nobody can reach cannot read out a code, so it is
			// called out rather than left looking sent.
			field.$wrapper.html(`<div style="margin-bottom:10px">${sent.reachable
				? `<span class="lcc-muted">${who}: ${esc(to.join(", "))}${
					sent.receiver ? ` (${esc(sent.receiver)})` : ""}</span>`
				: `<span style="color:var(--red-600,#b91c1c)">${
					__("{0}: no email or mobile on file — the store cannot receive a code.", [who])
				}</span>`}</div>`);
		});
	}

	_ops_trip_action(method, prefix = _LCC, extraArgs = {}) {
		if (!this.active_trip) return;
		const trip = this.active_trip;
		frappe.call({ method: prefix + method, args: { trip, ...extraArgs } })
			.then(() => {
				frappe.show_alert({ message: __("Done"), indicator: "green" });
				this._ops_load();
				// The board reload above refreshes the main list, but never
				// touched the open side panel — every trip action (Start,
				// Re-sequence, Unassign, Cancel…) looked like a no-op
				// because the stops/status shown there stayed frozen on
				// pre-action data. Re-fetch it too if it's still open.
				if (this.active_trip === trip) this._ops_open_trip(trip);
			});
	}

	_ops_start_trip() {
		// Pre-flight the empty-stop warning trip_start would otherwise throw
		// on, same as the Delivery App's Accept & Start — one confirm here
		// instead of a failed call.
		if (!this.active_trip) return;
		const trip = this.active_trip;
		frappe.call({ method: _LCC + "get_empty_stop_warning", args: { trip } }).then((r) => {
			const empty = (r.message && r.message.empty_stops) || [];
			if (!empty.length) {
				this._ops_trip_action("trip_start");
				return;
			}
			frappe.confirm(
				__("Stop(s) {0} have no shipments assigned — nothing to load or unload there. Start anyway?",
					[empty.map((s) => "#" + s).join(", ")]),
				() => this._ops_trip_action("trip_start", _LCC, { override_empty_stops: 1 })
			);
		});
	}

	_ops_cancel_trip() {
		if (!this.active_trip) return;
		const trip = this.active_trip;
		const d = new frappe.ui.Dialog({
			title: __("Cancel Trip {0}", [trip]),
			fields: [
				{ fieldtype: "HTML", options: `<div class="alert alert-warning">${__("This pre-departure cancellation will cancel the trip, release the driver, and release every attached manifest back to Packed / Unassigned Manifests so it can be re-attached to a different trip — the manifests themselves are NOT cancelled and their stock is not reversed. If physical pickup has started, the server will require Abort & Recall instead.")}</div>` },
				{
					fieldtype: "Select", fieldname: "reason_code", label: __("Reason"),
					options: LCC_TRIP_CANCEL_REASONS.join("\n"), reqd: 1,
				},
				{
					fieldtype: "Small Text", fieldname: "reason_notes", label: __("Details"),
					description: __("Anything the next person needs to know. Required when the reason is Other."),
					mandatory_depends_on: "eval:doc.reason_code == 'Other'",
				},
			],
			primary_action_label: __("Cancel Trip"),
			primary_action: (vals) => {
				const reason = lcc_trip_cancel_reason(vals);
				if (!vals.reason_code) {
					frappe.show_alert({ message: __("A reason is required."), indicator: "red" });
					return;
				}
				if (vals.reason_code === "Other" && !(vals.reason_notes || "").trim()) {
					frappe.show_alert({
						message: __("Say what happened — 'Other' on its own tells the next person nothing."),
						indicator: "red",
					});
					return;
				}
				d.hide();
				frappe.call({ method: _LCC + "trip_cancel", args: { trip, reason } })
					.then(() => {
						frappe.show_alert({ message: __("Trip {0} cancelled", [trip]), indicator: "orange" });
						this._ops_close_side();
						this._ops_load();
					});
			},
		});
		d.show();
	}

	_ops_recall_trip() {
		if (!this.active_trip) return;
		const trip = this.active_trip;
		const d = new frappe.ui.Dialog({
			title: __("Abort & Recall Trip {0}", [trip]),
			fields: [
				{
					fieldtype: "HTML",
					options: `<div class="alert alert-danger">
						<strong>${__("Physical return required")}</strong><br>
						${__("Every active manifest will be recalled. The trip remains open until source staff scan all returned IMEIs, attach return proof, and every stock reversal succeeds.")}
					</div>`,
				},
				{ fieldtype: "Small Text", fieldname: "reason", label: __("Abort / Recall Reason"), reqd: 1 },
				{ fieldtype: "Small Text", fieldname: "notes", label: __("Instructions to Driver") },
			],
			primary_action_label: __("Abort & Recall Trip"),
			primary_action: (values) => {
				frappe.confirm(
					__("Recall every active manifest on trip {0}?", [trip]),
					() => {
						d.hide();
						frappe.call({
							method: _LCC + "trip_initiate_recall",
							args: { trip, reason: values.reason, notes: values.notes },
							freeze: true,
							freeze_message: __("Initiating trip recall..."),
						}).then((r) => {
							frappe.show_alert({
								message: r.message?.message || __("Trip recall initiated"),
								indicator: "orange",
							}, 7);
							this._ops_close_side();
							this._ops_load();
						});
					}
				);
			},
		});
		d.show();
	}

	_ops_assign_driver() {
		if (!this.active_trip) return;
		const trip = this.active_trip;
		const d = new frappe.ui.Dialog({
			title: __("Assign Driver"),
			fields: [
				{
					fieldtype: "Link", fieldname: "driver", label: __("Driver"), options: "Driver", reqd: 1,
					get_query: () => ({ query: "ch_logistics.api.logistics_api.unassigned_drivers_query" }),
					onchange: () => {
						const driver = d.get_value("driver");
						if (!driver || d.get_value("vehicle")) return;
						frappe.db.get_value("Driver", driver, "custom_default_vehicle", (r) => {
							if (r && r.custom_default_vehicle) d.set_value("vehicle", r.custom_default_vehicle);
						});
					},
				},
				{ fieldtype: "Link", fieldname: "vehicle", label: __("Vehicle"), options: "Vehicle" },
			],
			primary_action_label: __("Assign"),
			primary_action: (vals) => {
				frappe.call({ method: _LCC + "trip_assign_driver", args: { trip, driver: vals.driver, vehicle: vals.vehicle || null } })
					.then(() => { d.hide(); frappe.show_alert({ message: __("Driver assigned"), indicator: "green" }); this._ops_load(); });
			},
		});
		d.show();
	}

	_ops_detach_manifest(manifest) {
		frappe.confirm(__("Detach manifest {0}?", [manifest]), () => {
			frappe.call({ method: _LCC + "detach_manifest", args: { manifest } })
				.then(() => { frappe.show_alert({ message: __("Detached"), indicator: "green" }); this._ops_load(); });
		});
	}

	_ops_attach() {
		if (!this.selected_manifests.size) return;
		const manifests = Array.from(this.selected_manifests);
		// A Select field's options double as both label and submitted
		// value, so a bare trip ID list is all it could ever show. Same
		// "Name — extra detail" convention used elsewhere (e.g. the POS
		// store picker) — build a readable label per trip, then split the
		// ID back off the front of whichever one was chosen.
		const opts = [];
		Object.values(this.board.buckets || {}).flat()
			.filter((t) => ["Draft","Assigned","Started"].includes(t.status))
			.forEach((t) => opts.push(
				`${t.name} — ${t.route || "—"} — ${t.driver_name || __("Unassigned")}`
			));

		if (!opts.length) {
			frappe.msgprint(__("No eligible trips (Draft/Assigned/Started) available to attach to."));
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("Attach {0} manifest(s) to trip", [manifests.length]),
			fields: [{ fieldtype: "Select", fieldname: "trip", label: __("Trip"), options: opts.join("\n"), reqd: 1 }],
			primary_action_label: __("Attach"),
			primary_action: (vals) => {
				const trip = (vals.trip || "").split(" — ")[0];
				frappe.call({ method: _LCC + "attach_manifests", args: { trip, manifests: JSON.stringify(manifests) } })
					.then(() => { d.hide(); frappe.show_alert({ message: __("Attached"), indicator: "green" }); this._ops_load(); });
			},
		});
		d.show();
	}

	/* ── Map ──────────────────────────────────────────────────── */

	async _ops_map_toggle() {
		this.map_open = !this.map_open;
		const $wrap = $("#lcc-ops-map-wrap");
		if (!this.map_open) { $wrap.hide(); return; }
		$wrap.show();
		try { await this._ensure_leaflet(); } catch (err) {
			$("#lcc-ops-map-info").html(`<div class="lcc-empty">${__("Could not load map: {0}", [err.message || err])}</div>`);
			return;
		}
		this._ops_load_map();
	}

	_ensure_leaflet() {
		if (window.L) return Promise.resolve();
		if (this._leaflet_loading) return this._leaflet_loading;
		this._leaflet_loading = new Promise((resolve, reject) => {
			const css = document.createElement("link"); css.rel = "stylesheet";
			css.href = "/assets/frappe/js/lib/leaflet/leaflet.css";
			document.head.appendChild(css);
			const js = document.createElement("script");
			js.src = "/assets/frappe/js/lib/leaflet/leaflet.js";
			js.onload = resolve; js.onerror = () => reject(new Error("network"));
			document.head.appendChild(js);
		});
		return this._leaflet_loading;
	}

	async _ops_load_map() {
		const r = await frappe.call({ method: _LCC + "ops_map_data", args: { trip_date: this.trip_date, include_days: this.include_days } });
		const data = r.message || { trips: [], manifests_with_coords: 0, manifests_total: 0 };
		const $info = $("#lcc-ops-map-info");
		$info.html(`<span class="lcc-map-summary"><b>${data.trips.length}</b> ${__("trips")} · ${data.manifests_with_coords}/${data.manifests_total} ${__("manifests with coords")}</span>`);

		const el = document.getElementById("lcc-ops-map");
		if (this._map) { this._map.remove(); this._map = null; }
		this._map = L.map(el).setView([20.5937, 78.9629], 5);
		L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap" }).addTo(this._map);

		if (!data.trips.length || !data.manifests_with_coords) {
			$info.append(` <span class="lcc-muted">${__("No GPS coordinates yet.")}</span>`);
			return;
		}
		const sc = { Draft:"#9aa0a6", Assigned:"#f29900", Started:"#1a73e8", Completed:"#188038", Closed:"#5f6368" };
		const all_pts = [];
		data.trips.forEach((t) => {
			const clr = sc[t.status] || "#1a73e8";
			const pts = [];
			t.manifests.forEach((m) => {
				if (m.pickup) {
					const ll = [m.pickup.lat, m.pickup.lng];
					L.circleMarker(ll, { radius: 7, color: "#188038", fillColor: "#188038", fillOpacity: 0.85, weight: 2 })
						.bindPopup(`<b>${__("Pickup")}</b><br>${frappe.utils.escape_html(m.name)}`).addTo(this._map);
					pts.push(ll); all_pts.push(ll);
				}
				if (m.delivery) {
					const ll = [m.delivery.lat, m.delivery.lng];
					L.circleMarker(ll, { radius: 7, color: "#1a73e8", fillColor: "#1a73e8", fillOpacity: 0.85, weight: 2 })
						.bindPopup(`<b>${__("Delivery")}</b><br>${frappe.utils.escape_html(m.name)}`).addTo(this._map);
					pts.push(ll); all_pts.push(ll);
				}
			});
			if (pts.length >= 2) L.polyline(pts, { color: clr, weight: 3, opacity: 0.6 }).bindTooltip(`${t.trip} · ${t.status}`).addTo(this._map);
		});
		if (all_pts.length) this._map.fitBounds(L.latLngBounds(all_pts), { padding: [40, 40] });
	}

	// ══════════════════════════════════════════════════════════════
	// DIALOGS
	// ══════════════════════════════════════════════════════════════

	_dlg_new_trip(preselected_manifests = null) {
		const manifests = Array.isArray(preselected_manifests) && preselected_manifests.length
			? preselected_manifests : null;
		const title = manifests
			? __("New Trip — {0} manifest(s)", [manifests.length])
			: __("New Trip");
		const d = new frappe.ui.Dialog({
			title,
			fields: [
				{ fieldtype: "Date",     fieldname: "trip_date",     label: __("Trip Date"),   reqd: 1, default: this.trip_date },
				{ fieldtype: "Link",     fieldname: "company",       label: __("Company"),     options: "Company", reqd: 1, default: frappe.defaults.get_default("company") },
				// A consignment we are not carrying travels someone else's
				// network — no leg of ours to plan, no route of ours to walk.
				{ fieldtype: "Select", fieldname: "direction", label: __("Direction"),
					options: "Forward\nReverse\nMixed", default: "Forward",
					depends_on: "eval:doc.transport_mode=='Own'" },
				{ fieldtype: "Link", fieldname: "route", label: __("Route"), options: "CH Route",
					depends_on: "eval:doc.transport_mode=='Own'",
					description: __("Optional — pre-populates stops from this route. Leave blank to auto-resolve from stops once they're added, or when manifests are attached.") },
				{ fieldtype: "Column Break" },
				// Who carries it. Our own driver takes a vehicle; a courier
				// takes the consignment and hands back a tracking number, so
				// only one pair of fields can ever apply to a trip.
				{
					fieldtype: "Select", fieldname: "transport_mode", label: __("Mode"),
					options: "Own\nCourier\nOthers", default: "Own", reqd: 1,
				},
				{
					fieldtype: "Link", fieldname: "driver", label: __("Driver"), options: "Driver",
					depends_on: "eval:doc.transport_mode=='Own'",
					get_query: () => ({ query: "ch_logistics.api.logistics_api.unassigned_drivers_query" }),
					onchange: () => {
						const driver = d.get_value("driver");
						if (!driver || d.get_value("vehicle")) return;
						frappe.db.get_value("Driver", driver, "custom_default_vehicle", (r) => {
							if (r && r.custom_default_vehicle) d.set_value("vehicle", r.custom_default_vehicle);
						});
					},
				},
				{ fieldtype: "Link", fieldname: "vehicle", label: __("Vehicle"), options: "Vehicle",
					depends_on: "eval:doc.transport_mode=='Own'" },
				{
					fieldtype: "Link", fieldname: "courier_partner", label: __("Courier Partner"),
					options: "Courier Partner",
					depends_on: "eval:doc.transport_mode=='Courier'",
					mandatory_depends_on: "eval:doc.transport_mode=='Courier'",
				},
				{
					fieldtype: "Data", fieldname: "tracking_number",
					label: __("Tracking / AWB Number"),
					depends_on: "eval:doc.transport_mode=='Courier'",
					description: __("The courier's own number, if they have given one yet."),
				},
				{
					fieldtype: "Attach Image", fieldname: "packing_photo",
					label: __("Packing Photo"),
					depends_on: "eval:doc.transport_mode!='Own'",
					description: __("What was handed over — the only record of it once a courier has the box."),
				},
				{
					// A courier trip is typed in after the fact, so it can start
					// at the step it has already reached. Delivered is not here:
					// it books stock in, and a new trip has no manifests yet.
					fieldtype: "Select", fieldname: "status", label: __("Status"),
					options: "Draft\nAssigned\nPicked Up\nDelivery Pending",
					default: "Assigned",
					depends_on: "eval:doc.transport_mode!='Own'",
					description: __("Move it on from the trip itself once the carrier reports back."),
				},
				{
					fieldtype: "Data", fieldname: "carrier_name", label: __("Carried By"),
					depends_on: "eval:doc.transport_mode=='Others'",
					mandatory_depends_on: "eval:doc.transport_mode=='Others'",
					description: __("Who is carrying it — no driver or courier record will hold it."),
				},
				{ fieldtype: "Datetime", fieldname: "planned_start", label: __("Planned Start") },
				{ fieldtype: "Datetime", fieldname: "planned_end",   label: __("Planned End") },
				...(manifests ? [
					{ fieldtype: "Section Break", label: __("Attached Manifests") },
					{ fieldtype: "HTML",     fieldname: "mf_html",
						options: `<div class="text-muted" style="font-size:12px">${
							manifests.map((m) => frappe.utils.escape_html(m)).join(", ")
						}</div>` },
				] : []),
			],
			primary_action_label: manifests ? __("Create & Attach") : __("Create"),
			primary_action: (vals) => {
				const args = { ...vals };
				if (manifests) args.manifests = JSON.stringify(manifests);
				frappe.call({ method: _LCC + "trip_create", args, freeze: true }).then((r) => {
					d.hide();
					const msg = manifests
						? __("Trip {0} created with {1} manifest(s)", [r.message, manifests.length])
						: __("Trip {0} created", [r.message]);
					frappe.show_alert({ message: msg, indicator: "green" });
					if (this.mode === "ops") {
						this.selected_manifests.clear();
						this._ops_load();
						if (r.message) this._ops_open_trip(r.message);
					} else if (String(this.mode).startsWith("mf_")) {
						// The new trip is listed on the Trip tab — go there.
						this.selected_manifests.clear();
						this._switch_mode("mf_trip");
					} else {
						this._switch_mode("ops");
					}
				});
			},
		});
		d.show();
	}

	_ops_new_trip_from_selection() {
		if (!this.selected_manifests.size) return;
		this._dlg_new_trip(Array.from(this.selected_manifests));
	}

	/* ── Bundle into one consolidated pickup QR ──────────────────
	 * One-click "pack N manifests together, print ONE scannable
	 * label" — the dispatcher-side equivalent of how Ekart /
	 * Delhivery group a multi-shipment drop into a single driver scan.
	 *
	 * Hard rules (enforced both client- and server-side):
	 *   • All selected manifests must share the SAME source warehouse
	 *     (one physical pickup point).
	 *   • All selected manifests must share the SAME destination
	 *     store / warehouse (one physical drop point).
	 * Anything that violates these is refused with a clear modal
	 * before the API is ever called.
	 *
	 * Pipeline:
	 *   1. club_transfers_into_trip(selected) — creates one trip with
	 *      one stop (because all destinations are identical). The stop
	 *      owns a random pickup_token + delivery_token (length 22,
	 *      minted in _ensure_stop_tokens on the trip controller).
	 *   2. get_stop_label(trip, sequence, kind=pickup) — server-side
	 *      renders printable HTML with the QR data-URI embedded.
	 *   3. Open every label in a single print-ready window so the
	 *      packing team can hit Ctrl+P once and get the consolidated
	 *      pickup sheet.
	 */
	_dlg_auto_plan() {
		const d = new frappe.ui.Dialog({
			title: __("Auto-plan Trips"),
			fields: [
				{ fieldname: "trip_date",     fieldtype: "Date",   label: __("Trip Date"), reqd: 1, default: this.trip_date },
				{ fieldname: "direction",     fieldtype: "Select", label: __("Direction"), options: "Forward\nReverse\nMixed", default: "Forward", reqd: 1 },
				{ fieldname: "company",       fieldtype: "Link",   label: __("Company"),   options: "Company", reqd: 1, default: frappe.defaults.get_user_default("Company") },
				{ fieldname: "hub_warehouse", fieldtype: "Link",   label: __("Hub Warehouse"), options: "Warehouse" },
				{ fieldname: "max_stops",     fieldtype: "Int",    label: __("Max Stops / Trip"), default: 20 },
				{
					fieldname: "driver", fieldtype: "Link", label: __("Driver (optional)"), options: "Driver",
					get_query: () => ({ query: "ch_logistics.api.logistics_api.unassigned_drivers_query" }),
				},
			],
			primary_action_label: __("Preview"),
			primary_action: (vals) => {
				frappe.call({ method: _LCC + "auto_plan_trips", args: { ...vals, commit: 0 }, freeze: true })
					.then((r) => {
						const res = r.message || { proposals: [] };
						if (!res.proposals.length) {
							frappe.msgprint({ title: __("No proposals"), message: res.skipped_reason || __("No unassigned manifests match.") });
							return;
						}
						const html = res.proposals.map((p, i) =>
							`<div style="margin-bottom:8px"><b>${__("Trip")} ${i + 1}</b> — ${p.stops.length} ${__("stops")}, ${p.manifests.length} ${__("manifests")}
							<ul style="margin:4px 0 0 18px">${p.stops.map((s) => `<li>${frappe.utils.escape_html(s.store || s.warehouse)} <span class="text-muted">(${s.stop_type || "Drop"} · ${s.manifest_count})</span></li>`).join("")}</ul></div>`
						).join("");
						frappe.confirm(
							`<div>${__("Create")} <b>${res.proposals.length}</b> ${__("trip(s)?")}</div>
							 <div style="max-height:300px;overflow:auto;margin-top:8px">${html}</div>`,
							() => {
								frappe.call({ method: _LCC + "auto_plan_trips", args: { ...vals, commit: 1 }, freeze: true })
									.then((r2) => {
										const c = (r2.message && r2.message.created) || [];
										frappe.show_alert({ message: __("Created {0} trip(s)", [c.length]), indicator: "green" });
										d.hide();
										if (this.mode === "ops") this._ops_load();
									});
							}
						);
					});
			},
		});
		d.show();
	}

	// ══════════════════════════════════════════════════════════════
	// SHARED HELPERS
	// ══════════════════════════════════════════════════════════════

	_go_list(doctype, filters = {}) {
		const from = this.filters?.fields?.from_date?.get_value();
		const to   = this.filters?.fields?.to_date?.get_value();
		const co   = this.filters?.fields?.company?.get_value();
		if (co) filters.company = co;
		const df_map = { "CH Transfer Manifest": "manifest_date", "Stock Entry": "posting_date" };
		const df = df_map[doctype];
		if (df && from && to)  filters[df] = ["between", [from, to]];
		else if (df && from)   filters[df] = [">=", from];
		else if (df && to)     filters[df] = ["<=", to];
		frappe.set_route("List", doctype, filters);
	}
}
