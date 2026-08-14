"""End-to-end driver-side test for the bundle QR cascade.

Covers the path the delivery app drives when the operator hits
"Arrive & Pick Up" / "Arrive & Deliver" on a stop that has a
consolidated pickup_token / delivery_token:

    1. start_stop_pickup(scanned_qr=stop.pickup_token)
       -> cascades start_pickup on every manifest at the stop,
          flipping each Assigned -> In Transit and stamping a
          per-manifest pickup photo + GPS.

    2. request_stop_otp(trip, sequence, lat, lng)
       -> calls mark_reached_destination per manifest, mints ONE
          shared 6-digit OTP, and writes it onto every manifest's
          delivery_otp so the driver only has to type one code.

    3. complete_stop_delivery(scanned_qr=stop.delivery_token, otp=shared)
       -> cascades complete_delivery on every manifest, flipping
          each In Transit -> Delivered. Verifies the per-manifest
          arrival ping, OTP, photo, receiver_name + GPS are all
          recorded for downstream compliance reporting.

Also asserts get_trip_detail exposes ``has_pickup_token`` and
``has_delivery_token`` booleans on the stop dict (the only signal
the driver UI needs to switch from per-manifest to bundle flow —
the actual token strings stay server-side).

Run with:
    bench --site erpnext.local execute \\
        ch_logistics.tests.test_bundle_qr_driver_flow.run

Self-cleans on every run; safe to re-run.
"""
from __future__ import annotations

import frappe
from frappe.utils import nowdate

from ch_logistics.api import logistics_api as api


_TAG = "BUNDLE-DRIVER-E2E"


def _company():
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name")
    if not company:
        raise RuntimeError("No Company exists; create one before running this test.")
    return company


def _ensure_warehouse(name, abbr):
    full = f"{name} - {abbr}"
    if frappe.db.exists("Warehouse", full):
        return full
    wh = frappe.new_doc("Warehouse")
    wh.warehouse_name = name
    wh.company = _company()
    wh.is_group = 0
    wh.insert(ignore_permissions=True)
    return wh.name


def _store_location():
    """Resolve a (city, zone) pair for this test's company.

    ``CH Store._validate_operational_location`` requires Company + City + Zone
    on any Active store, and it runs *before*
    ``validate_store_location_contract`` — which is what would otherwise derive
    city from zone. So the fixture has to supply both explicitly.
    """
    company = _company()
    zone = frappe.db.get_value(
        "CH Store Zone", {"company": company}, ["name", "city"], as_dict=True
    ) or frappe.db.get_value("CH Store Zone", {}, ["name", "city"], as_dict=True)
    if not zone:
        raise RuntimeError("No CH Store Zone exists; seed store zones before running this test.")
    city = zone.city or frappe.db.get_value("CH City", {}, "name")
    if not city:
        raise RuntimeError("No CH City exists; seed cities before running this test.")
    return city, zone.name


def _ensure_store(name, warehouse):
    # CH Store autonames by store code — look up by store_name.
    existing = frappe.db.get_value("CH Store", {"store_name": name, "company": _company()})
    if existing:
        return existing
    city, zone = _store_location()
    s = frappe.new_doc("CH Store")
    s.store_name = name
    s.company = _company()
    s.warehouse = warehouse
    s.city = city
    s.zone = zone
    s.insert(ignore_permissions=True)
    return s.name


def _ensure_stub_stock_entry(source_wh):
    """The CH Transfer Manifest 'transfers' table is reqd:1 — every
    manifest must reference at least one Stock Entry. For this test we
    don't care which one (we're testing the cascade routing, not the
    receiving-side SE sync), so we hunt for any existing submitted SE
    in the database and reuse it across every manifest. This mirrors
    what the dispatcher pickup flow does.
    """
    se = frappe.db.get_value(
        "Stock Entry", {"docstatus": 1, "stock_entry_type": "Material Transfer"}, "name")
    if not se:
        # No SE on this site yet — fall back to any non-cancelled one.
        se = frappe.db.get_value(
            "Stock Entry", {"docstatus": ["<", 2], "stock_entry_type": "Material Transfer"}, "name")
    if not se:
        raise RuntimeError(
            "No Stock Entry exists on this site; this E2E expects at least one "
            "Stock Entry to anchor manifest.transfers rows. Run a Material "
            "Receipt or Material Transfer first."
        )
    return se


