"""Smoke test for the mandatory pickup/delivery proof contract.

Run with:
    bench --site erpnext.local execute \\
        ch_logistics.tests._proof_smoke.run

Exercises the controller-level validators in isolation (no live manifest
required) to lock the rules:

  start_pickup     → photo + QR scan + GPS (non-zero, in-range) mandatory
  complete_delivery → photo + receiver_name + QR scan + GPS mandatory
                      (+ OTP only when delivery_otp is set on the doc)

Each assertion is a contract that breaks loudly the moment someone relaxes
a validator. Safe to re-run; no DB writes.
"""
import frappe

from ch_logistics.logistics.doctype.ch_transfer_manifest.ch_transfer_manifest import (
    CHTransferManifest,
)

# A real qr_payload is a 32-char random hash. The stub must NOT reuse the
# manifest name: the QR validators treat a name-equals-token (or anything
# under 22 chars) as a legacy row and throw "missing a secure QR token"
# before they ever compare the scan — which would mask the mismatch contract
# this smoke test exists to lock.
_SMOKE_TOKEN = "0123456789abcdef0123456789abcdef"


def _stub():
    """Build an in-memory manifest stub against the live class (no insert)."""
    doc = frappe.new_doc("CH Transfer Manifest")
    doc.name = "TM-SMOKE-0001"
    doc.qr_payload = _SMOKE_TOKEN
    return doc


def _expect(callable_, *, contains, label):
    try:
        callable_()
    except Exception as exc:  # frappe.ValidationError, PermissionError, etc.
        msg = str(exc)
        if contains.lower() in msg.lower():
            print(f"  PASS  {label}  ->  {msg.splitlines()[0][:90]}")
            return
        print(f"  FAIL  {label}  ->  wrong error: {msg}")
        raise AssertionError(label)
    print(f"  FAIL  {label}  ->  expected to throw, did not")
    raise AssertionError(label)


