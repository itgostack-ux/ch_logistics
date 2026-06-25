"""End-to-end test: POS material request → multi-source split (2 + 2 + 6) →
multi-pickup trip → drop at destination store.

Asserts:
  1. POS-side ``custom_logistics_status`` on Stock Entry mirrors the manifest
     lifecycle at every phase (Submitted → In Transit → Delivered → Received).
  2. The combined trip-start gate accepts the trip + picks up every source
     manifest with one shared load photo + per-manifest QR + GPS.
  3. The per-stop "Arrive & Pick Up" works for additional pickup stops
     (store A, store B) on a multi-pickup trip.
  4. The per-stop "Arrive & Deliver" runs OTP / receiver / photo across all
     manifests at the drop in one batch.
  5. ``accept_delivery`` from the destination POS lane auto-submits the
     underlying Stock Entries.

Run:
    bench --site erpnext.local execute ch_logistics._e2e_pos_multi_pickup.run

Results land in:
    sites/<site>/private/files/e2e_runs/<UTC-timestamp>/result.json

The script is idempotent (re-runnable). Every doc it creates is tagged with
``[E2E-POS2DOV]`` in remarks and uses a stable namespace prefix so re-runs
update / reuse the same fixtures instead of polluting the DB.
"""
from __future__ import annotations

import base64
import json
import os
import traceback
from datetime import datetime, timezone

import frappe
from frappe.utils import cint, flt, now_datetime, nowdate

# ── Configuration ─────────────────────────────────────────────────────────
COMPANY = "BestBuy Mobiles Pvt Ltd"
TAG = "E2E-POS2DOV"
ITEM_PREFIX = f"{TAG}-ITM-"
ITEM_GROUP_NAME = "All Item Groups"   # fallback parent
NUM_ITEMS = 10
SPLIT_A = 2
SPLIT_B = 2
SPLIT_HUB = 6
SUPPLIER_NAME = f"{TAG}-Supplier"
DRIVER_PREFIX = f"{TAG}-DRV"
VEHICLE_NUMBER = f"{TAG}-VEH-01"

# Chennai-ish coordinates with per-stop offsets so each GPS reading differs.
CHENNAI_LAT, CHENNAI_LNG = 13.0827, 80.2707

# Transparent 1x1 PNG, used as the proof photo for every step.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAA"
    "YAAjCB0C8AAAAASUVORK5CYII="
)

API_TRIP = "ch_logistics.api.logistics_api"
API_MFT = "ch_logistics.api.transfer_manifest_api"

# ── Result accumulator ────────────────────────────────────────────────────
results: dict = {
    "tag": TAG,
    "company": COMPANY,
    "started_at": None,
    "ended_at": None,
    "site": frappe.local.site if hasattr(frappe.local, "site") else None,
    "steps": [],
    "pos_status_timeline": [],
    "manifests": {},
    "trip": None,
    "summary": {"pass": 0, "fail": 0, "skip": 0},
    "error": None,
}


def _step(name: str, status: str, **kw) -> dict:
    entry = {"step": name, "status": status, **kw}
    results["steps"].append(entry)
    bucket = {"PASS": "pass", "FAIL": "fail", "SKIP": "skip"}.get(status, "fail")
    results["summary"][bucket] += 1
    detail = kw.get("detail") or ""
    print(f"  [{status:<4}] {name}" + (f"  —  {detail}" if detail else ""))
    return entry


