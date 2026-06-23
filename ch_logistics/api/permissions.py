"""Permission helpers exposed via hooks."""
from __future__ import annotations

import frappe


def has_logistics_access() -> bool:
	"""True if the user has any logistics-related role."""
	roles = set(frappe.get_roles())
	return bool(roles & {"System Manager", "Logistics Manager", "Logistics User"})
