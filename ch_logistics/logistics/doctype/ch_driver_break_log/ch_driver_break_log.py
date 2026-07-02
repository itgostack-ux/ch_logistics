"""CH Driver Break Log — one row per break, driver-taken during a trip.

Written by the driver-app APIs:
  * ``driver_api.set_break()``  → creates a Draft row (start_ts=now, no end)
  * ``driver_api.end_break()``  → stamps end_ts + duration_min on the newest
                                   open row for that driver

Consumed by the Monthly Driver KPI report (Break Minutes column).

Purpose: comply with driver-hours-of-service style logging without
building a full HR shift-schedule doctype.  Matches SAP TM Driver
Rest Log and Oracle OTM Driver Break Register — one row per pause,
computed duration on close.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


class CHDriverBreakLog(Document):
    def before_save(self):
        # Compute duration_min when both timestamps are present.  We do
        # this in Python (not a MySQL generated column) so the value
        # stays stable across imports and manual edits.
        if self.start_ts and self.end_ts:
            start = get_datetime(self.start_ts)
            end = get_datetime(self.end_ts)
            if end < start:
                frappe.throw(_(
                    "End time cannot be before Start time."
                ), title=_("Invalid Break Window"))
            self.duration_min = round((end - start).total_seconds() / 60.0, 1)
        else:
            self.duration_min = None

    def validate(self):
        # A driver may only have ONE open break log at a time.  The
        # ``end_break`` API always operates on the newest open row;
        # allowing multiple concurrent open rows would let stale
        # untermed breaks silently linger in the KPI report.
        if self.end_ts:
            return
        others = frappe.get_all(
            "CH Driver Break Log",
            filters={
                "driver": self.driver,
                "end_ts": ["is", "not set"],
                "name": ["!=", self.name or ""],
            },
            pluck="name",
            limit=1,
        )
        if others:
            frappe.throw(_(
                "Driver {0} already has an open break log ({1}). "
                "Close it before starting a new one."
            ).format(self.driver, others[0]),
                title=_("Duplicate Open Break"))


def start_break_for_driver(
    driver: str,
    *,
    break_type: str = "Rest",
    reason: str | None = None,
    trip: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    """Create a Draft break log for ``driver`` and return its name.

    Idempotent for concurrent taps of the mobile app: if there is
    already an open row, return its name instead of erroring.
    """
    open_row = frappe.db.get_value(
        "CH Driver Break Log",
        {"driver": driver, "end_ts": ["is", "not set"]},
        "name",
    )
    if open_row:
        return open_row

    doc = frappe.new_doc("CH Driver Break Log")
    doc.driver = driver
    doc.break_type = break_type or "Rest"
    doc.reason = reason
    doc.trip = trip
    doc.start_ts = now_datetime()
    if latitude is not None:
        doc.start_latitude = latitude
    if longitude is not None:
        doc.start_longitude = longitude
    doc.insert(ignore_permissions=True)
    return doc.name


def end_break_for_driver(
    driver: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str | None:
    """Close the newest open break for ``driver``.

    Returns the closed row's name, or ``None`` if no open break was
    found (caller should treat this as a no-op — the driver may have
    already closed manually, or set_break may never have run).
    """
    open_row = frappe.db.get_value(
        "CH Driver Break Log",
        {"driver": driver, "end_ts": ["is", "not set"]},
        "name",
    )
    if not open_row:
        return None

    doc = frappe.get_doc("CH Driver Break Log", open_row)
    doc.end_ts = now_datetime()
    if latitude is not None:
        doc.end_latitude = latitude
    if longitude is not None:
        doc.end_longitude = longitude
    doc.save(ignore_permissions=True)
    return doc.name
