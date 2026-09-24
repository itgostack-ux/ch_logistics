# Copyright (c) 2026, GoStack and contributors

"""Getting audit-verified damaged stock from a store to the hub.

The audit half lives in ch_erp15, which is the base layer every other app
depends on and so must not reach into logistics. This is the other side of
that line: ch_logistics already depends on ch_erp15, so it listens for a
closed Damaged Stock Verification and raises the transfer.

Nothing bespoke is invented for the journey. The stock moves exactly the way
every other inter-store movement on this bench moves, and for the same reason:
``procurement_guardrails.enforce_no_direct_material_transfer_submit`` forbids a
direct Material Transfer between locations, so goods cannot jump from a store
to the hub without transit visibility. The audit produces a DRAFT Stock Entry
and a DRAFT manifest, and from there it is the ordinary flow --

    audit closes  ->  Draft manifest + draft Stock Entry (Pending With Goods)
                        |
                      the STORE packs it and marks it ready for pickup
                        |
                      trip, pickup, delivery, receive at the hub

which is the division the floor already works to: the audit team says what
leaves, the store hands it over, logistics carries it. Raising the request and
packing it are deliberately two different people.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate


def _hub_warehouse(company: str) -> str | None:
    """Where a company's damaged stock is consolidated."""
    return (frappe.db.get_value("Company", company, "damaged_stock_warehouse")
            or frappe.db.get_value("Company", company, "master_hub_warehouse"))


def on_audit_session_update(doc, method=None):
    """Raise the transfer once a damaged-stock verification is closed.

    Hooked rather than called, so the audit app needs no knowledge of
    logistics. Silent for every other audit type and every earlier status.
    """
    try:
        from ch_erp15.ch_erp15 import damaged_stock_audit as audit
    except ImportError:
        return

    if not audit.is_damaged_stock_verification(doc):
        return
    if (doc.get("status") or "") not in ("Closed", "Completed"):
        return
    if doc.get("custom_damaged_transfer_manifest"):
        return                    # already raised; closing twice must not duplicate it

    try:
        release_verified_damaged_stock(doc.name)
    except Exception:  # noqa: BLE001 - deliberate: see below
        # A failure here must not block the audit from closing. The audit is
        # the record of what was counted; the transfer is a consequence, and a
        # consequence that fails is something to retry, not a reason to lose
        # the count.
        frappe.log_error(
            title=f"Damaged stock transfer not raised for {doc.name}",
            message=frappe.get_traceback(),
        )


@frappe.whitelist()
def release_verified_damaged_stock(session: str) -> dict:
    """Draft Stock Entry + Draft manifest for what the audit actually counted.

    Both are drafts on purpose. The store has not packed anything yet, and
    saying the goods have moved before anyone has picked them up is precisely
    the claim the in-transit rule exists to prevent.
    """
    from ch_erp15.ch_erp15 import damaged_stock_audit as audit

    doc = frappe.get_doc("CH Stock Audit Session", session)
    doc.check_permission("read")

    lines = audit.verified_lines(doc)
    if not lines:
        frappe.throw(
            _("This verification counted nothing, so there is nothing to send."),
            title=_("Nothing Verified"),
        )

    company = doc.get("company")
    source = audit.damaged_bin_for(doc.get("store"))
    target = _hub_warehouse(company)
    if not source:
        frappe.throw(_("This store has no Damaged bin."), title=_("No Source Bin"))
    if not target:
        frappe.throw(
            _("{0} has no Damaged Stock Warehouse configured, so there is nowhere at "
              "the hub to send this to.").format(company),
            title=_("Hub Not Configured"),
        )
    if source == target:
        frappe.throw(
            _("This store's damaged bin IS the company's hub bin, so there is no "
              "journey to make."),
            title=_("Already At The Hub"),
        )

    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Transfer"
    se.company = company
    se.posting_date = nowdate()
    # The manifest fills each line's warehouses from the Stock Entry's HEADER,
    # not from its item rows, and seeds the trip's pickup/delivery stops off
    # those. Setting only the item-level s_warehouse/t_warehouse left the
    # manifest line blank and the journey with nowhere to stop.
    se.from_warehouse = source
    se.to_warehouse = target
    se.remarks = _("Audit-verified damaged stock released by {0}").format(session)
    if se.meta.get_field("custom_status"):
        # Waiting on the store to physically pack it -- the same state every
        # other outbound transfer starts in.
        se.custom_status = "Pending With Goods"
    for line in lines:
        row = {
            "item_code": line["item_code"],
            "qty": flt(line["qty"]),
            "s_warehouse": source,
            "t_warehouse": target,
        }
        if line.get("serials"):
            row["serial_no"] = "\n".join(line["serials"])
        se.append("items", row)
    se.flags.ignore_permissions = True
    se.insert(ignore_permissions=True)      # DRAFT: never submitted here

    manifest = frappe.new_doc("CH Transfer Manifest")
    manifest.update({
        "company": company,
        "source_warehouse": source,
        "destination_warehouse": target,
        "status": "Draft",
    })
    manifest.append("transfers", {
        "stock_entry": se.name,
        "from_warehouse": source,
        "to_warehouse": target,
        "total_qty": sum(flt(l["qty"]) for l in lines),
        "item_count": len(lines),
    })
    manifest.flags.ignore_permissions = True
    manifest.flags.ignore_mandatory = True
    manifest.insert(ignore_permissions=True)

    if doc.meta.get_field("custom_damaged_transfer_manifest"):
        doc.db_set("custom_damaged_transfer_manifest", manifest.name, update_modified=False)
    doc.add_comment(
        "Info",
        _("Damaged stock released to {0} on manifest {1} ({2} line(s)). "
          "The store packs it next.").format(target, manifest.name, len(lines)),
    )
    return {
        "ok": True, "manifest": manifest.name, "stock_entry": se.name,
        "lines": len(lines), "from": source, "to": target,
        "message": _("Manifest {0} raised. The store packs it and marks it ready for "
                     "pickup; logistics carries it from there.").format(manifest.name),
    }
