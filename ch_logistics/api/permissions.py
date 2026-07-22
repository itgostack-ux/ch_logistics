"""Permission helpers exposed via hooks."""
from __future__ import annotations

import frappe


def has_logistics_access() -> bool:
	"""True if the user has any logistics-related role (central registry)."""
	from ch_logistics import roles as role_registry
	return role_registry.user_has("app_access")
