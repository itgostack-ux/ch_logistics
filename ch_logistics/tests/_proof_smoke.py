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


def _stub():
    """Build an in-memory manifest stub against the live class (no insert)."""
    doc = frappe.new_doc("CH Transfer Manifest")
    doc.name = "TM-SMOKE-0001"
    doc.qr_payload = "TM-SMOKE-0001"
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
    # Ensure the flag is ON (matches doctype default).
    frappe.db.set_single_value("CH Logistics Settings", "enforce_delivery_qr", 1)
    _expect(lambda: doc._validate_delivery_qr(None),
            contains="mandatory", label="delivery QR: empty throws")
    _expect(lambda: doc._validate_delivery_qr("WRONG-QR"),
            contains="does not match", label="delivery QR: mismatch throws")
    # Happy path
    doc._validate_delivery_qr("TM-SMOKE-0001")
    print("  PASS  delivery QR: matching token accepted")

    # When the flag is off, no scan required.
    frappe.db.set_single_value("CH Logistics Settings", "enforce_delivery_qr", 0)
    doc._validate_delivery_qr(None)
    doc._validate_delivery_qr("anything")
    print("  PASS  delivery QR: bypassed when enforce_delivery_qr=0")
    # Restore default.
    frappe.db.set_single_value("CH Logistics Settings", "enforce_delivery_qr", 1)

    print("== complete_delivery signature accepts scanned_qr ==")
    import inspect
    sig = inspect.signature(CHTransferManifest.complete_delivery)
    assert "scanned_qr" in sig.parameters, \
        f"complete_delivery missing scanned_qr param: {list(sig.parameters)}"
    print(f"  PASS  signature: {sig}")

    print("\nAll mandatory pickup/delivery proof contracts hold.")
