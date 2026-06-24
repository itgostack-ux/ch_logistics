"""Daily logistics digest — a morning email summary for managers / logistics head.

Recipients are resolved from roles (Delivery Manager + Stock Manager) plus any
extra addresses in CH Logistics Settings, so nothing is hardcoded. Gated by the
``daily_digest_enabled`` setting and wired to the scheduler.
"""
import frappe
from frappe import _
from frappe.utils import nowdate, now_datetime, cint, get_url_to_report

from ch_logistics.api import driver_status as ds


def _enabled():
    # Read via the cached doc so the JSON default (1) applies when the single
    # has never been saved — get_single_value returns 0 for an unset Check.
    v = frappe.get_cached_doc("CH Logistics Settings").get("daily_digest_enabled")
    return v is None or cint(v) == 1


def _recipients():
    valid = set()
    role_users = frappe.get_all(
        "Has Role",
        filters={"role": ["in", ["Delivery Manager", "Stock Manager"]], "parenttype": "User"},
        pluck="parent",
    )
    for u in set(role_users):
        if not u or u in ("Administrator", "Guest"):
            continue
        info = frappe.db.get_value("User", u, ["enabled", "email"], as_dict=True)
        if info and info.enabled and info.email:
            valid.add(info.email)
    extra = frappe.db.get_single_value("CH Logistics Settings", "digest_recipients") or ""
    for e in extra.replace("\n", ",").split(","):
        if e.strip():
            valid.add(e.strip())
    return sorted(valid)


def _metrics():
    today = nowdate()
    now = now_datetime()
    M = "`tabCH Transfer Manifest`"

    def c(where, vals=None):
        return frappe.db.sql(f"SELECT COUNT(*) FROM {M} WHERE {where}", vals or {})[0][0]

    settled = "('Delivered','Partially Received','Received','Closed')"
    dead = "('Cancelled','Rejected','Returned')"
    m = {
        "created_today": c("manifest_date = %(d)s", {"d": today}),
        "delivered_today": c("DATE(delivery_datetime) = %(d)s", {"d": today}),
        "in_transit": c("status = 'In Transit'"),
        "pending_pickup": c("status IN ('Packed','Assigned')"),
        "awaiting_receipt": c("status = 'Delivered'"),
        "overdue": c(f"status NOT IN {settled} AND status NOT IN {dead} "
                     "AND estimated_delivery_date IS NOT NULL AND estimated_delivery_date < %(n)s",
                     {"n": now}),
        "rejected_today": frappe.db.sql(
            "SELECT COUNT(*) FROM `tabCH Manifest Rejection` WHERE DATE(rejected_on) = %(d)s",
            {"d": today})[0][0],
        "open_exceptions": frappe.db.sql(
            "SELECT COUNT(*) FROM `tabCH Logistics Exception` "
            "WHERE IFNULL(resolution_status,'Open') NOT IN ('Resolved','Closed')")[0][0],
        "trips_active": frappe.db.count("CH Logistics Trip", {"status": ["in", ["Assigned", "Started"]]}),
    }
    m["fleet"] = ds.status_counts()
    return m


def _html(m):
    f = m["fleet"]

    def tile(label, value, warn=False):
        color = "#dc2626" if (warn and value) else "#111827"
        return (f'<td style="padding:10px 14px;border:1px solid #e5e7eb;">'
                f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase">{label}</div>'
                f'<div style="font-size:22px;font-weight:700;color:{color}">{value}</div></td>')

    rows = [
        [tile(_("Delivered Today"), m["delivered_today"]),
         tile(_("In Transit"), m["in_transit"]),
         tile(_("Pending Pickup"), m["pending_pickup"]),
         tile(_("Awaiting Receipt"), m["awaiting_receipt"])],
        [tile(_("Overdue"), m["overdue"], warn=True),
         tile(_("Rejected Today"), m["rejected_today"], warn=True),
         tile(_("Open Exceptions"), m["open_exceptions"], warn=True),
         tile(_("Active Trips"), m["trips_active"])],
        [tile(_("Drivers Available"), f.get("Available", 0)),
         tile(_("Assigned"), f.get("Assigned", 0)),
         tile(_("On Trip"), f.get("In Transit", 0)),
         tile(_("Idle / Break"), f.get("Idle", 0) + f.get("Break", 0))],
    ]
    grid = "".join(f"<tr>{''.join(r)}</tr>" for r in rows)
    try:
        link = get_url_to_report("Manifest Delivery and SLA")
        cta = (f'<p style="margin-top:16px"><a href="{link}" '
               f'style="background:#2563eb;color:#fff;padding:8px 16px;border-radius:6px;'
               f'text-decoration:none">{_("Open Delivery & SLA Report")}</a></p>')
    except Exception:
        cta = ""
    return (f'<div style="font-family:Arial,sans-serif;color:#111827">'
            f'<h2 style="margin-bottom:2px">{_("Daily Logistics Digest")}</h2>'
            f'<div style="color:#6b7280;font-size:13px;margin-bottom:12px">{nowdate()}</div>'
            f'<table style="border-collapse:collapse;width:100%">{grid}</table>'
            f'{cta}'
            f'<p style="color:#9ca3af;font-size:11px;margin-top:18px">'
            f'{_("Automated by CH Logistics. Manage in CH Logistics Settings.")}</p></div>')


def send_logistics_daily_digest():
    """Scheduled entry point — compose and email the digest to managers."""
    if not _enabled():
        return {"sent": False, "reason": "disabled"}
    recipients = _recipients()
    if not recipients:
        return {"sent": False, "reason": "no-recipients"}
    m = _metrics()
    frappe.sendmail(
        recipients=recipients,
        subject=_("Daily Logistics Digest — {0}").format(nowdate()),
        message=_html(m),
        now=False,
    )
    return {"sent": True, "recipients": recipients, "metrics": {k: v for k, v in m.items() if k != "fleet"}}


@frappe.whitelist()
def preview_digest():
    """Render the digest HTML without sending — for a quick admin preview."""
    frappe.only_for(["System Manager", "Delivery Manager"])
    return {"recipients": _recipients(), "enabled": _enabled(), "html": _html(_metrics())}
