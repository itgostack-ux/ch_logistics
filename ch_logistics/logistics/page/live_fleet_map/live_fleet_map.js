/**
 * Live Fleet Map — dispatcher view of every online driver, color-coded
 * by availability_status. Auto-refreshes every 10s and listens for live
 * driver alerts via frappe.realtime.
 */

frappe.pages["live-fleet-map"].on_page_load = function(wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Live Fleet"),
		single_column: true,
	});

	// Expose instance so info-window links (rendered outside the JS scope)
	// can invoke `showTrail()` without re-querying state.
	window._ch_fleet_instance = new ch_logistics.LiveFleetMap(page);
};

ch_logistics.LiveFleetMap = class LiveFleetMap {
	constructor(page) {
		this.page = page;
		this.wrapper = $(page.body);
		this.map = null;
		this.markers = {};       // driver -> google.maps.Marker
		this.info_windows = {};
		this.trail_path = null;
		this.config = null;
		this.refresh_timer = null;
		this.selected_driver = null;
		this.status_filter = null;

		this._build_layout();
		this._bind_filters();
		this._init();
	}

	_build_layout() {
		this.wrapper.addClass("ch-logistics-map-page");
		this.wrapper.html(`
			<div class="ch-logistics-map-canvas" id="ch-fleet-map-canvas"></div>

			<div class="ch-logistics-map-overlay">
				<h4>${__("Fleet Status")}</h4>
				<div id="ch-fleet-counts" style="font-size:12px;line-height:1.6;">
					${__("Loading...")}
				</div>
				<div style="margin-top:8px;">
					<select class="form-control input-xs" id="ch-fleet-status-filter">
						<option value="">${__("All Statuses")}</option>
						<option value="Available">Available</option>
						<option value="Assigned">Assigned</option>
						<option value="In Transit">In Transit</option>
						<option value="Break">Break</option>
						<option value="Idle">Idle</option>
					</select>
				</div>
			</div>

			<div class="ch-logistics-fleet-panel">
				<h4 style="margin:0 0 8px;font-size:14px;font-weight:600;">
					${__("Drivers Online")}
				</h4>
				<div id="ch-fleet-driver-list">
					<div style="color:#9ca3af;padding:10px;text-align:center;">
						${__("Loading drivers...")}
					</div>
				</div>
			</div>

			<div class="ch-logistics-map-banner" id="ch-fleet-banner"></div>
		`);

		this.page.set_secondary_action(__("Refresh Now"),
									   () => this._refresh());
	}

	_bind_filters() {
		this.wrapper.on("change", "#ch-fleet-status-filter", (e) => {
			this.status_filter = e.target.value || null;
			this._refresh();
		});
	}

	async _init() {
		try {
			this.config = await ch_logistics.maps.get_config();
		} catch (e) {
			this.config = {
				default_map_zoom: 12,
				default_map_center: { lat: 13.0827, lng: 80.2707 },
			};
		}

		try {
			await ch_logistics.maps.load(["geometry", "marker"]);
		} catch (e) {
			this._banner(__("Map could not load: {0}", [e.message]), "warning");
			return;
		}

		this._init_map();
		this._subscribe_realtime();
		await this._refresh();

		// Periodic refresh as a fallback for realtime gaps.
		this.refresh_timer = setInterval(() => this._refresh(), 10000);
	}

	_init_map() {
		this.map = new google.maps.Map(
			document.getElementById("ch-fleet-map-canvas"),
			{
				center: this.config.default_map_center || { lat: 13.0827, lng: 80.2707 },
				zoom: this.config.default_map_zoom || 12,
				mapTypeControl: false,
				streetViewControl: false,
				fullscreenControl: true,
			}
		);
	}

	_subscribe_realtime() {
		frappe.realtime.on("ch_logistics:driver_alert", (msg) => {
			this._banner(`${msg.title}: ${msg.message}`, "warning");
		});
		frappe.realtime.on("ch_logistics:stop_geofence_arrival", (msg) => {
			this._banner(
				__("Driver {0} reached stop {1}", [msg.driver, msg.sequence]),
				"success"
			);
			this._refresh();
		});
		frappe.realtime.on("ch_logistics:manifest_rejected", (msg) => {
			this._banner(
				__("Manifest {0} rejected by {1}: {2}",
					[msg.manifest, msg.driver, msg.reason]),
				"warning"
			);
		});
	}

	async _refresh() {
		try {
			const r = await frappe.call({
				method: "ch_logistics.api.tracking_api.get_live_drivers",
				args: { status: this.status_filter },
			});
			const drivers = (r && r.message) || [];
			this._render_counts(drivers);
			this._render_driver_list(drivers);
			this._render_markers(drivers);
		} catch (e) {
			console.error("fleet refresh failed", e);
		}
	}

	_render_counts(drivers) {
		const buckets = {};
		drivers.forEach((d) => {
			buckets[d.availability_status] = (buckets[d.availability_status] || 0) + 1;
		});
		const total = drivers.length;
		const html = `
			<div><strong>${__("Total")}</strong>: ${total}</div>
			${Object.entries(buckets).map(([k, v]) => `
				<div>
					<span class="ch-logistics-status-pill ${k.toLowerCase().replace(/\s+/g, "")}">${k}</span>
					${v}
				</div>
			`).join("")}
		`;
		$("#ch-fleet-counts").html(html);
	}

	_render_driver_list(drivers) {
		if (!drivers.length) {
			$("#ch-fleet-driver-list").html(`
				<div style="color:#9ca3af;padding:10px;text-align:center;">
					${__("No drivers online.")}
				</div>
			`);
			return;
		}
		const rows = drivers.map((d) => `
			<div class="ch-logistics-driver-row" data-driver="${frappe.utils.escape_html(d.name)}">
				<div>
					<div class="name">${frappe.utils.escape_html(d.full_name || d.name)}</div>
					<div class="meta">
						${d.current_trip ? d.current_trip + " · " : ""}
						${d.last_geo_at || ""}
					</div>
				</div>
				<span class="ch-logistics-status-pill ${(d.availability_status || "offline").toLowerCase().replace(/\s+/g, "")}">
					${d.availability_status || "?"}
				</span>
			</div>
		`).join("");
		$("#ch-fleet-driver-list").html(rows);

		this.wrapper.find(".ch-logistics-driver-row").off("click").on("click", (e) => {
			const drv = $(e.currentTarget).data("driver");
			this._focus_driver(drv);
		});
	}

	_render_markers(drivers) {
		const seen = new Set();
		drivers.forEach((d) => {
			seen.add(d.name);
			const pos = { lat: d.current_lat, lng: d.current_lng };
			const color = this._color_for(d.availability_status);
			if (this.markers[d.name]) {
				this.markers[d.name].setPosition(pos);
				this.markers[d.name].setIcon(this._icon(color));
			} else {
				const marker = new google.maps.Marker({
					position: pos,
					map: this.map,
					title: d.full_name || d.name,
					icon: this._icon(color),
				});
				const iw = new google.maps.InfoWindow();
				marker.addListener("click", () => {
					iw.setContent(this._popup_html(d));
					iw.open(this.map, marker);
				});
				this.markers[d.name] = marker;
				this.info_windows[d.name] = iw;
			}
		});

		// Remove markers for drivers no longer in the response.
		Object.keys(this.markers).forEach((name) => {
			if (!seen.has(name)) {
				this.markers[name].setMap(null);
				delete this.markers[name];
				delete this.info_windows[name];
			}
		});
	}

	_color_for(status) {
		const map = {
			"Available": "#10b981",
			"Assigned":  "#3b82f6",
			"In Transit": "#f59e0b",
			"Break":     "#f97316",
			"Idle":      "#8b5cf6",
			"Offline":   "#6b7280",
		};
		return map[status] || "#6b7280";
	}

	_icon(color) {
		return {
			path: google.maps.SymbolPath.CIRCLE,
			scale: 9,
			fillColor: color,
			fillOpacity: 1,
			strokeColor: "white",
			strokeWeight: 2,
		};
	}

	_popup_html(d) {
		return `
			<div style="min-width:200px;">
				<div style="font-weight:600;font-size:13px;">
					${frappe.utils.escape_html(d.full_name || d.name)}
				</div>
				<div style="margin:4px 0;">
					<span class="ch-logistics-status-pill ${(d.availability_status || "").toLowerCase().replace(/\s+/g,"")}">
						${d.availability_status || "?"}
					</span>
				</div>
				${d.current_trip ? `
					<div><strong>${__("Trip")}:</strong>
						<a href="/app/ch-logistics-trip/${d.current_trip}">${d.current_trip}</a>
					</div>` : ""}
				${d.cell_number ? `<div><strong>${__("Phone")}:</strong> ${d.cell_number}</div>` : ""}
				<div style="color:#6b7280;font-size:11px;margin-top:4px;">
					${d.last_geo_at || ""}
				</div>
				<div style="margin-top:6px;">
					<a href="#" data-driver="${frappe.utils.escape_html(d.name)}"
					   onclick="event.preventDefault();
								window._ch_show_trail && window._ch_show_trail('${d.name}');">
						${__("Show 1-hour trail")}
					</a>
				</div>
			</div>
		`;
	}

	_focus_driver(name) {
		const m = this.markers[name];
		if (!m) return;
		this.map.panTo(m.getPosition());
		google.maps.event.trigger(m, "click");
	}

	async _show_trail(driver) {
		try {
			const r = await frappe.call({
				method: "ch_logistics.api.tracking_api.get_driver_trail",
				args: { driver, minutes: 60, limit: 500 },
			});
			const pts = (r && r.message) || [];
			if (this.trail_path) this.trail_path.setMap(null);
			if (!pts.length) {
				this._banner(__("No recent points for this driver."), "warning");
				return;
			}
			const path = pts.map((p) => ({ lat: p.latitude, lng: p.longitude }));
			this.trail_path = new google.maps.Polyline({
				path,
				geodesic: true,
				strokeColor: "#9333ea",
				strokeOpacity: 0.8,
				strokeWeight: 3,
				map: this.map,
			});
		} catch (e) {
			console.error("trail failed", e);
		}
	}

	_banner(msg, level) {
		const b = $("#ch-fleet-banner");
		b.text(msg).attr("class",
			"ch-logistics-map-banner show " + (level || ""));
		setTimeout(() => b.removeClass("show"), 4000);
	}
};

window.ch_logistics = window.ch_logistics || {};
// Expose trail loader globally so the info-window link can call it.
window._ch_show_trail = function(driver) {
	const inst = window._ch_fleet_instance;
	if (inst) inst._show_trail(driver);
};
