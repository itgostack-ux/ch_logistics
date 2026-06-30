"""Propagate CH Store lat/lng onto their warehouse(s) so the map + delivery
optimizer (which read Warehouse.custom_latitude/longitude) have coordinates for
every store. Idempotent; fills blanks only."""
import frappe


def execute():
    try:
        from ch_logistics.api.optimizer import sync_store_geo_to_warehouses
        res = sync_store_geo_to_warehouses()
        frappe.logger("ch_logistics").info(f"store geo sync: {res}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "v0_0_10 sync_store_geo_to_warehouses failed")
