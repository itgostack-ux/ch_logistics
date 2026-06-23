"""Public track-and-trace page for an inter-store transfer / delivery.

URL: /track?id=<tracking_token>
No login required — the opaque token is the access control.
"""
import frappe
from frappe import _

no_cache = 1


def get_context(context):
    token = (frappe.form_dict.get("id") or "").strip()
    context.no_cache = 1
    context.title = _("Track Delivery")
    context.found = False

    if not token:
        context.error = _("No tracking id provided.")
        return context

    try:
        from ch_logistics.api.customer_tracking import get_public_tracking
        data = get_public_tracking(token)
    except Exception:
        context.error = _("This tracking link is invalid or has expired.")
        return context

    context.found = True
    context.t = data
    context.maps_key = frappe.db.get_single_value("CH Tracking Settings", "google_maps_api_key") or ""
    return context
