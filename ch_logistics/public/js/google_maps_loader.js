/**
 * Google Maps loader — fetches a signed URL from the server (key never
 * appears in client JS) and injects the <script> tag exactly once.
 *
 * Usage from any page:
 *   ch_logistics.maps.load(["geometry","marker"]).then(() => {
 *       const map = new google.maps.Map(el, { ... });
 *   });
 */
(function() {
	window.ch_logistics = window.ch_logistics || {};
	window.ch_logistics.maps = window.ch_logistics.maps || {};

	let _loading_promise = null;

	ch_logistics.maps.load = function(libraries) {
		if (window.google && window.google.maps) {
			return Promise.resolve(window.google.maps);
		}
		if (_loading_promise) return _loading_promise;

		const libs = (libraries && libraries.length)
			? libraries.join(",")
			: "geometry,marker";

		_loading_promise = new Promise((resolve, reject) => {
			frappe.call({
				method: "ch_logistics.api.maps_api.get_maps_url",
				args: { libraries: libs },
			}).then((r) => {
				if (!r || !r.message || !r.message.ok) {
					reject(new Error(
						(r && r.message && r.message.error)
						|| "Google Maps API key not configured."
					));
					return;
				}
				const s = document.createElement("script");
				s.src = r.message.url;
				s.async = true;
				s.defer = true;
				s.onload = () => {
					if (window.google && window.google.maps) {
						resolve(window.google.maps);
					} else {
						reject(new Error("Google Maps loaded but global missing."));
					}
				};
				s.onerror = (e) => reject(new Error("Maps script load failed."));
				document.head.appendChild(s);
			}).catch(reject);
		});

		return _loading_promise;
	};

	/**
	 * Convenience — fetch tracking config (cadence, defaults, geofence).
	 */
	ch_logistics.maps.get_config = function() {
		return frappe.call({
			method: "ch_logistics.api.tracking_api.get_config",
		}).then((r) => (r && r.message) || {});
	};
})();
