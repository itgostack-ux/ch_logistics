"""Maps API — server-mediated Google Maps loader URL.

The Maps API key lives in CH Tracking Settings as an encrypted password and
must never be hard-coded into client JS. The browser calls ``get_maps_url``
to obtain the *one* loader URL that already embeds the key, then injects a
``<script>`` tag with that URL.
"""
from __future__ import annotations

import frappe
from frappe import _

from ch_logistics.logistics.doctype.ch_tracking_settings.ch_tracking_settings import (
	get_google_maps_api_key,
)


_ALLOWED_LIBRARIES = {"places", "geometry", "drawing", "marker"}


@frappe.whitelist()
def get_maps_url(libraries: str = "geometry,marker",
				 callback: str | None = None) -> dict:
	"""Return a signed Google Maps JS API URL for the current user.

	Only logged-in users can request the URL, so the key never goes to
	anonymous browsers.
	"""
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)

	key = get_google_maps_api_key()
	if not key:
		return {"ok": False, "error": "Google Maps API key not configured."}

	# Whitelist the libraries the caller can request.
	libs = ",".join(
		lib for lib in (libraries or "").split(",")
		if lib.strip() in _ALLOWED_LIBRARIES
	) or "geometry,marker"

	url = (
		"https://maps.googleapis.com/maps/api/js?"
		f"key={key}&libraries={libs}&v=weekly&loading=async"
	)
	if callback:
		# Only allow simple JS identifiers for safety.
		safe_cb = "".join(c for c in callback if c.isalnum() or c in "._$")
		if safe_cb:
			url += f"&callback={safe_cb}"
	return {"ok": True, "url": url}
