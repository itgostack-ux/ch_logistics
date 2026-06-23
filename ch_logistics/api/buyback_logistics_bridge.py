"""Buyback Logistics Bridge — connects Buyback Pickup Material Requests to
the Reverse Logistics manifest/trip layer (Phase D).

Trigger
-------
Hook: Stock Entry on_submit.

Behaviour
---------
When a submitted Stock Entry carries lines linked to a buyback pickup
Material Request (identified by ``custom_buyback_order`` set on the MR),
this module ensures:

1. A Draft *Reverse* CH Transfer Manifest exists for today / source_store
   / source_warehouse — or creates one if none.
2. The newly submitted Stock Entry is appended to that manifest's
   ``transfers`` table (idempotent on re-submit attempts).
3. If a Draft / Assigned reverse CH Logistics Trip exists for today that
   already includes this source warehouse as a stop (or has none yet),
   the manifest is auto-attached and ``stop_sequence`` is filled in.

The bridge never moves stock and never auto-submits a manifest — ops
still review and submit / dispatch via the existing flows.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, today


def on_stock_entry_submit(doc, method=None):
    """doc_events hook: CH Erp15 → Stock Entry → on_submit."""
    try:
        _process(doc)
    except Exception:
        # Bridge must never block stock entry submission.
        frappe.log_error(
            title="buyback_logistics_bridge",
            message=frappe.get_traceback(),
        )


# ---------------------------------------------------------------------------
def _process(se):
    pickup_mr_set = _buyback_pickup_mrs(se)
    if not pickup_mr_set:
        return

    # Only consider rows tied to a buyback pickup MR — these carry the real
    # source/destination for the reverse leg. Stock Entry's top-level
    # from_warehouse/to_warehouse are blank when rows differ.
    pickup_rows = [r for r in (se.items or [])
                   if r.material_request and r.material_request in pickup_mr_set]
    if not pickup_rows:
        return
    source_wh = pickup_rows[0].s_warehouse or se.from_warehouse
    dest_wh = pickup_rows[0].t_warehouse or se.to_warehouse
    if not source_wh or not dest_wh:
        return

    source_store = _resolve_store_from_warehouse(source_wh)
    company = se.company
    if not company:
        return

    manifest = _ensure_reverse_manifest(
        company=company,
        source_store=source_store,
        source_warehouse=source_wh,
        destination_warehouse=dest_wh,
        first_stock_entry=se.name,
    )
    _attach_se_to_manifest(manifest, se.name)

    trip = _find_open_reverse_trip(
        trip_date=today(),
        company=company,
        source_warehouse=source_wh,
    )
    if trip:
        _attach_manifest_to_trip(trip, manifest)


# ---------------------------------------------------------------------------
def _buyback_pickup_mrs(se):
    """Return a set of MR names referenced by this SE that are buyback
    pickup MRs (Material Request.custom_buyback_order set)."""
    mr_names = {row.material_request for row in (se.items or []) if row.material_request}
    if not mr_names:
        return set()
    rows = frappe.get_all(
        "Material Request",
        filters={"name": ["in", list(mr_names)], "custom_buyback_order": ["is", "set"]},
        pluck="name",
    )
    return set(rows)


def _resolve_store_from_warehouse(warehouse):
    """Best-effort CH Store lookup from a Warehouse. Returns None if no
    direct link doctype exists or no match found."""
    if not warehouse:
        return None
    try:
        meta = frappe.get_meta("CH Store")
    except frappe.DoesNotExistError:
        return None
    for fname in ("warehouse", "default_warehouse"):
        if meta.has_field(fname):
            store = frappe.db.get_value("CH Store", {fname: warehouse}, "name")
            if store:
                return store
    return None


def _ensure_reverse_manifest(company, source_store, source_warehouse,
                             destination_warehouse, first_stock_entry=None):
    """Find today's open Draft reverse manifest for this source, or create one.
    If creating new, attach first_stock_entry as the initial row so the
    mandatory ``transfers`` table is non-empty."""
    filters = {
        "company": company,
        "source_warehouse": source_warehouse,
        "destination_warehouse": destination_warehouse,
        "manifest_date": today(),
        "docstatus": 0,
    }
    meta = frappe.get_meta("CH Transfer Manifest")
    if meta.has_field("direction"):
        filters["direction"] = "Reverse"
    if source_store and meta.has_field("source_store"):
        filters["source_store"] = source_store

    existing = frappe.get_all("CH Transfer Manifest", filters=filters,
                              fields=["name"], limit=1, order_by="creation desc")
    if existing:
        return existing[0].name

    doc = frappe.new_doc("CH Transfer Manifest")
    doc.manifest_date = today()
    doc.company = company
    doc.source_warehouse = source_warehouse
    doc.destination_warehouse = destination_warehouse
    if meta.has_field("direction"):
        doc.direction = "Reverse"
    if source_store and meta.has_field("source_store"):
        doc.source_store = source_store
    if first_stock_entry:
        doc.append("transfers", {"stock_entry": first_stock_entry})
    doc.flags.ignore_permissions = True
    doc.insert(ignore_mandatory=not bool(first_stock_entry))
    return doc.name


def _attach_se_to_manifest(manifest_name, stock_entry):
    """Idempotently append a SE row to the manifest's transfers table."""
    existing = frappe.db.exists(
        "CH Transfer Manifest Item",
        {"parent": manifest_name, "stock_entry": stock_entry,
         "parenttype": "CH Transfer Manifest"},
    )
    if existing:
        return

    doc = frappe.get_doc("CH Transfer Manifest", manifest_name)
    if doc.docstatus != 0:
        # Manifest already submitted/cancelled — don't mutate.
        return
    doc.append("transfers", {"stock_entry": stock_entry})
    doc.flags.ignore_permissions = True
    doc.save()


