"""Operational driver-status state machine for the logistics driver app.

Mirrors how established last-mile fleets (Ekart, Delhivery, Amazon Logistics)
model a delivery associate's shift lifecycle:

    Offline → Available → Assigned → In Transit → Available → … → Offline
                       ↘ Break ↗
                       ↘ Idle  ↗     (auto, inactivity-driven)

The status lives on the ``Driver.availability_status`` custom field (installed
by ``install_driver_app_fields``). Every transition is funnelled through
``set_status`` and validated against ``ALLOWED_TRANSITIONS`` so the field can
never drift into an illegal state from the API layer. ``last_active`` is the
heartbeat column the idle sweeper reads.

This module is the single source of truth for driver state — ``logistics_api``,
``driver_api`` and the idle scheduler all delegate here.
"""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

# Canonical states ----------------------------------------------------------
OFFLINE = "Offline"
AVAILABLE = "Available"
ASSIGNED = "Assigned"
IN_TRANSIT = "In Transit"
BREAK = "Break"
IDLE = "Idle"

ALL_STATES = (OFFLINE, AVAILABLE, ASSIGNED, IN_TRANSIT, BREAK, IDLE)

# Legal transitions. A logout (force=True) may always drop a driver to Offline
# regardless of current state — mid-trip logouts happen and must be allowed.
ALLOWED_TRANSITIONS = {
    OFFLINE:    {AVAILABLE},
    AVAILABLE:  {ASSIGNED, BREAK, IDLE, OFFLINE},
    ASSIGNED:   {IN_TRANSIT, AVAILABLE, OFFLINE},
    IN_TRANSIT: {AVAILABLE, ASSIGNED, OFFLINE},
    BREAK:      {AVAILABLE, OFFLINE},
    IDLE:       {AVAILABLE, ASSIGNED, OFFLINE},
}

# Driver is on the clock (counts for utilisation / can receive work).
ONLINE_STATES = {AVAILABLE, ASSIGNED, IN_TRANSIT, BREAK, IDLE}
# Driver is actively working — exempt from the idle auto-sweep.
WORKING_STATES = {ASSIGNED, IN_TRANSIT}

_UNSET = object()


def _has_field(fieldname: str) -> bool:
    try:
        return frappe.get_meta("Driver").has_field(fieldname)
    except Exception:
        return False


def get_status(driver: str) -> str | None:
    if not driver or not _has_field("availability_status"):
        return None
    return frappe.db.get_value("Driver", driver, "availability_status")


def can_transition(current: str | None, target: str) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current or OFFLINE, set())


def set_status(driver, new_status, *, current_trip=_UNSET, force=False,
               touch_activity=True):
    """Transition ``driver`` to ``new_status`` with validation.

    - ``current_trip``: pass a trip name or ``None`` to also update the
      ``current_trip`` field; omit to leave it unchanged.
    - ``force``: skip transition validation (used by logout → Offline).
    - ``touch_activity``: refresh ``last_active`` (defeats the idle sweep).

    Best-effort and silent if the custom fields are not installed yet, so the
    rest of the logistics flow never breaks on a fresh bench.
    """
    if not driver or not _has_field("availability_status"):
        return None
    if new_status not in ALL_STATES:
        frappe.throw(_("Unknown driver status: {0}").format(new_status))

    current = frappe.db.get_value("Driver", driver, "availability_status")
    if not force and not can_transition(current, new_status):
        frappe.throw(
            _("Illegal driver status change: {0} → {1}").format(current or OFFLINE, new_status),
            title=_("Driver Status"),
        )

    updates = {"availability_status": new_status}
    if current_trip is not _UNSET and _has_field("current_trip"):
        updates["current_trip"] = current_trip
    if touch_activity and _has_field("last_active"):
        updates["last_active"] = now_datetime()
    frappe.db.set_value("Driver", driver, updates)
    return new_status


def touch_activity(driver):
    """Register driver activity (heartbeat / any API action). An Idle driver is
    automatically returned to Available (FR-047)."""
    if not driver or not _has_field("availability_status"):
        return None
    current = frappe.db.get_value("Driver", driver, "availability_status")
    if current == IDLE:
        return set_status(driver, AVAILABLE, force=True)
    if _has_field("last_active"):
        frappe.db.set_value("Driver", driver, "last_active", now_datetime(),
                            update_modified=False)
    return current


def _idle_timeout_minutes() -> int:
    val = frappe.db.get_single_value("CH Logistics Settings", "idle_timeout_minutes")
    return cint(val) or 15


def _auto_idle_enabled() -> bool:
    val = frappe.db.get_single_value("CH Logistics Settings", "auto_idle_enabled")
    return val is None or cint(val) == 1


def auto_mark_idle():
    """Scheduled sweep — move Available drivers with no activity beyond the
    configured window to Idle (FR-045, FR-046). Drivers actively on a trip
    (Assigned / In Transit) or on Break are never auto-idled."""
    if not _has_field("availability_status") or not _has_field("last_active"):
        return 0
    if not _auto_idle_enabled():
        return 0
    cutoff = add_to_date(now_datetime(), minutes=-_idle_timeout_minutes())
    stale = frappe.get_all(
        "Driver",
        filters={
            "availability_status": AVAILABLE,
            "last_active": ["<", cutoff],
        },
        pluck="name",
    )
    for driver in stale:
        # touch_activity=False: going Idle must NOT refresh the heartbeat.
        set_status(driver, IDLE, force=True, touch_activity=False)
    if stale:
        frappe.db.commit()
    return len(stale)


def status_counts() -> dict:
    """Driver counts per operational status for the ops dashboard (FR-048)."""
    counts = {s: 0 for s in ALL_STATES}
    if not _has_field("availability_status"):
        return counts
    rows = frappe.db.sql(
        """
        SELECT COALESCE(NULLIF(availability_status, ''), 'Offline') AS state,
               COUNT(name) AS n
        FROM `tabDriver`
        WHERE IFNULL(status, '') != 'Left'
        GROUP BY state
        """,
        as_dict=True,
    )
    for r in rows:
        if r.state in counts:
            counts[r.state] = cint(r.n)
    counts["total"] = sum(counts[s] for s in ALL_STATES)
    return counts
