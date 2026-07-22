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


def _digest_roles():
    from ch_logistics.roles import get_roles_for
    return get_roles_for("digest_recipients")


def _recipients(company=None):
    """Logistics managers — scoped to `company` when given (fail-closed via
    ch_erp15's notification router); explicit settings recipients always kept."""
    valid = set()
    if company:
        try:
            from ch_erp15.ch_erp15.notification_router import scoped_role_emails

            valid.update(scoped_role_emails(sorted(_digest_roles()), company=company))
        except ImportError:
            company = None  # router unavailable — fall back to role-wide below
    if not company:
        role_users = frappe.get_all(
            "Has Role",
            filters={"role": ["in", sorted(_digest_roles())], "parenttype": "User"},
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


def _metrics(company=None):
    """Logistics metrics, scoped to ``company`` when given. Manifests & trips
    carry company directly; rejections/exceptions are scoped through their
    parent manifest/trip."""
    today = nowdate()
    now = now_datetime()
    base = {"d": today, "n": now}
    co = ""
    if company:
        co = " AND m.company = %(co)s"
        base["co"] = company
    M = "`tabCH Transfer Manifest` m"

    def c(where, extra=None):
        v = dict(base, **(extra or {}))
        return frappe.db.sql(f"SELECT COUNT(*) FROM {M} WHERE {where}{co}", v)[0][0]

    settled = "('Delivered','Partially Received','Received','Closed')"
    dead = "('Cancelled','Rejected','Returned')"
    rej_co = (" JOIN `tabCH Transfer Manifest` m ON m.name = r.manifest" + co) if company else ""
    exc_co = (" JOIN `tabCH Logistics Trip` m ON m.name = e.parent" + co) if company else ""
    trip_co = {"status": ["in", ["Assigned", "Started"]]}
    if company:
        trip_co["company"] = company
    m = {
        "created_today": c("m.manifest_date = %(d)s"),
        "delivered_today": c("DATE(m.delivery_datetime) = %(d)s"),
        "in_transit": c("m.status = 'In Transit'"),
        "pending_pickup": c("m.status IN ('Packed','Assigned')"),
        "awaiting_receipt": c("m.status = 'Delivered'"),
        "overdue": c(f"m.status NOT IN {settled} AND m.status NOT IN {dead} "
                     "AND m.estimated_delivery_date IS NOT NULL AND m.estimated_delivery_date < %(n)s"),
        "rejected_today": frappe.db.sql(
            f"SELECT COUNT(*) FROM `tabCH Manifest Rejection` r{rej_co} "
            "WHERE DATE(r.rejected_on) = %(d)s", base)[0][0],
        "open_exceptions": frappe.db.sql(
            f"SELECT COUNT(*) FROM `tabCH Logistics Exception` e{exc_co} "
            "WHERE IFNULL(e.resolution_status,'Open') NOT IN ('Resolved','Closed')", base)[0][0],
        "trips_active": frappe.db.count("CH Logistics Trip", trip_co),
    }
    m["fleet"] = ds.status_counts()  # drivers are a shared fleet resource
    return m


def _html(m, company=None):
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
    subtitle = (company or _("All Companies")) + " · " + nowdate()
    return (f'<div style="font-family:Arial,sans-serif;color:#111827">'
            f'<h2 style="margin-bottom:2px">{_("Daily Logistics Digest")}</h2>'
            f'<div style="color:#6b7280;font-size:13px;margin-bottom:12px">{subtitle}</div>'
            f'<table style="border-collapse:collapse;width:100%">{grid}</table>'
            f'{cta}'
            f'<p style="color:#9ca3af;font-size:11px;margin-top:18px">'
            f'{_("Automated by CH Logistics. Manage in CH Logistics Settings.")}</p></div>')


def _active_companies():
    """Companies that actually have logistics activity (manifests)."""
    rows = frappe.db.sql(
        "SELECT DISTINCT company FROM `tabCH Transfer Manifest` WHERE IFNULL(company,'') != ''",
        as_dict=True)
    return [r.company for r in rows] or [c.name for c in frappe.get_all("Company", fields=["name"])]


def send_logistics_daily_digest():
    """Scheduled entry point — one company-scoped digest per company.

    Each company's managers get a digest covering only that company's logistics,
    honouring the single active-company model end-to-end."""
    if not _enabled():
        return {"sent": False, "reason": "disabled"}

    sent = []
    all_recipients = {}
    for company in _active_companies():
        recipients = _recipients(company)
        if not recipients:
            continue
        all_recipients[company] = recipients
        m = _metrics(company)
        frappe.sendmail(
            recipients=recipients,
            subject=_("Daily Logistics Digest — {0} — {1}").format(company, nowdate()),
            message=_html(m, company=company),
            now=False,
        )
        sent.append(company)
    if not sent:
        return {"sent": False, "reason": "no-recipients"}
    return {"sent": True, "companies": sent, "recipients": all_recipients}


@frappe.whitelist()
def preview_digest(company=None):
    """Render the digest HTML without sending — for a quick admin preview."""
    from ch_logistics import roles as role_registry
    role_registry.require("digest_preview", "preview the logistics digest")
    company = company or frappe.defaults.get_user_default("Company")
    return {"recipients": _recipients(), "enabled": _enabled(),
            "company": company, "html": _html(_metrics(company), company=company)}
