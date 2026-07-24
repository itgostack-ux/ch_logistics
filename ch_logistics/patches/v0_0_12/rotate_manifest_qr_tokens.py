import frappe


def execute():
    if not frappe.db.has_column("CH Transfer Manifest", "qr_payload"):
        return
    start = 0
    page_length = 500
    while True:
        rows = frappe.get_all(
            "CH Transfer Manifest",
            filters=[
                ["CH Transfer Manifest", "docstatus", "<", 2],
                ["CH Transfer Manifest", "status", "not in", ["Closed", "Cancelled", "Returned"]],
            ],
            fields=["name", "qr_payload"],
            order_by="name asc",
            limit_start=start,
            limit_page_length=page_length,
        )
        if not rows:
            break
        updates = {
            row.name: {"qr_payload": frappe.generate_hash(length=32)}
            for row in rows
            if not row.qr_payload or row.qr_payload == row.name or len(row.qr_payload) < 22
        }
        if updates:
            frappe.db.bulk_update("CH Transfer Manifest", updates, update_modified=False)
        start += len(rows)