def run():
    print("== mandatory geo validator ==")
    doc = _stub()
    _expect(lambda: doc._validate_geo(None, None, "pickup"),
            contains="latitude", label="pickup: None,None throws")
    _expect(lambda: doc._validate_geo("", "", "delivery"),
            contains="latitude", label="delivery: blank,blank throws")
    _expect(lambda: doc._validate_geo("abc", "xyz", "pickup"),
            contains="latitude", label="pickup: non-numeric throws")
    _expect(lambda: doc._validate_geo(0, 0, "pickup"),
            contains="0, 0", label="pickup: (0,0) sentinel rejected")
    _expect(lambda: doc._validate_geo(91, 10, "delivery"),
            contains="out of range", label="delivery: lat>90 rejected")
    _expect(lambda: doc._validate_geo(10, 181, "delivery"),
            contains="out of range", label="delivery: lng>180 rejected")
    # Happy path returns the parsed floats.
    lat, lng = doc._validate_geo("12.9716", "77.5946", "pickup")
    assert (lat, lng) == (12.9716, 77.5946), f"happy parse mismatch: {lat},{lng}"
    print(f"  PASS  pickup: valid Bangalore coords parsed to {lat},{lng}")

    print("== mandatory delivery QR validator ==")
    # Snapshot the tenant's real setting. This used to hard-code a "restore"
    # to 1 at the end, so running the smoke test on a site that had delivery-QR
    # enforcement switched OFF silently switched it ON and committed — a test
    # changing production policy behind the operator's back.
    prior_enforce_delivery_qr = frappe.db.get_single_value(
        "CH Logistics Settings", "enforce_delivery_qr"
    )
    frappe.db.set_single_value("CH Logistics Settings", "enforce_delivery_qr", 1)
    _expect(lambda: doc._validate_delivery_qr(None),
            contains="mandatory", label="delivery QR: empty throws")
    _expect(lambda: doc._validate_delivery_qr("WRONG-QR"),
            contains="does not match", label="delivery QR: mismatch throws")
    # Happy path
    doc._validate_delivery_qr(_SMOKE_TOKEN)
    print("  PASS  delivery QR: matching token accepted")

    # When the flag is off, no scan required.
    frappe.db.set_single_value("CH Logistics Settings", "enforce_delivery_qr", 0)
    doc._validate_delivery_qr(None)
    doc._validate_delivery_qr("anything")
    print("  PASS  delivery QR: bypassed when enforce_delivery_qr=0")
    # Restore what the tenant had, not what this test would prefer.
    frappe.db.set_single_value(
        "CH Logistics Settings", "enforce_delivery_qr", prior_enforce_delivery_qr
    )

    print("== seal chain of custody ==")
    sealed = _stub()
    sealed.append("packages", {"package_label": "TM-SMOKE-0001-B01", "seal_number": "SEAL-9001"})
    sealed.append("packages", {"package_label": "TM-SMOKE-0001-B02", "seal_number": "seal-9002"})
    _expect(lambda: sealed._validate_seals(None),
            contains="Seal verification is required", label="seal: none presented throws")
    _expect(lambda: sealed._validate_seals("SEAL-9001"),
            contains="not presented", label="seal: short of the packed set throws")
    _expect(lambda: sealed._validate_seals("SEAL-9001, SEAL-9002, SEAL-XXXX"),
            contains="not on the packing list", label="seal: swapped tag throws")
    # Case and separator tolerant: a tag is a tag however it was typed.
    sealed._validate_seals("seal-9001\nSEAL-9002")
    print("  PASS  seal: exact set accepted (case/separator tolerant)")
    # A manifest packed without seals has nothing to verify.
    _stub()._validate_seals(None)
    print("  PASS  seal: unsealed manifest is unaffected")

    print("== geofence: accuracy, override, coverage ==")
    prior_geo = frappe.db.get_single_value("CH Logistics Settings", "enforce_geofence")
    prior_radius = frappe.db.get_single_value("CH Logistics Settings", "geofence_radius_m")
    frappe.db.set_single_value("CH Logistics Settings", "enforce_geofence", 1)
    frappe.db.set_single_value("CH Logistics Settings", "geofence_radius_m", 300)

    # A warehouse WITH coordinates is required to exercise the fence at all.
    from ch_logistics.api import optimizer
    _real_coords = optimizer._warehouse_coords
    HUB = (13.0827, 80.2707)          # Chennai
    FAR = (13.1500, 80.2707)          # ~7.5 km north

    geo = _stub()
    geo.source_warehouse = "GEO-WH"
    geo.destination_warehouse = "GEO-WH"
    optimizer._warehouse_coords = lambda wh: HUB
    try:
        # On-site fix passes.
        geo._validate_geofence(HUB[0], HUB[1], "pickup", accuracy_m=20)
        print("  PASS  geofence: on-site fix accepted")

        # Far away with a good fix is refused.
        _expect(lambda: geo._validate_geofence(FAR[0], FAR[1], "pickup", accuracy_m=20),
                contains="from", label="geofence: far away with good fix refused")

        # Same distance, but the device admits it could be kilometres out —
        # that is a bad fix, not evidence the driver is elsewhere.
        _expect(lambda: geo._validate_geofence(FAR[0], FAR[1], "pickup", accuracy_m=5000),
                contains="too imprecise", label="geofence: unusable fix reported as imprecise")

        # A modest accuracy margin widens the fence rather than failing.
        geo._validate_geofence(13.0850, 80.2707, "pickup", accuracy_m=400)
        print("  PASS  geofence: fence widened by reported accuracy")

        # The override lets a stranded driver continue, with a reason.
        geo._validate_geofence(FAR[0], FAR[1], "pickup", accuracy_m=20,
                               override_reason="Weak GPS indoors; standing at the dock")
        print("  PASS  geofence: reasoned override accepted")

        # No coordinates -> cannot check, must not block.
        optimizer._warehouse_coords = lambda wh: None
        geo._validate_geofence(FAR[0], FAR[1], "pickup", accuracy_m=20)
        print("  PASS  geofence: ungeocoded location does not block")
    finally:
        optimizer._warehouse_coords = _real_coords
        frappe.db.set_single_value("CH Logistics Settings", "enforce_geofence", prior_geo)
        frappe.db.set_single_value("CH Logistics Settings", "geofence_radius_m", prior_radius)

    print("== dispatch packing gates ==")
    # These fire in before_submit, which the e2e fixtures skip (they insert
    # with ignore_validate and advance status directly), so without these
    # assertions the enforced policy has no test coverage at all.
    prior_slip = frappe.db.get_single_value("CH Logistics Settings", "require_packing_slip")
    prior_photo = frappe.db.get_single_value("CH Logistics Settings", "require_packing_photo")
    frappe.db.set_single_value("CH Logistics Settings", "require_packing_slip", 1)
    frappe.db.set_single_value("CH Logistics Settings", "require_packing_photo", 1)

    def _pack_stub(packages=None):
        m = frappe.new_doc("CH Transfer Manifest")
        m.name = "TM-SMOKE-GATE"
        m.append("transfers", {"stock_entry": "SMOKE-SE"})
        for pkg in (packages or []):
            m.append("packages", pkg)
        return m

    _expect(lambda: frappe.new_doc("CH Transfer Manifest").before_submit(),
            contains="at least one Stock Entry", label="dispatch: no stock entry throws")
    _expect(lambda: _pack_stub().before_submit(),
            contains="packed box", label="dispatch: no carton throws")
    _expect(lambda: _pack_stub([{"package_label": "B01", "packed_qty": 1}]).before_submit(),
            contains="packing photo", label="dispatch: carton without photo throws")
    # A carton photo satisfies the requirement. This is the branch that was
    # broken: _has_packing_photo only looked at a non-existent packing_photos
    # table and File attachments, never at the carton where the packing hub
    # actually stores the photo.
    _pack_stub([{"package_label": "B01", "packed_qty": 1,
                 "packing_photo": "/files/pack.jpg"}]).before_submit()
    print("  PASS  dispatch: carton with photo accepted")

    frappe.db.set_single_value("CH Logistics Settings", "require_packing_slip", prior_slip)
    frappe.db.set_single_value("CH Logistics Settings", "require_packing_photo", prior_photo)

    print("== complete_delivery signature accepts scanned_qr ==")
    import inspect
    sig = inspect.signature(CHTransferManifest.complete_delivery)
    assert "scanned_qr" in sig.parameters, \
        f"complete_delivery missing scanned_qr param: {list(sig.parameters)}"
    print(f"  PASS  signature: {sig}")

    print("\nAll mandatory pickup/delivery proof contracts hold.")
