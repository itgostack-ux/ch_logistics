"""Retired driver portal page — redirects to the Delivery App.

This page used to render its own stop table with per-stop Arrive / Complete
buttons. It was a second, weaker driver UI alongside ``/app/delivery-app`` and
had to go for two reasons:

1. Its buttons could not work standalone. ``stop_arrive`` / ``stop_complete``
   both require ``trip.status == "Started"``, and this page offered no way to
   accept and start a trip — so on an Assigned trip both actions just errored.

2. More seriously, on an already-Started trip it advanced stops to
   Arrived / Completed WITHOUT touching the manifests. Manifests stayed
   ``Assigned`` with no pickup photo, no QR scan and no receiver OTP, while the
   trip's stops read Completed — a chain-of-custody proof bypass.

The Delivery App is the single driver surface: it owns the three-stage
manifest contract (Assigned → In Transit → Delivered), pickup/delivery proof
capture, manifest rejection and the trip stop graph.

The route is kept alive as a redirect rather than deleted because drivers have
the URL bookmarked and it appears in the user guide.
"""
from __future__ import annotations

import frappe

no_cache = 1

#: Canonical driver surface. Desk page ``delivery-app`` (see
#: ``logistics/page/delivery_app``).
DELIVERY_APP_ROUTE = "/app/delivery-app"


def get_context(context):
	user = frappe.session.user
	if user in ("Guest", ""):
		# Land on the Delivery App after login, not back on this stub.
		frappe.local.flags.redirect_location = f"/login?redirect-to={DELIVERY_APP_ROUTE}"
		raise frappe.Redirect

	frappe.local.flags.redirect_location = DELIVERY_APP_ROUTE
	raise frappe.Redirect
