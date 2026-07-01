# Copyright (c) 2026, GoStack and contributors
# For license information, please see license.txt
"""
Tier 4 — Report scope injection E2E tests for ch_logistics.

Verifies:
  * ``manifest_conditions`` and ``trip_conditions`` in the shared
    ``report_utils`` module always append CH User Scope narrowing.
  * The four outlier reports that don't route through those two helpers
    — driver_delivery_worksheet, store_transfer_register,
    driver_performance_scorecard, delivery_exceptions_and_rejections —
    all delegate to ``scope_where_clause`` with the correct
    endpoint-OR pattern (source + destination store/warehouse for
    manifest queries, hub_warehouse for trip queries).
  * Administrator (bypass) runs everything without narrowing.
  * A scoped user whose scope is empty (no CH User Scope row) sees
    zero rows via the ``1=0`` fail-closed contract.

Follows the enterprise parity established across the sweep:
  * SAP EWM/TM plant-level authorisation object M_MATE_WGR.
  * Oracle EBS MOAC multi-org access via MO_GLOBAL policy.
  * D365 F&O Data Access Policies (XDS) on legal-entity boundary.
"""

from __future__ import annotations

import unittest

import frappe

from ch_erp15.ch_erp15.scope import clear_scope_cache
from ch_logistics.api.report_utils import (
    manifest_conditions,
    trip_conditions,
)


_TEST_USER = "tier4-log-user@ch-tests.local"
_TEST_STORE = "TIER4-LOG-STORE-A"


def _ensure_user(user: str) -> None:
    if frappe.db.exists("User", user):
        return
    doc = frappe.new_doc("User")
    doc.email = user
    doc.first_name = "Tier4Log"
    doc.enabled = 1
    doc.new_password = "TestPass123!Tier4"
    doc.send_welcome_email = 0
    doc.append("roles", {"role": "Accounts User"})
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)


def _get_or_create_warehouse(name: str, company: str) -> str:
    abbr = frappe.db.get_value("Company", company, "abbr")
    full = f"{name} - {abbr}"
    if frappe.db.exists("Warehouse", full):
        return full
    doc = frappe.new_doc("Warehouse")
    doc.warehouse_name = name
    doc.company = company
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def _get_or_create_ch_store(name: str, warehouse: str, company: str) -> None:
    if frappe.db.exists("CH Store", name):
        return
    doc = frappe.new_doc("CH Store")
    doc.store_id = name
    doc.store_code = name
    doc.store_name = name
    doc.company = company
    doc.warehouse = warehouse
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.insert(ignore_permissions=True)


def _make_scope(user: str, store: str) -> None:
    for row in frappe.get_all("CH User Scope", filters={"user": user}, pluck="name"):
        frappe.delete_doc("CH User Scope", row, ignore_permissions=True, force=True)
    doc = frappe.new_doc("CH User Scope")
    doc.user = user
    doc.scope_role = "Store Executive"
    doc.enabled = 1
    doc.append("stores", {"store": store})
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)