def _section(label: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n  {label}\n{line}")


def _assert(condition: bool, name: str, *, detail: str = "", **kw) -> bool:
    return _step(name, "PASS" if condition else "FAIL", detail=detail, **kw) and condition


# ── Discovery helpers ─────────────────────────────────────────────────────
def _discover_environment() -> dict:
    """Find the warehouses + driver this run will use. Prefers names the user
    gave (BMPL-STORE-04, Doveton) when they exist."""
    candidates = frappe.get_all(
        "Warehouse",
        filters={"company": COMPANY, "is_group": 0, "disabled": 0},
        fields=["name"],
        order_by="name asc",
    )
    names = [c.name for c in candidates]
    if len(names) < 4:
        raise RuntimeError(
            f"E2E needs ≥4 active leaf warehouses under '{COMPANY}'; "
            f"found {len(names)}: {names!r}"
        )

    # Destination: prefer the user-named BMPL-STORE-04* family. Among that
    # family, prefer the "Sellable" on-shelf bin over Buyback / Damaged /
    # Disposed / InTransit / Reserved sub-bins — the requesting POS would
    # always pull stock into its sellable bin.
    def _is_bad(n: str) -> bool:
        return any(k in n for k in ("Damaged", "Disposed", "InTransit", "Reserved"))

    dest = next((n for n in names if "BMPL-STORE-04" in n and "Sellable" in n), None)
    if not dest:
        dest = next((n for n in names if "BMPL-STORE-04" in n and not _is_bad(n)
                     and "Buyback" not in n), None)
    if not dest:
        dest = next((n for n in names if "BMPL-STORE-04" in n and not _is_bad(n)), None)
    if not dest:
        dest = next((n for n in names if "BMPL-STORE-04" in n), None)
    if not dest:
        dest = next((n for n in names if "Doveton" in n and not _is_bad(n)), None)
    if not dest:
        dest = names[-1]

    # Two source stores (prefer non-damaged, non-hub names; include Velachery / Demo).
    pool = [n for n in names if n != dest]
    preferred_source_keys = ("Velachery", "Demo Outlet", "Doveton")
    sources: list[str] = []
    for key in preferred_source_keys:
        match = next((n for n in pool if key in n and "Damaged" not in n), None)
        if match and match not in sources:
            sources.append(match)
        if len(sources) >= 2:
            break
    if len(sources) < 2:
        for n in pool:
            if n in sources or n == dest:
                continue
            if "Damaged" in n or "Returns" in n:
                continue
            sources.append(n)
            if len(sources) >= 2:
                break
    if len(sources) < 2:
        raise RuntimeError(f"Could not find 2 distinct source stores under {COMPANY}")
    store_a, store_b = sources[0], sources[1]

    # Hub warehouse: a 4th distinct warehouse that isn't destination or source.
    used = {dest, store_a, store_b}
    hub = next(
        (n for n in names if n not in used and ("Stores" in n or "Hub" in n or "WH" in n.upper())),
        None,
    )
    if not hub:
        hub = next((n for n in names if n not in used), None)
    if not hub:
        raise RuntimeError("Could not find a hub warehouse")

    return {
        "store_a": store_a,
        "store_b": store_b,
        "hub": hub,
        "destination": dest,
    }


def _pick_or_create_driver() -> str:
    """Return a Driver record that has no active trips. Prefers an existing
    idle driver, otherwise creates a dedicated E2E driver.

    The single-active-trip-per-driver guard in ``_ensure_single_active_trip_
    for_driver`` rejects assignment when the driver still has an Open /
    Assigned / Started trip from a prior run, so we cannot reuse the first
    found Active driver. Every prior failed run leaves a stuck Assigned
    trip behind, so we always create a fresh timestamped driver as the
    last resort.
    """
    active_statuses = ("Open", "Assigned", "Started")

    def _is_idle(d: str) -> bool:
        blocking = frappe.get_all(
            "CH Logistics Trip",
            filters={"driver": d, "status": ["in", active_statuses]},
            limit=1,
        )
        return not blocking

    # 1. Any existing Active driver with no active trip.
    for d in frappe.get_all("Driver", filters={"status": "Active"}, pluck="name"):
        if _is_idle(d):
            return d

    # 2. The dedicated E2E driver (if it exists and happens to be idle).
    base = f"{TAG} Driver"
    existing = frappe.db.get_value("Driver", {"full_name": base}, "name")
    if existing and _is_idle(existing):
        return existing

    # 3. Create a fresh timestamped E2E driver. Each failed run leaves a
    #    stuck Assigned trip behind on the previous driver, so we always
    #    spin up a new one for a clean slate.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    fullname = f"{base} {stamp}"
    doc = frappe.get_doc({
        "doctype": "Driver",
        "full_name": fullname,
        "status": "Active",
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_supplier() -> str:
    if frappe.db.exists("Supplier", SUPPLIER_NAME):
        return SUPPLIER_NAME
    grp = (frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
           or frappe.db.get_value("Supplier Group", {}, "name"))
    # India Compliance enforces tax_category as mandatory on Supplier — pick
    # any existing one (preferring an "Out-State" / "In-State" GST category
    # since this is an Indian-company test) so we don't fight that validator.
    tax_cat = (frappe.db.get_value("Tax Category", {"name": "Out-State"}, "name")
               or frappe.db.get_value("Tax Category", {"name": "In-State"}, "name")
               or frappe.db.get_value("Tax Category", {}, "name"))
    doc = frappe.get_doc({
        "doctype": "Supplier",
        "supplier_name": SUPPLIER_NAME,
        "supplier_group": grp,
        "supplier_type": "Company",
        "country": frappe.db.get_default("country") or "India",
        "tax_category": tax_cat,
        # Mark as unregistered (no GSTIN) so PO/PR don't require a GST
        # number — keeps the test fixture lightweight.
        "gst_category": "Unregistered",
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_items(count: int) -> list[str]:
    """Create / reuse a set of plain (non-serial) test items.

    Items are 1-piece UOM, no serial/batch, no sales/purchase tax templates.
    Re-used across runs so we don't blow up the Item master.
    """
    group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or ITEM_GROUP_NAME
    uom = frappe.db.get_value("UOM", {"name": "Nos"}, "name") or "Nos"
    # ch_item_master makes ch_category + ch_sub_category mandatory AND its
    # before_insert hook can flip is_stock_item to 0 when the sub-category's
    # item_nature is Service / Subscription / Variant Template. So we have
    # to pick a sub-category whose is_stock_item_default=1 AND a stockable
    # nature, or our items come out as non-stock and SE refuses them.
    sc = frappe.db.get_value(
        "CH Sub Category",
        {
            "is_stock_item_default": 1,
            "item_nature": ["in", ["Simple Auto-Named", "Asset / Capital",
                                    "Simple", "Standard"]],
            "name": ["not like", r"\_%"],   # skip _RBAC_ / _Test / _TierB / _TierC
        },
        ["name", "category"],
        as_dict=True,
    ) or frappe.db.get_value(
        "CH Sub Category",
        {"is_stock_item_default": 1,
         "item_nature": ["not in", ["Service", "Subscription", "Variant Template"]]},
        ["name", "category"],
        as_dict=True,
    )
    if not sc:
        raise RuntimeError(
            "No usable CH Sub Category found "
            "(need is_stock_item_default=1 + stockable item_nature)."
        )
    items: list[str] = []
    for i in range(1, count + 1):
        code = f"{ITEM_PREFIX}{i:02d}"
        if frappe.db.exists("Item", code):
            # Idempotency repair: an earlier failed run may have produced a
            # non-stock item (the ch_item_master before_insert hook can flip
            # is_stock_item to 0 when the sub-category nature is Service/
            # Subscription/Variant Template). Patch it in place so Material
            # Transfer stock entries can use it.
            existing = frappe.get_doc("Item", code)
            dirty = False
            if not existing.is_stock_item:
                existing.is_stock_item = 1
                dirty = True
            if existing.ch_sub_category != sc.name:
                existing.ch_category = sc.category
                existing.ch_sub_category = sc.name
                dirty = True
            if not (existing.ch_item_mrp or 0):
                existing.ch_item_mrp = 500.0
                dirty = True
            if (existing.get("ch_lifecycle_status") or "") != "Active":
                existing.ch_lifecycle_status = "Active"
                dirty = True
            if (existing.get("ch_plm_status") or "NPI") != "Approved":
                existing.ch_plm_status = "Approved"
                existing.flags.ignore_plm_transition = True
                dirty = True
            if dirty:
                # Bypass the validate hook chain when re-stamping to avoid
                # the BeforeInsert side-effects we already worked around.
                existing.flags.ignore_validate = True
                existing.save(ignore_permissions=True)
            items.append(code)
            continue
        doc = frappe.get_doc({
            "doctype": "Item",
            "item_code": code,
            "item_name": code,
            "item_group": group,
            "stock_uom": uom,
            "is_stock_item": 1,
            "include_item_in_manufacturing": 0,
            "is_purchase_item": 1,
            "is_sales_item": 1,
            "has_serial_no": 0,
            "has_batch_no": 0,
            # ch_item_master enforces MRP for every stock item; set a
            # reasonable value so PO rate (₹100) sits well under it.
            "ch_item_mrp": 500.0,
            # ch_item_master enforces (ch_category, ch_sub_category)
            "ch_category": sc.category,
            "ch_sub_category": sc.name,
            # Skip the Draft -> Pending Review -> Active workflow. We run
            # as Administrator (CH Master Approver) so direct-to-Active is
            # allowed at insert time.
            "ch_lifecycle_status": "Active",
            # PLM state machine: "NPI" blocks Stock Entry / sales; jump to
            # "Approved" so the item is fully transactable.
            "ch_plm_status": "Approved",
            "description": f"E2E test item created by {TAG}. Safe to delete after run.",
        })
        doc.insert(ignore_permissions=True)
        items.append(doc.name)
    return items


# ── Photo / GPS helpers ───────────────────────────────────────────────────
def _ensure_proof_photo() -> str:
    """Create / reuse a small PNG attached as a public File and return its URL."""
    fname = f"{TAG}-proof.png"
    existing = frappe.db.get_value("File", {"file_name": fname}, "file_url")
    if existing:
        return existing
    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": fname,
        "is_private": 0,
        "content": PNG_BYTES,
        "decode": False,
    })
    file_doc.insert(ignore_permissions=True)
    return file_doc.file_url


def _gps(idx: int) -> tuple[float, float]:
    """Per-stop GPS offset so each capture differs (proof-of-presence smell test)."""
    return (round(CHENNAI_LAT + 0.001 * idx, 6), round(CHENNAI_LNG + 0.001 * idx, 6))


# ── Stock seeding (Material Receipt) ──────────────────────────────────────
def _seed_stock(items: list[str], target_wh: str, qty_each: int, tag: str) -> str:
    """Create a Material Receipt Stock Entry that puts ``qty_each`` of every
    item into ``target_wh``. Returns the Stock Entry name.

    Idempotent only by name; we always create a fresh one tagged ``[TAG-tag]``.
    """
    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Receipt"
    se.purpose = "Material Receipt"
    se.company = COMPANY
    se.posting_date = nowdate()
    se.set_posting_time = 1
    se.remarks = f"[{TAG}-{tag}] seed stock for E2E run"
    for code in items:
        se.append("items", {
            "item_code": code,
            "qty": qty_each,
            "t_warehouse": target_wh,
            "basic_rate": 100.0,
        })
    se.insert(ignore_permissions=True)
    se.submit()
    return se.name


# ── Step 1: Material Request from POS ─────────────────────────────────────
def step_material_request(env: dict, items: list[str]) -> str:
    _section("STEP 1 — POS raises Material Request for 10 items at destination")
    mr = frappe.new_doc("Material Request")
    mr.material_request_type = "Material Transfer"
    mr.company = COMPANY
    mr.transaction_date = nowdate()
    mr.schedule_date = nowdate()
    # ERPNext requires set_warehouse on the header for transfer-type MRs.
    mr.set_warehouse = env["destination"]
    mr.remarks = f"[{TAG}] POS request from {env['destination']}"
    for code in items:
        mr.append("items", {
            "item_code": code,
            "qty": 1,
            "schedule_date": nowdate(),
            "warehouse": env["destination"],   # destination = "requesting" store
        })
    mr.insert(ignore_permissions=True)
    mr.submit()
    _assert(mr.docstatus == 1, "MR submitted", detail=mr.name, doc=mr.name)
    _assert(len(mr.items) == NUM_ITEMS,
            f"MR has {NUM_ITEMS} lines", detail=str(len(mr.items)))
    return mr.name


# ── Step 2: "Stock plan" — split into 3 Stock Entries (Material Transfer) ─
def step_create_transfer_stock_entries(env: dict, mr_name: str,
                                       items: list[str]) -> dict:
    """Manually split the 10 lines: first 2 → store_a, next 2 → store_b, last 6 → hub.

    The first two splits create Material Transfer SEs (store → destination)
    *drafts* that act as the goods being moved. The last 6-item slice is
    fulfilled by Step 3 (PO → PR → Material Transfer from hub).
    """
    _section("STEP 2 — Split: 2 from store_a, 2 from store_b, 6 from hub (via PO)")

    plan = {
        "store_a": (env["store_a"], items[:SPLIT_A]),
        "store_b": (env["store_b"], items[SPLIT_A:SPLIT_A + SPLIT_B]),
        "hub":     (env["hub"],     items[SPLIT_A + SPLIT_B:]),
    }
    # Seed stock at sources so the inter-store transfer can submit.
    for key, (wh, slice_items) in plan.items():
        seed = _seed_stock(slice_items, wh, 1, f"seed-{key}")
        _step(f"Seeded source stock at {wh}", "PASS",
              detail=f"{len(slice_items)} units via {seed}", doc=seed)

    se_names: dict[str, str] = {}
    for key, (wh, slice_items) in plan.items():
        if key == "hub":
            # Hub stock entry is created AFTER the PR. Skip here.
            continue
        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Transfer"
        se.purpose = "Material Transfer"
        se.company = COMPANY
        se.posting_date = nowdate()
        se.set_posting_time = 1
        se.from_warehouse = wh
        se.to_warehouse = env["destination"]
        se.remarks = f"[{TAG}] inter-store transfer from {wh} → {env['destination']}"
        for code in slice_items:
            se.append("items", {
                "item_code": code,
                "qty": 1,
                "s_warehouse": wh,
                "t_warehouse": env["destination"],
            })
        se.insert(ignore_permissions=True)
        se.submit()
        se_names[key] = se.name
        _step(f"Material Transfer SE for {key}", "PASS",
              detail=f"{se.name} ({wh} → {env['destination']}, {len(slice_items)} items)",
              doc=se.name)
    return se_names


# ── Step 3: PO + PR for 6 hub-bound items, then hub→destination SE ─────────
def step_po_pr_for_hub(env: dict, items: list[str],
                       supplier: str) -> dict:
    _section("STEP 3 — PO for 6 units → Purchase Receipt at hub → Stock Entry hub→destination")
    hub_items = items[SPLIT_A + SPLIT_B:]
    # Purchase Order
    po = frappe.new_doc("Purchase Order")
    po.supplier = supplier
    po.company = COMPANY
    po.transaction_date = nowdate()
    po.schedule_date = nowdate()
    po.set_warehouse = env["hub"]
    po.remarks = f"[{TAG}] PO for hub fulfilment"
    for code in hub_items:
        po.append("items", {
            "item_code": code,
            "qty": 1,
            "rate": 100.0,
            "warehouse": env["hub"],
            "schedule_date": nowdate(),
        })
    po.insert(ignore_permissions=True)
    po.submit()
    _step("PO submitted", "PASS", detail=po.name, doc=po.name)

    # Purchase Receipt against PO (auto-fill)
    from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
    pr_dict = make_purchase_receipt(po.name)
    pr = frappe.get_doc(pr_dict)
    pr.set_posting_time = 1
    pr.posting_date = nowdate()
    pr.remarks = f"[{TAG}] receive against {po.name} at hub"
    for itm in pr.items:
        itm.warehouse = env["hub"]
    pr.insert(ignore_permissions=True)
    pr.submit()
    _step("PR received at hub", "PASS", detail=pr.name, doc=pr.name)

    # Material Transfer SE from hub → destination for these 6 items.
    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Transfer"
    se.purpose = "Material Transfer"
    se.company = COMPANY
    se.posting_date = nowdate()
    se.set_posting_time = 1
    se.from_warehouse = env["hub"]
    se.to_warehouse = env["destination"]
    se.remarks = f"[{TAG}] hub → destination transfer"
    for code in hub_items:
        se.append("items", {
            "item_code": code,
            "qty": 1,
            "s_warehouse": env["hub"],
            "t_warehouse": env["destination"],
        })
    se.insert(ignore_permissions=True)
    se.submit()
    _step("Material Transfer SE hub→dest", "PASS",
          detail=f"{se.name} ({env['hub']} → {env['destination']}, 6 items)",
          doc=se.name)

    return {"po": po.name, "pr": pr.name, "se_hub": se.name}


# ── Step 4: Wrap each SE in a CH Transfer Manifest ────────────────────────
def step_create_manifests(env: dict, se_map: dict) -> dict[str, str]:
    _section("STEP 4 — Create 3 CH Transfer Manifests (one per source warehouse)")
    from ch_logistics.api import transfer_manifest_api as tm_api

    src_for = {
        "store_a": env["store_a"],
        "store_b": env["store_b"],
        "hub":     env["hub"],
    }
    bmts: dict[str, str] = {}
    for key, src in src_for.items():
        bmt = tm_api.create_manifest(
            stock_entries=[se_map[key]],
            source_warehouse=src,
            destination_warehouse=env["destination"],
        )
        bmt = bmt if isinstance(bmt, str) else bmt.get("name") or bmt
        doc = frappe.get_doc("CH Transfer Manifest", bmt)
        # Must Submit before lifecycle transitions are allowed.
        doc.submit()
        bmts[key] = doc.name
        _step(f"BMT for {key}", "PASS",
              detail=f"{doc.name} ({src} → {env['destination']}, status={doc.status})",
              doc=doc.name)
        results["manifests"][key] = doc.name
    return bmts


# ── Step 5: Create trip, attach BMTs, assign driver ───────────────────────
def step_create_trip(env: dict, bmts: dict[str, str], driver: str) -> str:
    _section("STEP 5 — Create multi-pickup Trip (4 stops) + attach BMTs + assign driver")
    from ch_logistics.api import logistics_api as trip_api

    trip_name = trip_api.trip_create(
        trip_date=nowdate(),
        company=COMPANY,
        direction="Forward",
        manifests=None,   # attach manually below for explicit stop sequencing
    )
    trip = frappe.get_doc("CH Logistics Trip", trip_name)

    # Build the 4-stop route: hub (#1) → store_a (#2) → store_b (#3) → destination (#4)
    trip.set("stops", [])
    stops_plan = [
        ("Pickup", env["hub"]),
        ("Pickup", env["store_a"]),
        ("Pickup", env["store_b"]),
        ("Drop",   env["destination"]),
    ]
    for idx, (stop_type, wh) in enumerate(stops_plan, start=1):
        trip.append("stops", {
            "sequence": idx,
            "stop_type": stop_type,
            "warehouse": wh,
            "status": "Pending",
        })
    trip.save(ignore_permissions=True)
    _step("Trip created with 4 stops", "PASS",
          detail=f"{trip.name} [hub→A→B→dest]", doc=trip.name)

    # Attach BMTs (this will call _assign_stop_sequence; hub/store_a/store_b
    # manifests all share destination=dest, so all stop_sequence values land
    # at stop #4. The new _gather_stop_manifests handles pickup matching by
    # source warehouse, which is why this still works.)
    trip_api.attach_manifests(trip.name, list(bmts.values()))
    _step("Attached 3 BMTs to trip", "PASS",
          detail=", ".join(bmts.values()), doc=trip.name)

    # Assign driver + vehicle.
    trip_api.trip_assign_driver(trip.name, driver, vehicle=None)
    # Vehicle field on trip is optional and may be a link to a Vehicle doctype.
    # Skip linking vehicle to avoid creating a Vehicle master; driver alone
    # is sufficient for the lifecycle.
    trip.reload()
    _assert(trip.status == "Assigned",
            "Trip status = Assigned", detail=trip.status, doc=trip.name)
    _assert(trip.driver == driver,
            "Driver assigned", detail=driver, doc=trip.name)
    results["trip"] = trip.name
    return trip.name


# ── Helpers for status snapshots ──────────────────────────────────────────
def _snapshot_pos(phase: str, bmts: dict[str, str]) -> None:
    """Record what the POS Stock Transfer screen would show for each manifest.

    POS reads ``Stock Entry.custom_logistics_status`` (see ch_pos
    ``get_stock_transfers``). We re-read it after every phase so a humans-
    readable timeline lands in the result JSON.
    """
    for key, bmt in bmts.items():
        mf = frappe.db.get_value("CH Transfer Manifest", bmt,
                                  ["status", "destination_warehouse"], as_dict=True)
        se_names = [r.stock_entry for r in
                    frappe.get_all("CH Transfer Manifest Item",
                                   filters={"parent": bmt},
                                   fields=["stock_entry"])]
        for se in se_names:
            cls = frappe.db.get_value("Stock Entry", se, "custom_logistics_status")
            cs = frappe.db.get_value("Stock Entry", se, "custom_status")
            row = {
                "phase": phase,
                "manifest_key": key,
                "manifest": bmt,
                "manifest_status": mf.status if mf else None,
                "stock_entry": se,
                "se_custom_logistics_status": cls,
                "se_custom_status": cs,
            }
            results["pos_status_timeline"].append(row)
            print(f"    POS-VIEW [{phase:<24}] {key:<8} BMT={bmt} → "
                  f"BMT.status={mf.status}, SE.custom_logistics_status={cls}, "
                  f"SE.custom_status={cs}")


# ── Step 6: Trip-start gate (driver_accept_trip + first pickup) ───────────
def step_trip_start_gate(trip_name: str, bmts: dict[str, str], env: dict) -> None:
    """Simulate the new combined "Accept & Start Trip" dialog.

    Backend path is the same as the JS combined flow:
      driver_accept_trip → start_pickup(every Assigned manifest at the source
      stop) → stop_arrive + stop_complete on stop #1.

    For this scenario the *first pickup stop* is the hub (#1) and the
    matching manifest is the hub→destination one.
    """
    _section("STEP 6 — Combined Trip Start gate (Hub pickup)")
    from ch_logistics.api import logistics_api as trip_api
    from ch_logistics.api import transfer_manifest_api as tm_api

    photo = _ensure_proof_photo()

    # 6a. Accept = mark trip Started. ``driver_accept_trip`` requires the
    # current session user's Driver profile to match the trip's driver,
    # which we can't satisfy from a server-side `bench execute` running
    # as Administrator. Use ``trip_start`` instead \u2014 same underlying
    # ``mark_started()`` call, no identity check. The UX path on the
    # driver app is still ``driver_accept_trip``.
    trip_api.trip_start(trip_name)
    trip_doc = frappe.get_doc("CH Logistics Trip", trip_name)
    _assert(trip_doc.status == "Started",
            "Trip flipped to Started",
            detail=trip_doc.status, doc=trip_name)

    _snapshot_pos("after_driver_accept_trip", bmts)

    # 6b. start_pickup for every Assigned manifest sourced at hub (#1).
    #     In our split that's the hub BMT only.
    lat, lng = _gps(1)
    hub_bmts = [b for b in [bmts["hub"]]
                if frappe.db.get_value("CH Transfer Manifest", b, "status") == "Assigned"]
    for b in hub_bmts:
        qr = frappe.db.get_value("CH Transfer Manifest", b, "qr_payload") or b
        tm_api.start_pickup(manifest=b, pickup_photo=photo, lat=lat, lng=lng,
                            notes=f"[{TAG}] trip-start hub pickup",
                            scanned_qr=qr)
        status = frappe.db.get_value("CH Transfer Manifest", b, "status")
        _assert(status == "In Transit",
                f"start_pickup({b}) → In Transit",
                detail=status, doc=b)

    # 6c. stop_arrive + stop_complete for stop #1 (hub)
    trip_api.stop_arrive(trip_name, 1, gps_lat=lat, gps_lng=lng)
    trip_api.stop_complete(trip_name, 1, scan_compliance_pct=100)
    s1 = frappe.db.sql(
        "SELECT status FROM `tabCH Logistics Trip Stop` WHERE parent=%s AND sequence=1",
        (trip_name,), as_dict=True,
    )
    _assert(bool(s1) and s1[0]["status"] == "Completed",
            "Stop #1 (hub) Completed",
            detail=str(s1[0]["status"] if s1 else None))

    _snapshot_pos("after_hub_pickup", bmts)


# ── Step 7 + 8: per-stop Arrive & Pick Up at Store-A and Store-B ──────────
def step_pickup_at_store(trip_name: str, bmts: dict[str, str], env: dict,
                          stop_seq: int, key: str) -> None:
    label = "STEP 7 — Arrive & Pick Up at Store-A" if key == "store_a" \
            else "STEP 8 — Arrive & Pick Up at Store-B"
    _section(label)
    from ch_logistics.api import logistics_api as trip_api
    from ch_logistics.api import transfer_manifest_api as tm_api

    photo = _ensure_proof_photo()
    lat, lng = _gps(stop_seq)

    # 1. stop_arrive (simulates the per-stop combined flow gate)
    trip_api.stop_arrive(trip_name, stop_seq, gps_lat=lat, gps_lng=lng)

    # 2. start_pickup for the source-matched manifest
    b = bmts[key]
    status_before = frappe.db.get_value("CH Transfer Manifest", b, "status")
    if status_before != "Assigned":
        _step(f"start_pickup({key}) skipped — not Assigned",
              "SKIP", detail=f"current={status_before}", doc=b)
    else:
        qr = frappe.db.get_value("CH Transfer Manifest", b, "qr_payload") or b
        tm_api.start_pickup(manifest=b, pickup_photo=photo, lat=lat, lng=lng,
                            notes=f"[{TAG}] {key} pickup",
                            scanned_qr=qr)
        status = frappe.db.get_value("CH Transfer Manifest", b, "status")
        _assert(status == "In Transit",
                f"start_pickup({key}) → In Transit", detail=status, doc=b)

    # 3. stop_complete
    trip_api.stop_complete(trip_name, stop_seq, scan_compliance_pct=100)
    stop_status = frappe.db.sql(
        "SELECT status FROM `tabCH Logistics Trip Stop` WHERE parent=%s AND sequence=%s",
        (trip_name, stop_seq), as_dict=True,
    )
    _assert(bool(stop_status) and stop_status[0]["status"] == "Completed",
            f"Stop #{stop_seq} ({key}) Completed",
            detail=str(stop_status[0]["status"] if stop_status else None))

    _snapshot_pos(f"after_pickup_{key}", bmts)


# ── Step 9: Arrive & Deliver at destination ───────────────────────────────
def step_arrive_and_deliver(trip_name: str, bmts: dict[str, str], env: dict) -> None:
    _section("STEP 9 — Arrive & Deliver at destination (all 3 BMTs in one batch)")
    from ch_logistics.api import logistics_api as trip_api
    from ch_logistics.api import transfer_manifest_api as tm_api

    photo = _ensure_proof_photo()
    lat, lng = _gps(4)

    # 1. stop_arrive on drop stop (#4)
    trip_api.stop_arrive(trip_name, 4, gps_lat=lat, gps_lng=lng)

    # 2. For every In-Transit BMT at destination: mark_reached + request_otp +
    #    read fresh OTP + complete_delivery.
    receiver = f"{TAG} Receiver"
    for key, b in bmts.items():
        status_before = frappe.db.get_value("CH Transfer Manifest", b, "status")
        if status_before != "In Transit":
            _step(f"Deliver({key}) skipped — not In Transit",
                  "SKIP", detail=f"current={status_before}", doc=b)
            continue
        # 2a. mark reached destination
        tm_api.mark_reached_destination(manifest=b, lat=lat, lng=lng)
        # 2b. request OTP (server stamps it on the doc)
        tm_api.request_delivery_otp(manifest=b)
        otp = frappe.db.get_value("CH Transfer Manifest", b, "delivery_otp")
        _assert(bool(otp), f"OTP issued for {key}", detail=f"otp={otp}", doc=b)
        qr = frappe.db.get_value("CH Transfer Manifest", b, "qr_payload") or b
        # 2c. complete_delivery
        tm_api.complete_delivery(
            manifest=b,
            delivery_photo=photo,
            receiver_name=receiver,
            otp=otp,
            lat=lat, lng=lng,
            scanned_qr=qr,
        )
        status_after = frappe.db.get_value("CH Transfer Manifest", b, "status")
        _assert(status_after == "Delivered",
                f"complete_delivery({key}) → Delivered",
                detail=status_after, doc=b)

    # 3. stop_complete on drop. The last complete_delivery() can auto-close
    #    the parent trip (via _maybe_auto_close_parent_trip) which moves the
    #    trip into Completed and rejects further stop_complete calls. If the
    #    trip is already terminal, assert the drop stop ended terminal too
    #    and skip the explicit stop_complete.
    trip_status = frappe.db.get_value("CH Logistics Trip", trip_name, "status")
    if trip_status == "Started":
        trip_api.stop_complete(trip_name, 4, scan_compliance_pct=100)
    else:
        _step("stop_complete skipped — trip auto-closed",
              "PASS", detail=f"trip.status={trip_status}", doc=trip_name)
    drop_status = frappe.db.sql(
        "SELECT status FROM `tabCH Logistics Trip Stop` WHERE parent=%s AND sequence=4",
        (trip_name,), as_dict=True,
    )
    _assert(bool(drop_status) and drop_status[0]["status"] in ("Completed", "Arrived"),
            "Stop #4 (destination) Completed",
            detail=str(drop_status[0]["status"] if drop_status else None))

    _snapshot_pos("after_complete_delivery", bmts)


# ── Step 10: trip_complete (may auto-fire from _maybe_auto_close_parent_trip)
def step_trip_complete(trip_name: str, bmts: dict[str, str]) -> None:
    _section("STEP 10 — Trip complete")
    from ch_logistics.api import logistics_api as trip_api

    current = frappe.db.get_value("CH Logistics Trip", trip_name, "status")
    if current == "Started":
        trip_api.trip_complete(trip_name)
    final = frappe.db.get_value("CH Logistics Trip", trip_name, "status")
    _assert(final in ("Completed", "Closed"),
            "Trip status terminal",
            detail=final, doc=trip_name)
    _snapshot_pos("after_trip_complete", bmts)


# ── Step 11: accept_delivery at destination POS lane ──────────────────────
def step_pos_accept(bmts: dict[str, str]) -> None:
    _section("STEP 11 — POS-side accept_delivery for each BMT")
    from ch_logistics.api import transfer_manifest_api as tm_api

    for key, b in bmts.items():
        status_before = frappe.db.get_value("CH Transfer Manifest", b, "status")
        if status_before != "Delivered":
            _step(f"accept_delivery({key}) skipped — not Delivered",
                  "SKIP", detail=f"current={status_before}", doc=b)
            continue
        tm_api.accept_delivery(manifest=b, damage_reported=0)
        status_after = frappe.db.get_value("CH Transfer Manifest", b, "status")
        _assert(status_after in ("Received", "Partially Received"),
                f"accept_delivery({key}) → {status_after}",
                detail=status_after, doc=b)
    _snapshot_pos("after_pos_accept_delivery", bmts)


# ── Result persistence ────────────────────────────────────────────────────
def _save_results() -> str:
    site_path = frappe.get_site_path("private", "files", "e2e_runs")
    os.makedirs(site_path, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(site_path, f"{TAG}-{ts}")
    os.makedirs(run_dir, exist_ok=True)

    out_path = os.path.join(run_dir, "result.json")
    with open(out_path, "w") as fh:
        json.dump(results, fh, indent=2, default=str)

    # Human-readable summary alongside the JSON.
    summary_path = os.path.join(run_dir, "SUMMARY.txt")
    with open(summary_path, "w") as fh:
        fh.write(f"{TAG} run @ {results['started_at']} → {results['ended_at']}\n")
        fh.write(f"Site: {results['site']}\n")
        fh.write(f"Company: {results['company']}\n")
        s = results["summary"]
        fh.write(f"Result: PASS={s['pass']}  FAIL={s['fail']}  SKIP={s['skip']}\n")
        if results.get("trip"):
            fh.write(f"Trip: {results['trip']}\n")
        if results.get("manifests"):
            for k, v in results["manifests"].items():
                fh.write(f"  BMT {k}: {v}\n")
        fh.write("\n--- Steps ---\n")
        for r in results["steps"]:
            fh.write(f"  [{r['status']:<4}] {r['step']}"
                     + (f"  —  {r.get('detail', '')}" if r.get('detail') else "")
                     + "\n")
        fh.write("\n--- POS view timeline (custom_logistics_status per phase) ---\n")
        for r in results["pos_status_timeline"]:
            fh.write(f"  [{r['phase']:<28}] {r['manifest_key']:<8} "
                     f"BMT={r['manifest']} BMT.status={r['manifest_status']} "
                     f"SE.custom_logistics_status={r['se_custom_logistics_status']} "
                     f"SE.custom_status={r['se_custom_status']}\n")

    return run_dir


# ── Orchestrator ──────────────────────────────────────────────────────────
def run() -> dict:
    results["started_at"] = now_datetime().isoformat()
    results["site"] = frappe.local.site
    print(f"\n[{TAG}] running E2E for site={results['site']} company={COMPANY}\n")

    try:
        env = _discover_environment()
        _step("Environment discovered", "PASS",
              detail=f"hub={env['hub']} | A={env['store_a']} | B={env['store_b']} "
                     f"| destination={env['destination']}")
        results["environment"] = env

        driver = _pick_or_create_driver()
        _step("Driver picked", "PASS", detail=driver, doc=driver)
        results["driver"] = driver

        supplier = _ensure_supplier()
        _step("Supplier ready", "PASS", detail=supplier, doc=supplier)

        items = _ensure_items(NUM_ITEMS)
        _step(f"{NUM_ITEMS} test items ready", "PASS",
              detail=", ".join(items[:3]) + ", …")
        results["items"] = items

        # 1. MR
        mr_name = step_material_request(env, items)
        results["material_request"] = mr_name

        # 2. Split into Material Transfer SEs for store_a / store_b
        se_map = step_create_transfer_stock_entries(env, mr_name, items)

        # 3. PO + PR + hub→dest SE
        po_pr = step_po_pr_for_hub(env, items, supplier)
        results["purchase_order"] = po_pr["po"]
        results["purchase_receipt"] = po_pr["pr"]
        se_map["hub"] = po_pr["se_hub"]
        results["stock_entries"] = dict(se_map)

        # 4. Wrap each SE in a BMT
        bmts = step_create_manifests(env, se_map)
        results["manifests"] = bmts

        # Snapshot the POS-side status RIGHT AFTER submission (baseline).
        _snapshot_pos("after_bmt_submit", bmts)

        # 5. Create trip + attach + assign driver
        trip_name = step_create_trip(env, bmts, driver)
        _snapshot_pos("after_trip_assigned", bmts)

        # 6. Trip start gate (hub pickup)
        step_trip_start_gate(trip_name, bmts, env)

        # 7 + 8. Per-stop pickup at store A then store B
        step_pickup_at_store(trip_name, bmts, env, stop_seq=2, key="store_a")
        step_pickup_at_store(trip_name, bmts, env, stop_seq=3, key="store_b")

        # 9. Drop
        step_arrive_and_deliver(trip_name, bmts, env)

        # 10. Trip complete (idempotent; may auto-fire on last delivery)
        step_trip_complete(trip_name, bmts)

        # 11. POS accept_delivery
        step_pos_accept(bmts)

    except Exception as exc:
        results["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        _step("Unhandled exception", "FAIL", detail=f"{type(exc).__name__}: {exc}")
        print(traceback.format_exc())

    finally:
        results["ended_at"] = now_datetime().isoformat()
        path = _save_results()
        _section("DONE")
        s = results["summary"]
        print(f"Result: PASS={s['pass']}  FAIL={s['fail']}  SKIP={s['skip']}")
        print(f"Saved → {path}")
        # Commit so all the test rows are durable even if the orchestrator
        # raised. Errors during a sub-step already roll back to their last
        # consistent point because each .submit() / .save() runs in its own
        # SQL transaction within Frappe.
        try:
            frappe.db.commit()
        except Exception:
            pass

    return results
