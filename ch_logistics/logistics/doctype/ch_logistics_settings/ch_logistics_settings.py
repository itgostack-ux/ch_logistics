import frappe
from frappe.model.document import Document


class CHLogisticsSettings(Document):
    pass


def get_settings():
    """Cached accessor for the single. Falls back to sane defaults if the doc
    has not been initialised yet."""
    return frappe.get_cached_doc("CH Logistics Settings")
