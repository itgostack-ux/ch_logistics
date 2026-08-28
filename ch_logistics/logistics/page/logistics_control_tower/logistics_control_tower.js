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
const _OPT = "ch_logistics.api.optimizer.";

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

class LogisticsCommandCenter {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;

		// shared state
		this.mode = "overview";
		this.filters = null;		// hub_filters handle
		this._auto_timer = null;

		// ops state
		this.trip_date = frappe.datetime.get_today();
		this.include_days = 1;
		this.bottom_tab = "manifests";
		this.board = { buckets: {}, totals: {} };
		this.unassigned = [];
		this.exceptions = [];
		this.drivers = [];
		this.recalls = [];
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
		this.filters = ch_erp15.hub_filters.attach(this.page, {
			include_dates: true,
			on_change: () => { if (this.mode === "overview") this._ov_load(); },
		});

		// Root shell
		this.$root = $('<div class="lcc-root"></div>').appendTo(this.page.body);
		this._render_shell();
		// A full browser refresh recreates this page object from scratch,
		// so without remembering the tab, it always landed back on
		// Overview even if you'd been sitting in Operations. Restore
		// whichever tab was last open instead.
		let last_mode = "overview";
		try {
			last_mode = localStorage.getItem("lcc_last_mode") || "overview";
		} catch (e) { /* localStorage unavailable — fall back to Overview */ }
		this._switch_mode(["overview", "draft", "pending_with_goods", "packing", "ops"].includes(last_mode) ? last_mode : "overview");
	}

	// ─────────────────────────────────────────────────────────────
	// SHELL
	// ─────────────────────────────────────────────────────────────

	_render_shell() {
		this.$root.html(`
			<div class="lcc-modebar">
				<div class="lcc-modebar-tabs">
					<button class="lcc-mode-btn" data-mode="overview">
						<i class="fa fa-tachometer"></i> ${__("Overview")}
					</button>
					<button class="lcc-mode-btn" data-mode="draft">
						<i class="fa fa-file-text-o"></i> ${__("Draft")}
					</button>
					<button class="lcc-mode-btn" data-mode="pending_with_goods">
						<i class="fa fa-clock-o"></i> ${__("Pending With Goods")}
					</button>
					<button class="lcc-mode-btn" data-mode="packing">
						<i class="fa fa-cube"></i> ${__("Packed Order")}
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
			const route = [r.source_store || r.source_warehouse, r.destination_store || r.destination_warehouse].filter(Boolean).join(" → ");
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
			const route = [r.source_store || r.source_warehouse, r.destination_store || r.destination_warehouse].filter(Boolean).join(" → ");
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
			: __("Showing Material Transfers Pending With Goods — not yet packed. Click a column heading to sort.");
		$("#lcc-content").html(`
			<div class="lcc-pack" id="lcc-draft-pending">
				<div class="lcc-ops-bar">
					<div class="lcc-ops-bar-actions">
						<button class="btn btn-xs btn-default lcc-dp-refresh-btn">
							<i class="fa fa-refresh"></i> ${__("Refresh")}
						</button>
					</div>
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
			$r.on("click", ".lcc-dp-open", (e) => { e.preventDefault(); frappe.set_route("Form", "Stock Entry", $(e.currentTarget).data("name")); });
			$r.on("click", ".lcc-dp-sortable", (e) => {
				const field = $(e.currentTarget).data("field");
				const st = this.dp_state;
				st.sort_order = (st.sort_field === field && st.sort_order === "asc") ? "desc" : "asc";
				st.sort_field = field;
				st.start = 0;
				this._draft_pending_load();
			});
			$r.on("click", ".lcc-dp-page-prev:not([disabled])", () => {
				const st = this.dp_state;
				st.start = Math.max(0, st.start - st.page_length);
				this._draft_pending_load();
			});
			$r.on("click", ".lcc-dp-page-next:not([disabled])", () => {
				const st = this.dp_state;
				if (st.start + st.page_length < (this.dp_total_count || 0)) {
					st.start += st.page_length;
					this._draft_pending_load();
				}
			});
			this._dp_events_bound = true;
		}
		this._draft_pending_load();
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

	_draft_pending_render() {
		const $b = $("#lcc-dp-body");
		const $p = $("#lcc-dp-pagination");
		if (!(this.draft_pending_queue || []).length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No Draft or Pending With Goods Stock Entries.")}</div>`);
			$p.html("");
			return;
		}
		const status_colors = {
			"Draft": "red", "Pending With Goods": "orange", "Partially Packed": "yellow",
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
				<td><a href="#" class="lcc-dp-open" data-name="${nm}">${nm}</a></td>
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
					${this._dp_sort_th(__("Order Id"), "name")}
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
		$p.html(`
			<div class="lcc-dp-page-info">
				${__("Showing {0}-{1} of {2}", [from, to, total])}
			</div>
			<div class="lcc-dp-page-btns">
				<button class="btn btn-xs btn-default lcc-dp-page-prev" ${st.start <= 0 ? "disabled" : ""}>
					<i class="fa fa-chevron-left"></i> ${__("Prev")}
				</button>
				<button class="btn btn-xs btn-default lcc-dp-page-next" ${(st.start + st.page_length >= total) ? "disabled" : ""}>
					${__("Next")} <i class="fa fa-chevron-right"></i>
				</button>
			</div>
		`);
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
					<span class="lcc-muted lcc-ops-bar-hint">
						<i class="fa fa-info-circle"></i>
						${__("Showing fully Packed Stock Entries not yet on a manifest. Tick some and click Create Manifest to group them for dispatch.")}
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
			$r.on("click", ".lcc-pack-create-manifest-btn", () => this._pack_create_manifest());
			$r.on("click", ".lcc-pack-open",                (e) => { e.preventDefault(); frappe.set_route("Form", "Stock Entry", $(e.currentTarget).data("name")); });
			$r.on("change", ".lcc-pack-select-all", (e) => {
				$r.find(".lcc-pack-row-check").prop("checked", e.currentTarget.checked);
				this._pack_update_create_btn();
			});
			$r.on("change", ".lcc-pack-row-check", () => this._pack_update_create_btn());
			this._pack_events_bound = true;
		}
		this._pack_load();
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

	_pack_render() {
		const $b = $("#lcc-pack-body");
		if (!(this.pack_queue || []).length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No fully Packed Stock Entries waiting to be grouped into a manifest.")}</div>`);
			return;
		}
		const rows = this.pack_queue.map((se) => {
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

			// Same status colors as the Stock Entry list view (stock_entry_list.js)
			// — this queue is filtered to custom_status == "Packed" so it'll
			// always render teal here, but kept as a real lookup rather than a
			// hardcoded color so it stays correct if the filter ever loosens.
			const status_colors = {
				"Draft": "red", "Pending With Goods": "orange", "Partially Packed": "yellow",
				"Packed": "teal", "Ready For Pickup": "teal", "Assigned": "blue",
				"Ready For Receive": "cyan", "In Transit": "yellow", "Receive At Transit": "purple",
				"Partially Transferred": "orange", "Transferred": "green", "Force Closed": "gray",
				"Rejected": "red",
			};
			const status = se.custom_status || "";
			const status_color = status_colors[status] || "gray";
			const since = se.custom_status_since || se.creation;
			return `<tr>
				<td><input type="checkbox" class="lcc-pack-row-check" data-name="${nm}"></td>
				<td><a href="#" class="lcc-pack-open" data-name="${nm}">${nm}</a></td>
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
			<div class="lcc-table-wrap"><table class="lcc-table lcc-pack-table">
				<thead><tr>
					<th style="width:32px"><input type="checkbox" class="lcc-pack-select-all"></th>
					<th>${__("Order Id")}</th>
					<th>${__("Date")}</th>
					<th>${__("Status")}</th>
					<th>${__("Source Warehouse")}</th>
					<th>${__("Target Warehouse")}</th>
					<th class="tr">${__("Qty")}</th>
					<th class="tr">${__("Boxes")}</th>
					<th class="tr">${__("Weight")}</th>
					<th>${__("Age")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>
		`);
		this._pack_update_create_btn();
	}

	_pack_update_create_btn() {
		const $r = this.$root;
		const n = $r.find(".lcc-pack-row-check:checked").length;
		const $btn = $r.find(".lcc-pack-create-manifest-btn");
		$btn.prop("disabled", n === 0);
		$btn.html(`<i class="fa fa-plus"></i> ${__("Create Manifest")}${n ? ` (${n})` : ""}`);
		const total = $r.find(".lcc-pack-row-check").length;
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
		const names = $r.find(".lcc-pack-row-check:checked").map((_, el) => $(el).data("name")).get();
		if (!names.length) {
			frappe.msgprint(__("Tick at least one Stock Entry."));
			return;
		}
		// No exact-route pre-check here — a manifest can now carry a
		// multi-leg/chained route (e.g. Annanagar->Ashok Nagar plus
		// Ashok Nagar->Chennai Hub), which group_stock_entries_into_manifest
		// resolves server-side via zone matching on the manifest's own
		// Stops table. It still refuses (with a clear message) if the
		// ticked entries genuinely don't connect.
		frappe.call({
			method: "ch_erp15.ch_erp15.custom.stock_entry.group_stock_entries_into_manifest",
			args: { stock_entries: JSON.stringify(names) },
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
			frappe.db.get_doc("CH Transfer Manifest", manifest_name).then((doc) => {
				doc.status = "Packed";
				return frappe.call({ method: "frappe.client.submit", args: { doc } });
			}).then(() => {
				frappe.show_alert({
					message: __("Manifest {0} created with {1} Stock Entry(s) and moved to Operations.", [manifest_name, names.length]),
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
					<button class="lcc-tab" data-tab="exceptions">
						<i class="fa fa-exclamation-triangle"></i> ${__("Exception Inbox")}
						<span class="lcc-tab-badge lcc-tab-badge-warn" id="lcc-cnt-exc">0</span>
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
		$r.on("click",  ".lcc-bundle-print-qr",   () => this._ops_bundle_print_qr());

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
		$r.on("click",  ".lcc-exc-resolve",   (e) => this._ops_resolve_exc($(e.currentTarget).data("trip"), $(e.currentTarget).data("row")));
		$r.on("click",  "#lcc-side-assign",   () => this._ops_assign_driver());
		$r.on("click",  "#lcc-side-start",    () => this._ops_start_trip());
		$r.on("click",  "#lcc-side-complete", () => this._ops_trip_action("trip_complete"));
		$r.on("click",  "#lcc-side-complete-override", () => this._ops_trip_action("trip_complete", _LCC, { override_exceptions: 1 }));
		$r.on("click",  "#lcc-side-resequence", () => this._ops_trip_action("resequence_trip", _OPT));
		$r.on("click",  "#lcc-side-close-trip",()=> this._ops_trip_action("trip_close"));
		$r.on("click",  "#lcc-side-cancel",   () => this._ops_trip_action("trip_unassign"));
		$r.on("click",  "#lcc-side-cancel-trip", () => this._ops_cancel_trip());
		$r.on("click",  "#lcc-side-recall-trip", () => this._ops_recall_trip());
		$r.on("click",  ".lcc-side-detach",   (e) => this._ops_detach_manifest($(e.currentTarget).data("name")));
		$r.on("click",  ".lcc-trip-link",     (e) => { e.preventDefault(); this._ops_open_trip($(e.currentTarget).data("name")); });

		// Per-row print actions for the Operations → Manifests panel.
		$r.on("click", ".lcc-mf-open",          (e) => { e.preventDefault(); this._ops_show_manifest_items($(e.currentTarget).data("name")); });
		$r.on("click", ".lcc-mf-print-box",     (e) => { e.preventDefault(); this._ops_print_manifest($(e.currentTarget).data("name"), "CH Transfer Manifest Label"); });
		$r.on("click", ".lcc-mf-print-receipt", (e) => { e.preventDefault(); this._ops_print_manifest($(e.currentTarget).data("name"), "ePOD Transfer Manifest"); });
		$r.on("click", ".lcc-mf-print-ewb",     (e) => { e.preventDefault(); this._ops_print_ewaybills($(e.currentTarget).data("name")); });

		// Recalls inbox — confirm physical return + reverse stock.
		$r.on("click", ".lcc-rec-confirm",      (e) => { e.preventDefault(); this._ops_confirm_recall_return($(e.currentTarget).data("name")); });
	}

	async _ops_load() {
		const co = this.filters?.fields?.company?.get_value() || undefined;
		const args_board = { trip_date: this.trip_date, include_days: this.include_days };
		if (co) args_board.company = co;

		const [b, u, x, d, rc, lc] = await Promise.all([
			frappe.call({ method: _LCC + "ops_board",              args: args_board }),
			frappe.call({ method: _LCC + "ops_unassigned_manifests", args: { limit: 100 } }),
			frappe.call({ method: _LCC + "ops_exception_inbox",     args: { resolution_status: "Open", limit: 100 } }),
			frappe.call({ method: _LCC + "ops_drivers_available" }),
			frappe.call({ method: _LCC + "ops_recall_inbox",        args: { limit: 100 } }),
			frappe.call({ method: _LCC + "ops_lifecycle_counts",    args: args_board }),
		]);

		this.board      = b.message || { buckets: {}, totals: {} };
		this.unassigned = u.message || [];
		this.exceptions = x.message || [];
		this.drivers    = d.message || [];
		this.recalls    = rc.message || [];
		this.lifecycle  = lc.message || { stages: [] };
		this.selected_manifests.clear();

		this._ops_render_lifecycle();
		this._ops_render_board();
		this._ops_render_kpi();
		this._ops_render_bottom();
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
			const $col = $(`
				<div class="lcc-ops-col${dim}${hi}">
					<div class="lcc-ops-col-head lcc-ops-s-${status.toLowerCase().replace(/ /g,"-")}">
						${__(status)} <span class="lcc-ops-col-cnt">${trips.length}</span>
					</div>
					<div class="lcc-ops-col-body"></div>
				</div>`).appendTo($board);
			trips.forEach((t) => $col.find(".lcc-ops-col-body").append(this._trip_card(t)));
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
		const exc_cnt = this.exceptions.length;
		const rec_cnt = (this.recalls || []).length;
		$("#lcc-cnt-mf").text(this.unassigned.length);
		$("#lcc-cnt-exc").text(exc_cnt).toggleClass("has-items", exc_cnt > 0);
		$("#lcc-cnt-rec").text(rec_cnt).toggleClass("has-items", rec_cnt > 0);
		$("#lcc-cnt-drv").text(this.drivers.length);

		const $b = $("#lcc-ops-bottom").empty();
		if (this.bottom_tab === "manifests")  return this._ops_render_manifests($b);
		if (this.bottom_tab === "recalls")    return this._ops_render_recalls($b);
		if (this.bottom_tab === "exceptions") return this._ops_render_exceptions($b);
		if (this.bottom_tab === "drivers")    return this._ops_render_drivers($b);
	}

	_ops_render_manifests($b) {
		// Optional lifecycle filter: when a stage chip (draft / packed) is
		// active, narrow the panel to just that status. Otherwise show
		// everything attachable, like before.
		const want = this._ops_manifest_status_filter;
		const list = want
			? (this.unassigned || []).filter((m) => (m.status || "").toLowerCase() === want.toLowerCase())
			: (this.unassigned || []);
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
		const rows = list.map((m) => {
			const color = STATUS_COLOR[m.status] || "gray";
			const status = frappe.utils.escape_html(STATUS_LABEL[m.status] || m.status || "—");
			const nm = encodeURIComponent(m.name);
			return `<tr>
			<td><input type="checkbox" class="lcc-mf-check" data-name="${m.name}"></td>
			<td><a href="/app/ch-transfer-manifest/${nm}" class="lcc-mf-open" data-name="${frappe.utils.escape_html(m.name)}">${frappe.utils.escape_html(m.name)}</a></td>
			<td><span class="indicator-pill ${color}">${status}</span></td>
			<td>${frappe.utils.escape_html(m.direction || "—")}</td>
			<td><span class="lcc-prio lcc-prio-${(m.shipment_priority || "Normal").toLowerCase()}">${m.shipment_priority || "Normal"}</span></td>
			<td>${window.ch_wh_label_html ? ch_wh_label_html(m.source_warehouse, "—") : frappe.utils.escape_html(m.source_warehouse || "—")} → ${window.ch_wh_label_html ? ch_wh_label_html(m.destination_warehouse, "—") : frappe.utils.escape_html(m.destination_warehouse || "—")}</td>
			<td class="tr">${m.total_qty || 0}</td>
			<td class="tr">${m.box_count || 0}</td>
			<td>${frappe.datetime.str_to_user(m.creation)}</td>
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
				<button class="btn btn-sm btn-warning lcc-bundle-print-qr" disabled
					title="${__("Group selected manifests into one trip per destination and print the consolidated pickup QR — driver scans once per drop.")}">
					<i class="fa fa-qrcode"></i> ${__("Bundle & Print Pickup QR")}
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
					<th>${__("Route")}</th><th class="tr">${__("Qty")}</th>
					<th class="tr">${__("Boxes")}</th><th>${__("Created")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table></div>`);
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
		const d = new frappe.ui.Dialog({
			title: __("Manifest {0}", [name]),
			size: "large",
			fields: [{ fieldname: "items_html", fieldtype: "HTML" }],
		});
		d.fields_dict.items_html.$wrapper.html(
			`<div class="lcc-loading"><i class="fa fa-spinner fa-spin"></i> ${__("Loading…")}</div>`
		);
		d.show();
		d.$wrapper.on("click", ".lcc-item-open", (e) => {
			e.preventDefault();
			const $t = $(e.currentTarget);
			this._ops_show_order_serials($t.data("order"), $t.data("item"));
		});
		try {
			const r = await frappe.call({
				method: _LCC + "get_manifest_items_bulk",
				args: { manifests: [name] },
			});
			const rows = (r.message && r.message[name]) || [];
			if (!rows.length) {
				d.fields_dict.items_html.$wrapper.html(
					`<div class="lcc-empty">${__("No item detail found for this manifest.")}</div>`
				);
				return;
			}
			// Level 1: Stock Entry (Order Id) — grouped, not a flat repeated
			// column, so a manifest with several Stock Entries reads as
			// distinct shipments. Level 2 (Item Name, below) drills into
			// Level 3 (IMEI) via _ops_show_order_serials, same as before.
			const esc = frappe.utils.escape_html;
			const groups = {};
			for (const row of rows) {
				const key = row.stock_entry || "—";
				(groups[key] = groups[key] || []).push(row);
			}
			const stock_entries = Object.keys(groups).sort();
			const sections = stock_entries.map((se) => {
				const item_rows = groups[se].map((row) => `<tr>
						<td>
							<a href="#" class="lcc-item-open"
								data-order="${esc(se)}"
								data-item="${esc(row.item_code || "")}">
								${esc(row.item_name || row.item_code || "—")}
							</a>
						</td>
						<td class="tr">${row.qty != null ? row.qty : "—"}${row.uom ? " " + esc(row.uom) : ""}</td>
						<td><span class="indicator-pill ${this._status_color(row.status)}"><span>${esc(this._status_label(row.status))}</span></span></td>
					</tr>`).join("");
				return `
					<div class="lcc-se-group" style="margin-bottom:16px">
						<div class="lcc-se-group-header" style="font-weight:700;font-size:13px;padding:4px 0 6px;border-bottom:2px solid #e5e7eb;display:flex;align-items:center;gap:8px">
							<i class="fa fa-file-text-o" style="color:#888"></i>
							<a href="/app/stock-entry/${encodeURIComponent(se)}" target="_blank">${esc(se)}</a>
							<span class="lcc-muted" style="font-weight:400;font-size:11px">${groups[se].length} ${__("item(s)")}</span>
						</div>
						<table class="lcc-table">
							<thead><tr>
								<th>${__("Item Name")}</th>
								<th class="tr">${__("Qty")}</th>
								<th>${__("Status")}</th>
							</tr></thead>
							<tbody>${item_rows}</tbody>
						</table>
					</div>`;
			}).join("");
			d.fields_dict.items_html.$wrapper.html(
				`<div class="lcc-table-wrap">${sections}</div>`
			);
		} catch (e) {
			d.fields_dict.items_html.$wrapper.html(
				`<div class="lcc-empty"><i class="fa fa-exclamation-triangle"></i> ${__("Failed to load manifest items.")}</div>`
			);
		}
	}

	async _ops_show_order_serials(order_id, item_code) {
		const d = new frappe.ui.Dialog({
			title: __("Order {0}", [order_id]),
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

	_ops_render_exceptions($b) {
		if (!this.exceptions.length) {
			$b.html(`<div class="lcc-empty"><i class="fa fa-check-circle"></i> ${__("No open exceptions.")}</div>`);
			return;
		}
		const rows = this.exceptions.map((e) => `<tr>
			<td><span class="lcc-sev lcc-sev-${(e.severity || "medium").toLowerCase()}">${e.severity || ""}</span></td>
			<td>${frappe.utils.escape_html(e.exception_type || "")}</td>
			<td>
				<a href="#" class="lcc-trip-link" data-name="${e.trip}">${e.trip}</a>
				<div class="lcc-muted">${frappe.utils.escape_html(e.driver_name || "")}</div>
			</td>
			<td>${e.stop_sequence || "—"}</td>
			<td class="lcc-exc-remarks">${frappe.utils.escape_html(e.remarks || "")}</td>
			<td>${frappe.datetime.str_to_user(e.occurred_at)}</td>
			<td>
				<button class="btn btn-xs btn-success lcc-exc-resolve"
					data-trip="${e.trip}" data-row="${e.row_name}">${__("Resolve")}</button>
			</td>
		</tr>`).join("");

		$b.html(`<div class="lcc-table-wrap"><table class="lcc-table">
			<thead><tr>
				<th>${__("Severity")}</th><th>${__("Type")}</th><th>${__("Trip / Driver")}</th>
				<th>${__("Stop")}</th><th>${__("Remarks")}</th><th>${__("When")}</th><th></th>
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
		this.$root.find(".lcc-bundle-print-qr").prop("disabled", n < 2);
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
		const can_detach = ["Draft", "Assigned", "Started"].includes(t.status);
		const can_resequence = ["Assigned", "Started"].includes(t.status);
		// Server-resolved capability (CH Logistics Settings → Role Matrix);
		// cosmetic only — trip_close re-checks head_override server-side.
		const can_complete_override = can_complete && !!(this._capabilities || {}).head_override;

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
			return `<div class="lcc-side-stop">
				<div class="lcc-side-stop-head">
					<b>#${s.sequence}</b> ${window.ch_wh_label_html ? ch_wh_label_html(s.warehouse, "") : frappe.utils.escape_html(s.warehouse || "")}
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
			<div class="lcc-side-actions">
				${can_assign   ? `<button class="btn btn-sm btn-default" id="lcc-side-assign"><i class="fa fa-user-plus"></i> ${__("Assign Driver")}</button>` : ""}
				${can_start    ? `<button class="btn btn-sm btn-warning" id="lcc-side-start" disabled title="${__("Starting a trip from here is currently disabled")}"><i class="fa fa-play"></i> ${__("Start")}</button>` : ""}
				${can_resequence ? `<button class="btn btn-sm btn-default" id="lcc-side-resequence"><i class="fa fa-random"></i> ${__("Re-sequence")}</button>` : ""}
				${can_complete ? `<button class="btn btn-sm btn-success" id="lcc-side-complete" disabled title="${__("Completing a trip from here is currently disabled")}"><i class="fa fa-check"></i> ${__("Complete")}</button>` : ""}
				${can_complete_override ? `<button class="btn btn-sm btn-danger" id="lcc-side-complete-override"><i class="fa fa-shield"></i> ${__("Complete (Override Exceptions)")}</button>` : ""}
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
				{ fieldtype: "Small Text", fieldname: "reason", label: __("Reason"), reqd: 1 },
			],
			primary_action_label: __("Cancel Trip"),
			primary_action: (vals) => {
				const reason = (vals.reason || "").trim();
				if (!reason) {
					frappe.show_alert({ message: __("A reason is required."), indicator: "red" });
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

	_ops_resolve_exc(trip, row_name) {
		frappe.call({ method: _LCC + "exception_resolve", args: { trip, row_name, resolution_status: "Resolved" } })
			.then(() => { frappe.show_alert({ message: __("Exception resolved"), indicator: "green" }); this._ops_load(); });
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
				{ fieldtype: "Select",   fieldname: "direction",     label: __("Direction"),   options: "Forward\nReverse\nMixed", default: "Forward" },
				{ fieldtype: "Link",     fieldname: "route",         label: __("Route"),       options: "CH Route",
					description: __("Optional — pre-populates stops from this route. Leave blank to auto-resolve from stops once they're added, or when manifests are attached.") },
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Link", fieldname: "driver", label: __("Driver"), options: "Driver",
					get_query: () => ({ query: "ch_logistics.api.logistics_api.unassigned_drivers_query" }),
					onchange: () => {
						const driver = d.get_value("driver");
						if (!driver || d.get_value("vehicle")) return;
						frappe.db.get_value("Driver", driver, "custom_default_vehicle", (r) => {
							if (r && r.custom_default_vehicle) d.set_value("vehicle", r.custom_default_vehicle);
						});
					},
				},
				{ fieldtype: "Link",     fieldname: "vehicle",       label: __("Vehicle"),     options: "Vehicle" },
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
	async _ops_bundle_print_qr() {
		const selected = Array.from(this.selected_manifests || []);
		if (selected.length < 2) {
			frappe.show_alert({ message: __("Select at least 2 manifests to bundle."), indicator: "orange" }, 5);
			return;
		}
		const co = this.filters?.fields?.company?.get_value() || frappe.defaults.get_user_default("Company");
		// A bundle = one pickup QR + one delivery QR. That only makes
		// sense when every selected manifest shares (a) the same source
		// warehouse — the one physical pickup point — AND (b) the same
		// destination store/warehouse — the one physical drop point.
		// Mixed sources break the pickup label; mixed destinations break
		// the delivery label.
		const rows  = (this.unassigned || []).filter((m) => selected.includes(m.name));
		const _key  = (r) => r.destination_store || r.destination_warehouse || "";
		const sources = new Set(rows.map((r) => r.source_warehouse).filter(Boolean));
		const dests   = new Set(rows.map(_key).filter(Boolean));
		if (sources.size > 1) {
			frappe.msgprint({
				title: __("Cannot bundle"),
				message: __("Selected manifests are picked up from {0} different warehouses. A single pickup QR only works for one pickup location.", [sources.size]),
				indicator: "red",
			});
			return;
		}
		if (dests.size > 1) {
			const list = Array.from(dests).map(frappe.utils.escape_html).join(", ");
			frappe.msgprint({
				title: __("Cannot bundle"),
				message: __("Selected manifests are dropping at {0} different destinations ({1}). To share one pickup &amp; delivery QR, all manifests in a bundle must go to the <b>same destination store</b>.", [dests.size, list]),
				indicator: "red",
			});
			return;
		}
		if (sources.size === 0) {
			frappe.show_alert({ message: __("Selected manifests have no source warehouse set."), indicator: "red" }, 7);
			return;
		}
		if (dests.size === 0) {
			frappe.show_alert({ message: __("Selected manifests have no destination store set."), indicator: "red" }, 7);
			return;
		}
		const source_warehouse = sources.values().next().value;

		frappe.dom.freeze(__("Bundling manifests and minting pickup QR…"));
		try {
			const club = await frappe.call({
				method: "ch_logistics.api.logistics_api.club_transfers_into_trip",
				args: {
					source_warehouse,
					manifests: selected,
					trip_date: this.trip_date || frappe.datetime.get_today(),
					company:   co,
					enforce_single_destination: 1,
				},
			});
			const res = (club && club.message) || {};
			const trip = res.trip;
			const stops = res.stops || [];
			if (!trip || !stops.length) {
				frappe.dom.unfreeze();
				frappe.msgprint({ title: __("Bundle Failed"), message: __("Server did not return a trip."), indicator: "red" });
				return;
			}

			// Pull every stop's printable label in parallel.
			const labels = await Promise.all(stops.map((s) =>
				frappe.call({
					method: "ch_logistics.api.logistics_api.get_stop_label",
					args: { trip, sequence: s.sequence, kind: "pickup" },
				}).then((r) => r.message)
			));

			frappe.dom.unfreeze();

			// Render in a print-ready dialog. One page-break per stop so
			// hitting Print produces a sheet per drop.
			const sheets = labels.map((lb, i) => `
				<div class="lcc-bundle-sheet" style="page-break-after:${i < labels.length - 1 ? "always" : "auto"};">
					${(lb && lb.html) || `<div class="lcc-empty">${__("Label render failed for stop {0}", [stops[i].sequence])}</div>`}
				</div>
			`).join("");

			const d = new frappe.ui.Dialog({
				title: __("Pickup QR — Trip {0}", [trip]),
				size: "large",
				fields: [{
					fieldname: "html", fieldtype: "HTML",
					options: `
						<div class="lcc-bundle-summary alert alert-success" style="padding:8px 12px;margin-bottom:10px">
							<i class="fa fa-check-circle"></i>
							${__("Created trip <b>{0}</b> with <b>{1}</b> destination stop(s) covering <b>{2}</b> manifest(s). Each stop has one consolidated pickup QR — driver scans once per drop.", [trip, stops.length, selected.length])}
						</div>
						<div class="lcc-bundle-sheets">${sheets}</div>
					`,
				}],
				primary_action_label: __("Print"),
				primary_action: () => {
					// Frappe-style print: open the rendered HTML in a new
					// window so the browser print dialog scopes to JUST
					// the labels (not the desk chrome).
					const w = window.open("", "_blank", "width=480,height=720");
					w.document.write(`
						<html><head><title>${frappe.utils.escape_html(trip)} — ${__("Pickup QR")}</title>
						<style>
							body { font-family: Arial, sans-serif; margin: 0; padding: 16px; background:#fff; }
							.lcc-bundle-sheet { margin: 0 auto 24px auto; }
							@media print { .lcc-bundle-sheet { page-break-after: always; } .lcc-bundle-sheet:last-child { page-break-after: auto; } }
						</style>
						</head><body>${sheets}<script>window.onload=()=>setTimeout(()=>window.print(),200);</script></body></html>
					`);
					w.document.close();
				},
				secondary_action_label: __("Open Trip"),
				secondary_action: () => {
					d.hide();
					frappe.set_route("Form", "CH Logistics Trip", trip);
				},
			});
			d.show();

			// Refresh the panel so the bundled manifests disappear from
			// the unassigned list (they now have a trip).
			this.selected_manifests.clear();
			this._ops_load();
		} catch (err) {
			frappe.dom.unfreeze();
			const msg = (err && err.message) || __("Unknown error");
			frappe.msgprint({ title: __("Bundle Failed"), message: msg, indicator: "red" });
		}
	}

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
