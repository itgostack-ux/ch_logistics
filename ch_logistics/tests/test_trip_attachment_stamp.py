"""Attaching a manifest to a trip records WHEN it happened.

The trip link was written straight through `frappe.db.set_value` from four
places. That bypasses the version trail, so nothing recorded the moment a
consignment went onto a trip — the pipeline report had to infer it from the
trip's own creation, which on live data was days out (a manifest raised 30 Jul
was inferred onto a trip created 8 Aug).

Every attach and detach now goes through `trip_lock.set_manifest_trip`, which
stamps `trip_attached_at`. This proves it, and proves a detach clears it.

Invocation:
    bench --site <site> console
    >>> from ch_logistics.tests import test_trip_attachment_stamp as t; t.run_all()
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

PASSED: list[str] = []
FAILED: list[str] = []


class TestFailure(AssertionError):
    pass


def _check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}")


def run_all():
    from ch_logistics.api.trip_lock import set_manifest_trip

    PASSED.clear()
    FAILED.clear()

    _check("the field exists on the manifest",
           frappe.db.has_column("CH Transfer Manifest", "trip_attached_at"))
    meta_field = frappe.get_meta("CH Transfer Manifest").get_field("trip_attached_at")
    _check("it is read-only and submit-editable",
           meta_field and meta_field.read_only and meta_field.allow_on_submit,
           f"read_only={getattr(meta_field, 'read_only', None)} "
           f"allow_on_submit={getattr(meta_field, 'allow_on_submit', None)}")

    manifest = frappe.db.get_value("CH Transfer Manifest", {"docstatus": ("<", 2)}, "name")
    trip = frappe.db.get_value("CH Logistics Trip", {"docstatus": ("<", 2)}, "name")
    if not manifest or not trip:
        raise TestFailure("need at least one manifest and one trip to test against")
    print(f"fixture: manifest={manifest} trip={trip}\n")

    try:
        before = now_datetime()

        # ── Attach ────────────────────────────────────────────────────────
        set_manifest_trip(manifest, trip)
        stamped_trip, stamped_at = frappe.db.get_value(
            "CH Transfer Manifest", manifest, ["trip", "trip_attached_at"])
        _check("attach sets the trip", stamped_trip == trip, f"trip={stamped_trip}")
        _check("attach stamps the moment", bool(stamped_at), f"stamp={stamped_at}")
        _check("the stamp is the attachment time, not the trip's creation",
               stamped_at and stamped_at >= before,
               f"stamp={stamped_at} vs attach started {before}")

        trip_created = frappe.db.get_value("CH Logistics Trip", trip, "creation")
        print(f"        captured {stamped_at} — the trip itself was created {trip_created}")

        # ── Extras ride the same write ────────────────────────────────────
        if frappe.get_meta("CH Transfer Manifest").has_field("stop_sequence"):
            set_manifest_trip(manifest, trip, extra={"stop_sequence": 7})
            seq = frappe.db.get_value("CH Transfer Manifest", manifest, "stop_sequence")
            _check("attach-time extras land in the same write", int(seq or 0) == 7,
                   f"stop_sequence={seq}")

        # ── Detach ────────────────────────────────────────────────────────
        set_manifest_trip(manifest, None)
        cleared_trip, cleared_at = frappe.db.get_value(
            "CH Transfer Manifest", manifest, ["trip", "trip_attached_at"])
        _check("detach clears the trip", not cleared_trip, f"trip={cleared_trip}")
        _check("detach clears the stamp too", not cleared_at,
               f"stamp={cleared_at} — a manifest on no trip must not look attached")

        # ── Nobody can write the link without the stamp any more ──────────
        import subprocess
        raw = subprocess.run(
            ["grep", "-rn", 'set_value("CH Transfer Manifest".*"trip"',
             "/home/palla/erpnext-bench/apps/ch_logistics"],
            capture_output=True, text=True,
        ).stdout
        offenders = [
            line for line in raw.splitlines()
            if ".pyc" not in line and "/tests/" not in line
        ]
        _check("no raw trip write bypasses the helper", not offenders,
               "; ".join(offenders[:3]))

    finally:
        frappe.db.rollback()
        print("\n(rolled back — no documents left behind)")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    if FAILED:
        raise TestFailure(f"{len(FAILED)} check(s) failed")
    return {"passed": len(PASSED), "failed": 0}