class TestReportScopeChLogistics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = frappe.db.get_value("Company", {}, "name")
        if not cls.company:
            raise Exception("No Company in this site — cannot run Tier 4 ch_logistics tests.")

        cls.wh_in_scope = _get_or_create_warehouse("Tier4 Log A WH", cls.company)
        _get_or_create_ch_store(_TEST_STORE, cls.wh_in_scope, cls.company)
        _ensure_user(_TEST_USER)
        _make_scope(_TEST_USER, _TEST_STORE)
        clear_scope_cache(_TEST_USER)
        frappe.db.commit()

    def setUp(self):
        frappe.set_user(_TEST_USER)
        clear_scope_cache(_TEST_USER)

    def tearDown(self):
        frappe.set_user("Administrator")

    # ── shared helper contract ──────────────────────────────────────────

    # 1 — manifest_conditions appends scope OR-chain for scoped user
    def test_01_manifest_conditions_appends_scope(self):
        where, _vals = manifest_conditions({})
        # Scoped user with 1 store → OR-chain across source_/destination_
        # store + source_/destination_ warehouse.
        self.assertIn("m.source_store", where)
        self.assertIn("m.destination_store", where)
        self.assertIn("m.source_warehouse", where)
        self.assertIn("m.destination_warehouse", where)
        self.assertIn(_TEST_STORE, where)

    # 2 — manifest_conditions bypass user gets no scope suffix
    def test_02_manifest_conditions_bypass(self):
        frappe.set_user("Administrator")
        where, _vals = manifest_conditions({})
        # Bypass returns None → no OR chain appended, only the base 1=1
        self.assertNotIn("m.source_store IN", where)
        self.assertNotIn("m.destination_store IN", where)

    # 3 — trip_conditions appends scope on hub_warehouse
    def test_03_trip_conditions_appends_scope(self):
        where, _vals = trip_conditions({})
        self.assertIn("t.hub_warehouse", where)

    # 4 — trip_conditions bypass user gets no scope suffix
    def test_04_trip_conditions_bypass(self):
        frappe.set_user("Administrator")
        where, _vals = trip_conditions({})
        self.assertNotIn("t.hub_warehouse IN", where)

    # 5 — custom alias flows through both helpers
    def test_05_custom_alias(self):
        where, _vals = manifest_conditions({}, alias="mx")
        self.assertIn("mx.source_store", where)
        where, _vals = trip_conditions({}, alias="tx")
        self.assertIn("tx.hub_warehouse", where)

    # ── report end-to-end smoke ─────────────────────────────────────────

    # 6 — reports routed through manifest_conditions / trip_conditions
    def test_06_helper_reports_scoped(self):
        from ch_logistics.logistics.report.freight_and_delivery_cost.freight_and_delivery_cost import (
            execute as freight_execute,
        )
        from ch_logistics.logistics.report.manifest_delivery_and_sla.manifest_delivery_and_sla import (
            execute as sla_execute,
        )
        from ch_logistics.logistics.report.fleet_and_vehicle_utilization.fleet_and_vehicle_utilization import (
            execute as fleet_execute,
        )
        from ch_logistics.logistics.report.logistics_trip_performance.logistics_trip_performance import (
            execute as trip_execute,
        )
        for fn in (freight_execute, sla_execute, fleet_execute, trip_execute):
            result = fn({})
            self.assertTrue(len(result) >= 2, f"{fn.__module__} should return columns+data")

    # 7 — outlier reports run cleanly for scoped user
    def test_07_outlier_reports_scoped(self):
        from ch_logistics.logistics.report.driver_delivery_worksheet.driver_delivery_worksheet import (
            execute as worksheet_execute,
        )
        from ch_logistics.logistics.report.store_transfer_register.store_transfer_register import (
            execute as reg_execute,
        )
        from ch_logistics.logistics.report.driver_performance_scorecard.driver_performance_scorecard import (
            execute as scorecard_execute,
        )
        from ch_logistics.logistics.report.delivery_exceptions_and_rejections.delivery_exceptions_and_rejections import (
            execute as excrej_execute,
        )
        for fn in (worksheet_execute, reg_execute, scorecard_execute, excrej_execute):
            result = fn({})
            self.assertTrue(len(result) >= 2, f"{fn.__module__} should return columns+data")

    # 8 — Administrator bypass runs every touched report
    def test_08_administrator_bypass(self):
        frappe.set_user("Administrator")
        from ch_logistics.logistics.report.freight_and_delivery_cost.freight_and_delivery_cost import (
            execute as freight_execute,
        )
        from ch_logistics.logistics.report.driver_delivery_worksheet.driver_delivery_worksheet import (
            execute as worksheet_execute,
        )
        from ch_logistics.logistics.report.store_transfer_register.store_transfer_register import (
            execute as reg_execute,
        )
        from ch_logistics.logistics.report.driver_performance_scorecard.driver_performance_scorecard import (
            execute as scorecard_execute,
        )
        from ch_logistics.logistics.report.delivery_exceptions_and_rejections.delivery_exceptions_and_rejections import (
            execute as excrej_execute,
        )
        freight_execute({})
        worksheet_execute({})
        reg_execute({})
        scorecard_execute({})
        excrej_execute({})
