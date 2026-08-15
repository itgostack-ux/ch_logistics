"""Contract tests for stop <-> manifest role resolution.

Locks the scenario that shipped broken: a trip built from a static CH Route
template carrying a forward leg (hub -> spoke) AND a return leg (spoke -> hub),
plus a template stop that serves nothing.

Before ``ch_logistics.api.stop_roles`` existed, the server branched on
``stop_type == "Pickup"`` and let ``Pickup+Drop`` fall into a drop-only branch.
On the trip below that meant:

  * the combined hub stop returned only the manifest being DELIVERED there,
    never the one waiting to be collected, and
  * the spoke stop that the template mistyped as ``Drop`` — but which is the
    return leg's ORIGIN — returned nothing at all, so the driver could not
    pick up.

The driver app still drew both manifests on those cards (it resolved them via a
tolerant location fallback), so the screen and the server disagreed about what
the driver could do.

Run with:
    bench --site erpnext.local execute \\
        ch_logistics.tests.test_stop_role_resolution.run

Pure in-memory: builds plain dicts against the predicate, touches no DB rows.
"""
from __future__ import annotations

import frappe

from ch_logistics.api import stop_roles


HUB = "Chennai - Hub - BM"
SPOKE_A = "GG-ALWARTHIRUNAGAR-Sellable - BM"
SPOKE_B = "GG-DOVETON-Sellable - BM"
SPOKE_C = "GG-KOLATHUR-Sellable - BM"


def _trip():
    """The screenshot's trip: 1 forward leg, 1 return leg, 1 dead stop."""
    stops = [
        # Types as the route TEMPLATE declared them — deliberately wrong for
        # stop 4, which is the return leg's origin.
        {"sequence": 1, "warehouse": HUB, "store": None, "stop_type": "Pickup+Drop"},
        {"sequence": 2, "warehouse": SPOKE_A, "store": None, "stop_type": "Drop"},
        {"sequence": 3, "warehouse": SPOKE_B, "store": None, "stop_type": "Drop"},
        {"sequence": 4, "warehouse": SPOKE_C, "store": None, "stop_type": "Drop"},
    ]
    manifests = [
        {"name": "TM-FWD", "source_store": None, "source_warehouse": HUB,
         "destination_store": None, "destination_warehouse": SPOKE_A},
        {"name": "TM-RET", "source_store": None, "source_warehouse": SPOKE_C,
         "destination_store": None, "destination_warehouse": HUB},
    ]
    return stops, manifests


def _check(label, got, want, failures):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          got={got!r}\n         want={want!r}")
        failures.append(label)


def run():
    failures: list[str] = []
    stops, manifests = _trip()

    print("== roles per stop ==")
    expected = {
        1: "Pickup+Drop",   # collect TM-FWD, deliver TM-RET
        2: "Drop",          # deliver TM-FWD
        3: None,            # serves nothing
        4: "Pickup",        # collect TM-RET — template said Drop
    }
    for stop in stops:
        got = stop_roles.derive_stop_type(stop, manifests)
        _check(f"stop #{stop['sequence']} derives {expected[stop['sequence']]!r}",
               got, expected[stop["sequence"]], failures)

    print("== membership (which manifests the driver handles where) ==")
    want_members = {1: {"TM-FWD", "TM-RET"}, 2: {"TM-FWD"}, 3: set(), 4: {"TM-RET"}}
    for stop in stops:
        got = {m["name"] for m in manifests if stop_roles.serves(m, stop)}
        _check(f"stop #{stop['sequence']} serves {sorted(want_members[stop['sequence']])}",
               got, want_members[stop["sequence"]], failures)

    print("== the combined stop separates its two legs ==")
    hub = stops[0]
    _check("TM-FWD is a PICKUP at the hub",
           stop_roles.manifest_roles_at_stop(manifests[0], hub), {stop_roles.PICKUP}, failures)
    _check("TM-RET is a DROP at the hub",
           stop_roles.manifest_roles_at_stop(manifests[1], hub), {stop_roles.DROP}, failures)

    print("== annotation the driver app consumes ==")
    stop_roles.annotate(stops, manifests)
    _check("TM-FWD stop_roles", manifests[0]["stop_roles"], {"1": "Pickup", "2": "Drop"}, failures)
    _check("TM-RET stop_roles", manifests[1]["stop_roles"], {"1": "Drop", "4": "Pickup"}, failures)
    _check("TM-FWD pickup@1 drop@2",
           (manifests[0]["pickup_stop_sequence"], manifests[0]["drop_stop_sequence"]), (1, 2), failures)
    _check("TM-RET pickup@4 drop@1",
           (manifests[1]["pickup_stop_sequence"], manifests[1]["drop_stop_sequence"]), (4, 1), failures)

    print("== round trip: one location that is both origin and destination ==")
    same = {"name": "TM-LOOP", "source_store": None, "source_warehouse": HUB,
            "destination_store": None, "destination_warehouse": HUB}
    _check("same-location manifest yields BOTH roles",
           stop_roles.manifest_roles_at_stop(same, stops[0]),
           {stop_roles.PICKUP, stop_roles.DROP}, failures)

    print("== store/warehouse asymmetry still matches ==")
    _check("combine_roles(empty) is None", stop_roles.combine_roles(set()), None, failures)
    _check("roles_of round-trips Pickup+Drop",
           stop_roles.roles_of("pickup+drop"), {stop_roles.PICKUP, stop_roles.DROP}, failures)

    if failures:
        raise AssertionError(f"{len(failures)} stop-role contract(s) broken: {failures}")
    print("\nAll stop-role contracts hold.")
    return {"ok": True, "checks": 15}