# ---------------------------------------------------------------------------
def _find_open_reverse_trip(trip_date, company, source_warehouse):
    """Return a Draft/Assigned reverse trip for today + company whose route
    includes this source warehouse as a stop, if any."""
    if not frappe.db.exists("DocType", "CH Logistics Trip"):
        return None

    trips = frappe.get_all(
        "CH Logistics Trip",
        filters={
            "trip_date": trip_date,
            "company": company,
            "direction": "Reverse",
            "status": ["in", ["Draft", "Assigned"]],
        },
        pluck="name",
        limit=20,
    )
    for t in trips:
        # Check if this warehouse is one of the trip's stops
        if frappe.db.exists(
            "CH Logistics Trip Stop",
            {"parent": t, "warehouse": source_warehouse,
             "parenttype": "CH Logistics Trip"},
        ):
            return t
    return None


def _attach_manifest_to_trip(trip, manifest):
    """Attach manifest to trip if not already, and set stop_sequence to
    match the stop where source_warehouse matches."""
    current = frappe.db.get_value("CH Transfer Manifest", manifest, "trip")
    if current == trip:
        return
    if current and current != trip:
        # Manifest is on another trip already — leave it alone.
        return

    # Determine stop_sequence
    source_wh = frappe.db.get_value("CH Transfer Manifest", manifest, "source_warehouse")
    seq = frappe.db.get_value(
        "CH Logistics Trip Stop",
        {"parent": trip, "warehouse": source_wh, "parenttype": "CH Logistics Trip"},
        "sequence",
    )

    updates = {"trip": trip}
    meta = frappe.get_meta("CH Transfer Manifest")
    if seq and meta.has_field("stop_sequence"):
        updates["stop_sequence"] = cint(seq)
    frappe.db.set_value("CH Transfer Manifest", manifest, updates)

    # Refresh trip totals
    try:
        trip_doc = frappe.get_doc("CH Logistics Trip", trip)
        trip_doc.flags.ignore_permissions = True
        trip_doc.save()
    except Exception:
        pass