def _make_manifest(source_wh, dest_wh, dest_store, idx, stub_se):
    m = frappe.new_doc("CH Transfer Manifest")
    m.company = _company()
    m.posting_date = nowdate()
    m.source_warehouse = source_wh
    m.destination_warehouse = dest_wh
    m.destination_store = dest_store
    m.status = "Packed"
    # ≥22 chars + != name: passes the secure-QR-token guard the same way
    # production minting (frappe.generate_hash on validate) does, while the
    # _TAG prefix keeps the LIKE-based teardown working.
    m.qr_payload = f"{_TAG}-" + frappe.generate_hash(length=24)
    # Anchor the reqd transfers table so subsequent self.save() calls
    # inside start_pickup / complete_delivery pass the mandatory check.
    m.append("transfers", {"stock_entry": stub_se})
    m.flags.ignore_validate = True
    m.flags.ignore_mandatory = True
    m.insert(ignore_permissions=True)
    return m.name


def _teardown():
    manifests = frappe.get_all(
        "CH Transfer Manifest",
        filters={"qr_payload": ["like", f"{_TAG}-%"]},
        pluck="name",
    )
    if manifests and frappe.db.exists("DocType", "CH Logistics OTP Log"):
        for log_name in frappe.get_all(
            "CH Logistics OTP Log",
            filters={"manifest": ["in", manifests]},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "CH Logistics OTP Log", log_name, force=1,
                    ignore_permissions=True, delete_permanently=True,
                )
            except Exception:
                pass
    for m in manifests:
        try:
            frappe.delete_doc("CH Transfer Manifest", m, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    for t in frappe.get_all("CH Logistics Trip",
                            filters={"hub_warehouse": ["like", f"%{_TAG}-Hub%"]},
                            pluck="name"):
        try:
            frappe.delete_doc("CH Logistics Trip", t, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    for s in frappe.get_all("CH Store",
                            filters={"store_name": ["like", f"{_TAG}-%"]},
                            pluck="name"):
        try:
            frappe.delete_doc("CH Store", s, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    for w in frappe.get_all("Warehouse",
                            filters={"warehouse_name": ["like", f"{_TAG}-%"]},
                            pluck="name"):
        try:
            frappe.delete_doc("Warehouse", w, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    _sweep_store_tree_warehouses()
    frappe.db.commit()


def _sweep_store_tree_warehouses():
    """Delete the warehouse tree CH Store.on_update builds for the fixtures.

    restructure_store_tree renames the sellable leaf and creates the store's
    bin tree (Buyback / Damaged / Demo plus the group) under a canonical
    GG-<STORECODE>-N name. None of those match ``{_TAG}-%``, so the sweep above
    misses them and every run leaks a fresh set. Match on the store-code
    fragment instead, and delete leaves before the groups they hang off.
    """
    code = _TAG.replace("-", "")
    rows = frappe.get_all("Warehouse", filters={"name": ["like", f"%{code}%"]},
                          fields=["name", "is_group"])
    for w in sorted(rows, key=lambda r: r.is_group):
        try:
            frappe.delete_doc("Warehouse", w.name, force=1,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass


def _expect(condition, label):
    if condition:
        print(f"  PASS  {label}")
        return
    print(f"  FAIL  {label}")
    raise AssertionError(label)


# Bangalore (Forum mall) — used as the driver's stand-in GPS for every
# pickup / arrival / delivery in this test.
_BLR_LAT = 12.9342
_BLR_LNG = 77.6101
_PHOTO   = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="


def run():
    print("== building fixtures ==")
    frappe.flags.in_test = True
    # The OTP flow tries to send mail to the destination warehouse +
    # store contacts. In a dev / test bench those addresses are usually
    # @example.com (RFC 2606 reserved) which a real SMTP server bounces.
    # Mute outbound mail for the duration of the test — the OTP value
    # itself is still written to every manifest, which is what we assert.
    frappe.flags.mute_emails = True
    _teardown()
    try:
        abbr = frappe.get_cached_value("Company", _company(), "abbr")
        src  = _ensure_warehouse(f"{_TAG}-Hub", abbr)
        dwh  = _ensure_warehouse(f"{_TAG}-S1",  abbr)
        st   = _ensure_store(f"{_TAG}-Store1", dwh)
        # CH Store.on_update restructures the store tree and renames the
        # sellable leaf to its canonical path, so `dwh` is stale the moment
        # the store exists. Re-read it from the store.
        dwh  = frappe.db.get_value("CH Store", st, "warehouse") or dwh

        # 3 same-source / same-destination Packed manifests — the
        # exact shape the bundle button now enforces.
        stub_se = _ensure_stub_stock_entry(src)
        m1 = _make_manifest(src, dwh, st, 1, stub_se)
        m2 = _make_manifest(src, dwh, st, 2, stub_se)
        m3 = _make_manifest(src, dwh, st, 3, stub_se)
        manifests = [m1, m2, m3]
        print(f"  packed manifests ready: {manifests} (transfers anchor SE={stub_se})")

        # ── Step 1 : Dispatcher bundles them ──────────────────────
        print("== bundle into trip ==")
        res = api.club_transfers_into_trip(
            source_warehouse=src,
            manifests=manifests,
            trip_date=nowdate(),
            company=_company(),
            enforce_single_destination=1,
        )
        trip = res["trip"]
        stop_seq = res["stops"][0]["sequence"]
        _expect(len(res["stops"]) == 1, "single consolidated stop created")

        # ── Step 2 : Trip detail exposes bundle-token flags ──────
        # The delivery app uses these to decide whether to show the
        # one-scan dialog vs. the per-manifest dialog. The actual
        # tokens are NEVER on the wire — only their presence.
        print("== get_trip_detail exposes has_pickup_token / has_delivery_token ==")
        detail = api.get_trip_detail(trip=trip)
        stop_view = next((s for s in detail["stops"] if int(s["sequence"]) == int(stop_seq)), None)
        _expect(stop_view is not None, "trip detail returns the stop")
        _expect(stop_view.get("has_pickup_token") is True,
                f"stop.has_pickup_token == True (got {stop_view.get('has_pickup_token')})")
        _expect(stop_view.get("has_delivery_token") is True,
                f"stop.has_delivery_token == True (got {stop_view.get('has_delivery_token')})")
        _expect("pickup_token" not in stop_view,
                "raw pickup_token is NOT returned in trip detail (security)")
        _expect("delivery_token" not in stop_view,
                "raw delivery_token is NOT returned in trip detail (security)")

        # Read the actual tokens directly from the trip doc — this is
        # what the printed QR encodes; the driver scans it from the
        # paper label, not from the API payload.
        trip_doc = frappe.get_doc("CH Logistics Trip", trip)
        stop = next(s for s in trip_doc.stops if int(s.sequence) == int(stop_seq))
        pickup_token   = stop.pickup_token
        delivery_token = stop.delivery_token
        _expect(bool(pickup_token) and bool(delivery_token),
                f"both tokens present on stop (pickup={pickup_token[:6]}…, drop={delivery_token[:6]}…)")

        # ── Step 3 : Dispatcher assigns the trip to the driver ───
        # The real workflow goes through assign_driver + assign_load.
        # We stub it by flipping each manifest to Assigned so
        # start_pickup is legal.
        for mname in manifests:
            frappe.db.set_value("CH Transfer Manifest", mname, "status", "Assigned")
        frappe.db.commit()

        # ── Step 4 : Negative — wrong pickup QR is refused ───────
        print("== start_stop_pickup: wrong QR refused ==")
        try:
            api.start_stop_pickup(
                trip=trip, sequence=stop_seq,
                scanned_qr="some-garbage-token",
                pickup_photo=_PHOTO, lat=_BLR_LAT, lng=_BLR_LNG,
            )
            _expect(False, "wrong pickup QR should have been rejected")
        except frappe.ValidationError as exc:
            _expect("not match" in str(exc).lower() or "wrong label" in str(exc).lower(),
                    f"server rejected bogus pickup token ({exc})")

        # Sanity: all manifests should still be Assigned.
        for mname in manifests:
            st_now = frappe.db.get_value("CH Transfer Manifest", mname, "status")
            _expect(st_now == "Assigned",
                    f"{mname} stayed Assigned after refusal (got {st_now})")

        # ── Step 5 : Happy path — one scan picks up ALL manifests ─
        print("== start_stop_pickup: cascades to every manifest ==")
        pick_res = api.start_stop_pickup(
            trip=trip, sequence=stop_seq,
            scanned_qr=pickup_token,
            pickup_photo=_PHOTO,
            lat=_BLR_LAT, lng=_BLR_LNG,
            notes="bundle driver e2e",
        )

        _expect(set(pick_res.get("started") or []) == set(manifests),
                f"all 3 manifests advanced via stop scan (got {pick_res.get('started')})")
        _expect(not pick_res.get("skipped"),
                f"no manifests skipped at pickup (got {pick_res.get('skipped')})")

        # Status now In Transit (start_pickup jumps Assigned -> In Transit).
        for mname in manifests:
            st_now = frappe.db.get_value("CH Transfer Manifest", mname, "status")
            _expect(st_now == "In Transit",
                    f"{mname} now In Transit (got {st_now})")
        # Stop should be Arrived.
        trip_doc.reload()
        stop_after_pickup = next(s for s in trip_doc.stops if int(s.sequence) == int(stop_seq))
        _expect(stop_after_pickup.status == "Arrived",
                f"stop.status == Arrived after pickup cascade (got {stop_after_pickup.status})")
        _expect(bool(stop_after_pickup.pickup_scanned_at),
                "stop.pickup_scanned_at stamped")

        # ── Step 6 : One shared OTP for the whole drop ───────────
        print("== request_stop_otp: mints one shared OTP across all manifests ==")
        otp_info = api.request_stop_otp(trip=trip, sequence=stop_seq,
                                        lat=_BLR_LAT, lng=_BLR_LNG)
        _expect(otp_info.get("manifest_count") == 3,
                f"manifest_count == 3 (got {otp_info.get('manifest_count')})")

        # Pull every manifest's delivery_otp and assert they're identical
        # and non-blank. This is the linchpin: the driver types ONE code.
        # OTPs are hashed at rest (hmac-sha256$...) — assert digest form,
        # then broadcast a KNOWN otp through the same digest helper so the
        # happy-path verify below exercises the real hmac compare.
        from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
            delivery_otp_digest,
        )
        otps = {mname: frappe.db.get_value("CH Transfer Manifest", mname, "delivery_otp")
                for mname in manifests}
        stored = otps[m1]
        _expect(bool(stored) and str(stored).startswith("hmac-sha256$"),
                f"stored OTP is an hmac digest, not plaintext (got {stored!r})")
        _expect(all(v == stored for v in otps.values()),
                f"every manifest carries the SAME delivery_otp ({otps})")
        otp_logs = {
            mname: frappe.db.get_value(
                "CH Transfer Manifest", mname, "delivery_otp_log"
            )
            for mname in manifests
        }
        _expect(all(otp_logs.values()) and len(set(otp_logs.values())) == 3,
                f"every manifest has its own OTP audit row ({otp_logs})")
        for mname, log_name in otp_logs.items():
            audit = frappe.db.get_value(
                "CH Logistics OTP Log",
                log_name,
                ["manifest", "status", "request_source", "otp_digest", "generated_at", "expires_at"],
                as_dict=True,
            )
            _expect(audit.manifest == mname and audit.status == "Pending",
                    f"{mname} pending OTP audit is linked")
            _expect(audit.request_source == "Consolidated Stop",
                    f"{mname} audit records consolidated-stop origin")
            _expect(audit.otp_digest == stored and audit.generated_at and audit.expires_at,
                    f"{mname} audit retains digest + generation/expiry")
        shared = "123456"
        known_digest = delivery_otp_digest(shared)
        for mname in manifests:
            frappe.db.set_value("CH Transfer Manifest", mname, "delivery_otp",
                                known_digest, update_modified=False)
            frappe.db.set_value(
                "CH Logistics OTP Log",
                otp_logs[mname],
                "otp_digest",
                known_digest,
                update_modified=False,
            )
        frappe.db.commit()

        # Each manifest's arrival_datetime must now be set so
        # complete_delivery's gate is satisfied.
        for mname in manifests:
            arr = frappe.db.get_value("CH Transfer Manifest", mname, "arrival_datetime")
            _expect(bool(arr), f"{mname} has arrival_datetime stamped")

        # ── Step 7 : Negative — wrong OTP refused ─────────────────
        # Contract (hardened): the stop cascade is ALL-OR-NOTHING. A wrong
        # OTP returns a structured refusal so the request can commit the
        # failed-attempt audit without delivering any manifest.
        print("== complete_stop_delivery: wrong OTP refused ==")
        wrong_result = api.complete_stop_delivery(
            trip=trip, sequence=stop_seq,
            scanned_qr=delivery_token,
            delivery_photo=_PHOTO,
            receiver_name="Store Manager",
            otp="000000",
            lat=_BLR_LAT, lng=_BLR_LNG,
        )
        _expect(wrong_result.get("ok") is False and not wrong_result.get("delivered"),
                f"wrong OTP rejected atomically ({wrong_result})")
        for log_name in otp_logs.values():
            _expect(
                frappe.db.get_value("CH Logistics OTP Log", log_name, "attempts") == 1,
                f"failed stop OTP attempt persisted on {log_name}",
            )

        # Sanity: still In Transit.
        for mname in manifests:
            st_now = frappe.db.get_value("CH Transfer Manifest", mname, "status")
            _expect(st_now == "In Transit",
                    f"{mname} stayed In Transit after wrong OTP (got {st_now})")

        # ── Step 8 : Negative — wrong delivery QR refused ─────────
        print("== complete_stop_delivery: wrong drop QR refused ==")
        try:
            api.complete_stop_delivery(
                trip=trip, sequence=stop_seq,
                scanned_qr="bogus-drop-token",
                delivery_photo=_PHOTO,
                receiver_name="Store Manager",
                otp=shared,
                lat=_BLR_LAT, lng=_BLR_LNG,
            )
            _expect(False, "wrong drop QR should have been rejected")
        except frappe.ValidationError as exc:
            _expect("not match" in str(exc).lower() or "wrong label" in str(exc).lower(),
                    f"server rejected bogus drop token ({exc})")

        # ── Step 9 : Happy path — one scan delivers ALL manifests ─
        print("== complete_stop_delivery: cascades to every manifest ==")
        drop_res = api.complete_stop_delivery(
            trip=trip, sequence=stop_seq,
            scanned_qr=delivery_token,
            delivery_photo=_PHOTO,
            receiver_name="Store Manager",
            otp=shared,
            lat=_BLR_LAT, lng=_BLR_LNG,
            notes="bundle drop e2e",
        )
        _expect(set(drop_res.get("delivered") or []) == set(manifests),
                f"all 3 manifests delivered via stop scan (got {drop_res.get('delivered')})")
        _expect(not drop_res.get("skipped"),
                f"no manifests skipped at delivery (got {drop_res.get('skipped')})")

        # Final assertions: every manifest is Delivered with proof on file.
        for mname in manifests:
            row = frappe.db.get_value(
                "CH Transfer Manifest", mname,
                ["status", "delivery_photo", "receiver_name", "delivery_otp_verified",
                 "delivery_datetime", "delivery_lat", "delivery_lng", "delivery_otp",
                 "delivery_otp_log", "delivery_otp_verified_at", "delivery_otp_verified_by"],
                as_dict=True,
            )
            _expect(row.status == "Delivered",
                    f"{mname} status == Delivered")
            _expect(bool(row.delivery_photo) and row.receiver_name == "Store Manager",
                    f"{mname} has delivery photo + receiver_name")
            _expect(int(row.delivery_otp_verified or 0) == 1,
                    f"{mname} delivery_otp_verified == 1")
            _expect(not row.delivery_otp and row.delivery_otp_log == otp_logs[mname],
                    f"{mname} clears one-time digest but retains audit link")
            _expect(bool(row.delivery_otp_verified_at) and bool(row.delivery_otp_verified_by),
                    f"{mname} verifier timestamp + user recorded")
            _expect(bool(row.delivery_datetime),
                    f"{mname} delivery_datetime stamped")
            _expect(row.delivery_lat == _BLR_LAT and row.delivery_lng == _BLR_LNG,
                    f"{mname} delivery GPS recorded (lat={row.delivery_lat}, lng={row.delivery_lng})")
            audit = frappe.db.get_value(
                "CH Logistics OTP Log",
                otp_logs[mname],
                ["status", "attempts", "verified_at", "verified_by", "otp_digest"],
                as_dict=True,
            )
            _expect(audit.status == "Verified" and audit.attempts == 2,
                    f"{mname} OTP audit is Verified with both attempts recorded")
            _expect(bool(audit.verified_at) and bool(audit.verified_by),
                    f"{mname} OTP audit keeps confirmation identity/time")
            _expect(str(audit.otp_digest).startswith("hmac-sha256$"),
                    f"{mname} audit retains only the HMAC digest")

        # Stop bookkeeping: status -> Completed, ata stamped, delivery_scanned_at stamped.
        trip_doc.reload()
        final_stop = next(s for s in trip_doc.stops if int(s.sequence) == int(stop_seq))
        _expect(final_stop.status == "Completed",
                f"stop.status == Completed after delivery cascade (got {final_stop.status})")
        _expect(bool(final_stop.delivery_scanned_at),
                "stop.delivery_scanned_at stamped")
        _expect(bool(final_stop.ata),
                "stop.ata stamped")

        print("\nALL BUNDLE-DRIVER FLOW ASSERTIONS PASSED")
        return {
            "ok": True,
            "trip": trip,
            "stop": stop_seq,
            "shared_otp": shared,
            "delivered": list(manifests),
        }
    finally:
        _teardown()
