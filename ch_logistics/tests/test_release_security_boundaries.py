"""Negative security regression tests for Logistics release boundaries."""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from ch_logistics import roles, scope_guard
from ch_logistics.api import (
    digest,
    customer_tracking,
    driver_api,
    driver_resolver,
    logistics_api,
    optimizer,
    transfer_manifest_api,
)
from ch_logistics.logistics.doctype.ch_manifest_rejection.ch_manifest_rejection import (
    CHManifestRejection,
)
from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
    CHTransferManifest,
    delivery_otp_digest,
    verify_delivery_otp,
)


class _FakeDocument(dict):
    def __getattr__(self, key):
        return self.get(key)

    def as_dict(self):
        return dict(self)

    def is_new(self):
        return True


class TestLogisticsReleaseSecurityBoundaries(unittest.TestCase):
    def test_notification_policy_excludes_privileged_and_configured_roles(self):
        with (
            patch.object(
                roles.frappe,
                "get_all",
                return_value=["admin@example.com", "muted@example.com", "ops@example.com"],
            ) as get_all,
            patch.object(
                roles,
                "get_int_setting",
                side_effect=lambda fieldname, default, minimum=1: (
                    200 if fieldname == "notification_recipient_limit" else 0
                ),
            ),
            patch.object(roles, "get_list_setting", return_value={"Muted Role"}),
            patch.object(
                roles,
                "is_privileged",
                side_effect=lambda user: user == "admin@example.com",
            ),
            patch.object(
                roles.frappe,
                "get_roles",
                side_effect=lambda user: ["Muted Role"] if user == "muted@example.com" else [],
            ),
        ):
            recipients = roles.filter_notification_users(
                ["admin@example.com", "muted@example.com", "ops@example.com"]
            )
        self.assertEqual(recipients, ["ops@example.com"])
        self.assertEqual(get_all.call_count, 1)

    def test_company_digest_accepts_only_company_mapped_extra_recipients(self):
        with (
            patch(
                "ch_logistics.roles.get_notification_role_users",
                return_value=["manager@example.com"],
            ),
            patch.object(
                digest.frappe,
                "get_all",
                return_value=["manager@example.com"],
            ),
            patch.object(
                digest.frappe.db,
                "get_single_value",
                return_value="COMPANY-A|a@example.com,COMPANY-B|b@example.com,legacy@example.com",
            ),
            patch.object(roles, "filter_notification_users", return_value=["manager@example.com"]),
            patch(
                "ch_erp15.ch_erp15.notification_router.filter_users_by_company",
                return_value=["manager@example.com"],
            ),
        ):
            recipients = digest._recipients("COMPANY-A")
        self.assertEqual(recipients, ["a@example.com", "manager@example.com"])

    def test_store_manager_contacts_are_policy_filtered_and_bulk_loaded(self):
        contacts = [
            frappe._dict(
                name="ops@example.com", email="ops@example.com", mobile_no="9999999999"
            )
        ]
        with (
            patch(
                "ch_erp15.ch_erp15.store_request_api._get_store_managers",
                return_value=["admin@example.com", "ops@example.com"],
            ),
            patch.object(
                roles, "filter_notification_users", return_value=["ops@example.com"]
            ),
            patch.object(transfer_manifest_api.frappe, "get_all", return_value=contacts) as get_all,
            patch.object(transfer_manifest_api.frappe.db, "get_value") as get_value,
        ):
            users, emails, mobiles = transfer_manifest_api._collect_store_manager_contacts(
                "STORE-A"
            )
        self.assertEqual(users, ["ops@example.com"])
        self.assertEqual(emails, ["ops@example.com"])
        self.assertEqual(mobiles, ["9999999999"])
        self.assertEqual(get_all.call_count, 1)
        get_value.assert_not_called()

    def test_notification_call_sites_use_central_recipient_policy(self):
        self.assertIn(
            "filter_notification_users", inspect.getsource(customer_tracking.notify_destination)
        )
        self.assertIn(
            "filter_notification_users",
            inspect.getsource(CHTransferManifest._notify_dispatcher_rejection),
        )

    def test_unknown_capability_fails_closed(self):
        self.assertFalse(roles.user_has("not_a_real_capability", user="user@example.com"))

    def test_administrator_bypass_is_immutable(self):
        self.assertTrue(roles.is_privileged("Administrator"))

    def test_system_manager_bypass_is_immutable(self):
        with patch.object(frappe, "get_roles", return_value=["System Manager"]):
            self.assertTrue(roles.is_privileged("manager@example.com"))

    def test_empty_scope_fails_closed(self):
        with patch.object(
            scope_guard,
            "_scope",
            return_value={"bypass": False, "stores": set(), "warehouses": set(), "companies": set()},
        ):
            self.assertFalse(scope_guard.is_in_scope(store="STORE-A", company="COMPANY-A"))
            self.assertFalse(scope_guard.is_in_scope(company="COMPANY-A"))

    def test_store_scope_does_not_expand_to_sibling_company_records(self):
        with patch.object(
            scope_guard,
            "_scope",
            return_value={
                "bypass": False,
                "stores": {"STORE-A"},
                "warehouses": set(),
                "companies": {"COMPANY-A"},
            },
        ):
            self.assertTrue(scope_guard.is_in_scope(store="STORE-A", company="COMPANY-A"))
            self.assertFalse(scope_guard.is_in_scope(company="COMPANY-A"))
            self.assertFalse(scope_guard.is_in_scope(store="STORE-B", company="COMPANY-A"))

    def test_list_scope_sql_uses_bound_parameters(self):
        malicious_location = "WAREHOUSE' OR 1=1 --"
        with patch.object(
            scope_guard,
            "_scope",
            return_value={
                "bypass": False,
                "stores": set(),
                "warehouses": {malicious_location},
                "companies": {"COMPANY-A"},
            },
        ):
            clause, params = scope_guard.build_scope_sql(
                location_fields=("t.hub_warehouse",), company_field="t.company"
            )
        self.assertNotIn(malicious_location, clause)
        self.assertIn(malicious_location, params.values())

    def test_driver_cannot_access_another_drivers_trip(self):
        trip = _FakeDocument(name="TRIP-1", driver="DRIVER-B", hub_warehouse="WH-A", company="COMPANY-A")
        with (
            patch.object(roles, "is_privileged", return_value=False),
            patch.object(roles, "user_has", return_value=False),
            patch.object(driver_resolver, "resolve_current_driver", return_value="DRIVER-A"),
        ):
            with self.assertRaises(frappe.PermissionError):
                driver_resolver.assert_trip_driver_access(trip)

    def test_manifest_driver_must_match_linked_trip_driver(self):
        manifest = _FakeDocument(name="MANIFEST-1", driver="DRIVER-A", trip="TRIP-1")
        with (
            patch.object(roles, "is_privileged", return_value=False),
            patch.object(roles, "user_has", return_value=False),
            patch.object(driver_resolver, "resolve_current_driver", return_value="DRIVER-A"),
            patch.object(frappe.db, "get_value", return_value="DRIVER-B"),
        ):
            with self.assertRaises(frappe.PermissionError):
                driver_resolver.assert_manifest_driver_access(manifest)

    def test_rejection_revalidates_assignment_before_submit(self):
        manifest = _FakeDocument(driver="DRIVER-B", trip="TRIP-B")
        rejection = _FakeDocument(
            driver="DRIVER-A",
            trip="TRIP-A",
            _authorized_manifest=lambda: manifest,
        )
        with self.assertRaises(frappe.ValidationError):
            CHManifestRejection.before_submit(rejection)

    def test_rejection_rejects_nonfinite_coordinates(self):
        rejection = _FakeDocument(
            proof_image_1="/files/a.jpg",
            proof_image_2="/files/b.jpg",
            remarks="",
            latitude=float("nan"),
            longitude=80.0,
        )
        with self.assertRaises(frappe.ValidationError):
            CHManifestRejection.validate(rejection)

    def test_rejection_submit_uses_authoritative_manifest_lifecycle(self):
        manifest = Mock()
        rejection = _FakeDocument(
            rejection_reason="Material Not Ready",
            proof_image_1="/files/a.jpg",
            remarks="not ready",
            _authorized_manifest=lambda: manifest,
            db_set=Mock(),
        )
        CHManifestRejection.on_submit(rejection)
        manifest.reject_manifest.assert_called_once_with(
            rejection_reason="Material Not Ready",
            rejection_photo="/files/a.jpg",
            rejection_notes="not ready",
        )

    def test_rejection_lifecycle_failure_is_not_swallowed(self):
        manifest = Mock()
        manifest.reject_manifest.side_effect = RuntimeError("sync failed")
        rejection = _FakeDocument(
            rejection_reason="Material Not Ready",
            proof_image_1="/files/a.jpg",
            remarks="not ready",
            _authorized_manifest=lambda: manifest,
            db_set=Mock(),
        )
        with self.assertRaises(RuntimeError):
            CHManifestRejection.on_submit(rejection)

    def test_delivery_otp_is_keyed_digest_and_constant_time_verified(self):
        with patch(
            "ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest.get_encryption_key",
            return_value=b"test-site-key",
        ):
            digest = delivery_otp_digest("123456")
            self.assertTrue(digest.startswith("hmac-sha256$"))
            self.assertNotIn("123456", digest)
            self.assertTrue(verify_delivery_otp(digest, "123456"))
            self.assertFalse(verify_delivery_otp(digest, "654321"))

    def test_manifest_direct_insert_cannot_forge_proof_fields(self):
        manifest = _FakeDocument(
            status="Draft",
            delivery_otp="123456",
            flags=frappe._dict(),
            meta=SimpleNamespace(has_field=lambda _field: True),
            _SERVER_MANAGED_FIELDS=CHTransferManifest._SERVER_MANAGED_FIELDS,
        )
        with patch.object(roles, "is_privileged", return_value=False):
            with self.assertRaises(frappe.PermissionError):
                CHTransferManifest._validate_server_managed_fields(manifest)

    def test_assigned_driver_can_access_own_trip(self):
        trip = _FakeDocument(name="TRIP-1", driver="DRIVER-A")
        with (
            patch.object(roles, "is_privileged", return_value=False),
            patch.object(driver_resolver, "resolve_current_driver", return_value="DRIVER-A"),
        ):
            driver_resolver.assert_trip_driver_access(trip)

    def test_driver_cannot_attach_break_to_another_drivers_trip(self):
        with (
            patch.object(driver_api, "_current_driver", return_value="DRIVER-A"),
            patch.object(frappe.db, "get_value", return_value="DRIVER-B"),
            patch.object(driver_api.ds, "set_status") as set_status,
        ):
            with self.assertRaises(frappe.PermissionError):
                driver_api.set_break(trip="TRIP-B")
        set_status.assert_not_called()

    def test_courier_api_key_uses_password_decryption(self):
        class Courier:
            def __init__(self):
                self.called = False

            def get_password(self, fieldname, raise_exception=False):
                self.called = True
                self.assertions = (fieldname, raise_exception)
                return "decrypted-secret"

        courier = Courier()
        self.assertEqual(transfer_manifest_api._get_courier_api_key(courier), "decrypted-secret")
        self.assertTrue(courier.called)
        self.assertEqual(courier.assertions, ("api_key", False))

    def test_courier_poll_rejects_private_dns_before_sending_credentials(self):
        courier = SimpleNamespace(
            api_base_url="https://tracking.example.test/{tracking_number}",
            get_password=lambda *args, **kwargs: "secret",
        )
        with (
            patch.object(roles, "get_list_setting", return_value={"tracking.example.test"}),
            patch.object(
                roles,
                "get_int_setting",
                side_effect=lambda _field, default, minimum=1: default,
            ),
            patch.object(
                transfer_manifest_api.socket,
                "getaddrinfo",
                return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
            ),
            patch("requests.get") as request,
        ):
            with self.assertRaises(frappe.PermissionError):
                transfer_manifest_api._fetch_partner_tracking_payload(courier, "TRACK-1")
        request.assert_not_called()

    def test_courier_poll_is_allowlisted_bounded_and_never_follows_redirects(self):
        courier = SimpleNamespace(
            api_base_url="https://tracking.example.test/{tracking_number}",
            get_password=lambda *args, **kwargs: "secret",
        )

        class Response:
            status_code = 200
            headers = {"Content-Length": "15"}
            encoding = "utf-8"

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield b'{"status":"ok"}'

            def close(self):
                self.closed = True

        response = Response()
        with (
            patch.object(roles, "get_list_setting", return_value={"tracking.example.test"}),
            patch.object(
                roles,
                "get_int_setting",
                side_effect=lambda _field, default, minimum=1: default,
            ),
            patch.object(
                transfer_manifest_api.socket,
                "getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
            ),
            patch("requests.get", return_value=response) as request,
        ):
            payload = transfer_manifest_api._fetch_partner_tracking_payload(
                courier, "TRACK / 1"
            )
        self.assertEqual(payload, {"status": "ok"})
        self.assertTrue(response.closed)
        _args, kwargs = request.call_args
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertIn("TRACK%20%2F%201", request.call_args.args[0])

    def test_courier_status_updates_cannot_replay_backwards(self):
        self.assertTrue(
            transfer_manifest_api._can_apply_courier_status("Assigned", "In Transit")
        )
        self.assertTrue(
            transfer_manifest_api._can_apply_courier_status("In Transit", "Delivered")
        )
        self.assertFalse(
            transfer_manifest_api._can_apply_courier_status("Delivered", "In Transit")
        )
        self.assertFalse(
            transfer_manifest_api._can_apply_courier_status("Closed", "Delivered")
        )

    def test_trip_rebundling_denies_before_creating_documents(self):
        with (
            patch.object(
                logistics_api,
                "_require_ops",
                side_effect=frappe.PermissionError("denied"),
            ),
            patch.object(frappe, "new_doc") as new_doc,
        ):
            with self.assertRaises(frappe.PermissionError):
                logistics_api.club_transfers_into_trip("WAREHOUSE-A")
        new_doc.assert_not_called()

    def test_two_opt_keeps_stops_and_never_increases_distance(self):
        stops = [SimpleNamespace(name=f"S{i}") for i in range(6)]
        coords = {
            "S0": (0, 4), "S1": (4, 0), "S2": (1, 3),
            "S3": (3, 1), "S4": (2, 2), "S5": (5, 5),
        }
        before = optimizer._route_len(stops, coords, (0, 0))
        ordered, after = optimizer._two_opt(stops, coords, (0, 0), max_passes=10)

        self.assertEqual({row.name for row in ordered}, {row.name for row in stops})
        self.assertLessEqual(after, before)

    def test_trip_eta_bounds_route_stops_before_processing(self):
        source = inspect.getsource(optimizer.compute_trip_eta)
        self.assertIn("optimizer_max_stops", source)
        self.assertIn("len(doc.stops or []) > stop_limit", source)
        self.assertIn("limit_page_length=stop_limit", source)

    def test_manifest_creation_denies_before_loading_stock_entries(self):
        with (
            patch.object(
                roles,
                "require",
                side_effect=frappe.PermissionError("denied"),
            ),
            patch.object(frappe, "get_doc") as get_doc,
        ):
            with self.assertRaises(frappe.PermissionError):
                transfer_manifest_api.create_manifest(["STE-1"])
        get_doc.assert_not_called()

    def test_manifest_creation_derives_company_from_trusted_stock_entries(self):
        stock_entry = _FakeDocument(
            name="STE-1",
            company="COMPANY-A",
            from_warehouse="WAREHOUSE-A",
            to_warehouse="WAREHOUSE-B",
            docstatus=1,
        )

        class Manifest(_FakeDocument):
            name = "MANIFEST-1"

            def append(self, fieldname, value):
                self.setdefault(fieldname, []).append(value)

            def insert(self):
                self["inserted"] = True

        manifest = Manifest()
        with (
            patch.object(roles, "require"),
            patch.object(frappe, "has_permission"),
            patch.object(frappe, "get_doc", return_value=stock_entry),
            patch.object(scope_guard, "assert_scope"),
            patch.object(transfer_manifest_api, "_resolve_manifest_store", return_value=None),
            patch.object(frappe, "new_doc", return_value=manifest),
        ):
            name = transfer_manifest_api.create_manifest(["STE-1"])

        self.assertEqual(name, "MANIFEST-1")
        self.assertEqual(manifest.company, "COMPANY-A")
        self.assertEqual(manifest.source_warehouse, "WAREHOUSE-A")
        self.assertEqual(manifest.destination_warehouse, "WAREHOUSE-B")
        self.assertEqual(manifest.transfers, [{"stock_entry": "STE-1"}])
        self.assertTrue(manifest.inserted)

    def test_control_tower_aggregates_apply_empty_location_scope(self):
        empty_visible_scope = {
            "bypass": False,
            "stores": {"__NO_VISIBLE_STORE__"},
            "warehouses": {"__NO_VISIBLE_WAREHOUSE__"},
            "companies": set(),
        }
        with (
            patch.object(logistics_api, "_require_ops"),
            patch.object(scope_guard, "_scope", return_value=empty_visible_scope),
        ):
            board = logistics_api.ops_board()
            lifecycle = logistics_api.ops_lifecycle_counts()

        self.assertEqual(sum(board["totals"].values()), 0)
        self.assertTrue(all(stage["count"] == 0 for stage in lifecycle["stages"]))

    def test_digest_preview_denies_out_of_scope_company_before_querying(self):
        with (
            patch.object(roles, "require"),
            patch.object(
                scope_guard,
                "assert_scope",
                side_effect=frappe.PermissionError("outside scope"),
            ),
            patch.object(digest, "_metrics") as metrics,
        ):
            with self.assertRaises(frappe.PermissionError):
                digest.preview_digest("COMPANY-B")
        metrics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
