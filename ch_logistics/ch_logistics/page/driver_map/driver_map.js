/**
 * Driver Map — driver's own live position + next stop + ETA.
 *
 * Lifecycle:
 *   1. Load Google Maps via the server-signed loader (no key in JS).
 *   2. Start navigator.geolocation.watchPosition.
 *   3. Every cadence-sec push position to server (POST ping_location).
 *   4. Update the blue dot on the map; recenter when off-screen.
 *   5. If on a trip, fetch trip stops + plot next-stop pin + ETA.
 */

frappe.pages["driver-map"].on_page_load = function(wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("My Map"),
		single_column: true,
	});

	new ch_logistics.DriverMap(page);
};

ch_logistics.DriverMap = class DriverMap {
	constructor(page) {
		this.page = page;
		this.wrapper = $(page.body);
		this.map = null;
		this.self_marker = null;
		this.stop_marker = null;
		this.path = null;
		this.geo_watch_id = null;
		this.ping_timer = null;
		this.config = null;
		this.last_ping_at = 0;
		this.last_pos = null;
		this.current_trip = null;

		this._build_layout();
		this._init();
	}

	_build_layout() {
		this.wrapper.addClass("ch-logistics-map-page");
		this.wrapper.html(`
			<div class="ch-logistics-map-canvas" id="ch-driver-map-canvas"></div>
			<div class="ch-logistics-map-overlay">
				<h4>${__("My Position")}</h4>
				<div class="status-line">
					<span class="ch-logistics-status-pill offline" id="ch-driver-status">--</span>
				</div>
				<div class="meta" id="ch-driver-meta" style="color:#6b7280;font-size:11px;margin-top:6px;">
					${__("Waiting for GPS...")}
				</div>
				<div class="meta" id="ch-driver-trip-meta" style="color:#374151;font-size:12px;margin-top:6px;"></div>
				<div class="meta" id="ch-driver-eta" style="color:#065f46;font-size:13px;font-weight:600;margin-top:6px;"></div>
			</div>
			<div class="ch-logistics-map-banner" id="ch-driver-banner"></div>
		`);

		this.page.add_action_icon("refresh", () => this._refresh_trip());
		this.page.set_secondary_action(__("Recenter"), () => this._recenter());
	}

	async _init() {
		try {
			this.config = await ch_logistics.maps.get_config();
		} catch (e) {
			console.error("Failed to load tracking config", e);
			this.config = {
				default_map_zoom: 13,
				default_map_center: { lat: 13.0827, lng: 80.2707 },
				recommended_interval_sec: 15,
			};
		}

		try {
			await ch_logistics.maps.load(["geometry", "marker"]);
		} catch (e) {
			this._banner(__("Map could not load: {0}", [e.message]), "warning");
			return;
		}

		this._init_map();
		await this._refresh_trip();
		this._start_geolocation();
	}

	_init_map() {
		const center = this.config.default_map_center || { lat: 13.0827, lng: 80.2707 };
		this.map = new google.maps.Map(document.getElementById("ch-driver-map-canvas"), {
			center,
			zoom: this.config.default_map_zoom || 13,
			mapTypeControl: false,
			streetViewControl: false,
			fullscreenControl: true,
		});
	}

	async _refresh_trip() {
		try {
			const r = await frappe.call({
				method: "ch_logistics.api.tracking_api.get_driver_last_position",
			});
			const info = r && r.message;
			if (!info) return;

			$("#ch-driver-status")
				.text(info.status || "Offline")
				.attr("class",
					"ch-logistics-status-pill " + (info.status || "offline")
						.toLowerCase().replace(/\s+/g, ""));

			if (info.trip) {
				this.current_trip = info.trip;
				$("#ch-driver-trip-meta").html(`
					${__("On Trip")}:
					<a href="/app/ch-logistics-trip/${info.trip}">${info.trip}</a>
				`);
				await this._plot_next_stop(info.trip);
			} else {
				this.current_trip = null;
				$("#ch-driver-trip-meta").text(__("No active trip."));
				if (this.stop_marker) {
					this.stop_marker.setMap(null);
					this.stop_marker = null;
				}
				if (this.path) {
					this.path.setMap(null);
					this.path = null;
				}
				$("#ch-driver-eta").text("");
			}
		} catch (e) {
			console.error("refresh_trip failed", e);
		}
	}

	async _plot_next_stop(trip) {
		// Reuse the existing logistics_api: trip_with_points returns Leaflet-ready
		// points; we just want the next pending stop.
		try {
			const r = await frappe.call({
				method: "ch_erp15.ch_erp15.logistics_api.trip_with_points",
				args: { trip },
			});
			const data = r && r.message;
			if (!data || !data.stops) return;

			// Find the first stop that's not yet completed.
			const pending = data.stops.find(
				(s) => !["Departed", "Completed", "Arrived"].includes(s.status)
			);
			if (!pending || !pending.gps_lat || !pending.gps_lng) {
				$("#ch-driver-eta").text("");
				return;
			}

			const pos = { lat: parseFloat(pending.gps_lat),
						  lng: parseFloat(pending.gps_lng) };

			if (this.stop_marker) this.stop_marker.setMap(null);
			this.stop_marker = new google.maps.Marker({
				position: pos,
				map: this.map,
				title: pending.shipment_to || pending.name,
				label: { text: String(pending.sequence || ""),
						 color: "white", fontWeight: "600" },
			});

			this._draw_route_to(pos);
		} catch (e) {
			console.error("plot_next_stop failed", e);
		}
	}

	_draw_route_to(dest) {
		if (!this.last_pos) return;
		if (this.path) this.path.setMap(null);
		this.path = new google.maps.Polyline({
			path: [this.last_pos, dest],
			geodesic: true,
			strokeColor: "#1e3a8a",
			strokeOpacity: 0.8,
			strokeWeight: 3,
		});
		this.path.setMap(this.map);

		// Haversine distance for ETA pill (assume 30 km/h urban avg).
		const dist_m = google.maps.geometry.spherical.computeDistanceBetween(
			new google.maps.LatLng(this.last_pos),
			new google.maps.LatLng(dest)
		);
		const km = (dist_m / 1000).toFixed(2);
		const mins = Math.round((dist_m / 1000) / 30 * 60);
		$("#ch-driver-eta").text(
			`${__("Next stop")}: ${km} km · ${__("approx")} ${mins} ${__("min")}`
		);

		// Geofence check — flash banner if within configured meters.
		const gf = (this.config && this.config.geofence_arrival_meters) || 100;
		if (dist_m <= gf) {
			this._banner(__("You have reached the location."), "success");
		}
	}

	_start_geolocation() {
		if (!navigator.geolocation) {
			this._banner(__("Geolocation is not supported on this device."), "warning");
			return;
		}
		this.geo_watch_id = navigator.geolocation.watchPosition(
			(p) => this._on_position(p),
			(err) => this._on_geo_error(err),
			{
				enableHighAccuracy: true,
				maximumAge: 5000,
				timeout: 20000,
			}
		);
	}

	_on_position(p) {
		const coords = p.coords;
		const pos = { lat: coords.latitude, lng: coords.longitude };
		this.last_pos = pos;

		// Update the self marker.
		if (!this.self_marker) {
			this.self_marker = new google.maps.Marker({
				position: pos,
				map: this.map,
				title: __("You"),
				icon: {
					path: google.maps.SymbolPath.CIRCLE,
					scale: 8,
					fillColor: "#2563eb",
					fillOpacity: 1,
					strokeColor: "white",
					strokeWeight: 2,
				},
			});
			this.map.setCenter(pos);
		} else {
			this.self_marker.setPosition(pos);
		}

		$("#ch-driver-meta").text(
			`${coords.latitude.toFixed(6)}, ${coords.longitude.toFixed(6)} · `
			+ `±${Math.round(coords.accuracy)}m`
			+ (coords.speed != null
				? ` · ${(coords.speed * 3.6).toFixed(1)} km/h`
				: "")
		);

		// Throttle pings to the recommended cadence.
		const now = Date.now();
		const interval_ms = (this.config.recommended_interval_sec || 15) * 1000;
		if (now - this.last_ping_at >= interval_ms) {
			this.last_ping_at = now;
			this._ping(pos, coords);
		}

		// Refresh the route if we have a destination.
		if (this.stop_marker) {
			this._draw_route_to(this.stop_marker.getPosition().toJSON());
		}
	}

	_on_geo_error(err) {
		console.warn("geolocation error", err);
		this._banner(__("Cannot read GPS: {0}", [err.message]), "warning");
	}

	_ping(pos, coords) {
		frappe.call({
			method: "ch_logistics.api.tracking_api.ping_location",
			type: "POST",
			args: {
				latitude: pos.lat,
				longitude: pos.lng,
				accuracy_m: coords.accuracy,
				speed_kmh: coords.speed != null ? coords.speed * 3.6 : null,
				heading: coords.heading,
				event_type: "Heartbeat",
				source: "Web",
				trip: this.current_trip,
			},
		}).catch((e) => console.warn("ping failed", e));
	}

	_recenter() {
		if (this.last_pos) this.map.panTo(this.last_pos);
	}

	_banner(msg, level) {
		const b = $("#ch-driver-banner");
		b.text(msg).attr("class",
			"ch-logistics-map-banner show " + (level || ""));
		setTimeout(() => b.removeClass("show"), 3500);
	}
};

window.ch_logistics = window.ch_logistics || {};
