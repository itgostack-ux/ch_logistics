"""Canonical stop <-> manifest role resolution.

Single source of truth for the question "what does the driver do with this
manifest at this stop?". Before this module the rule was reimplemented four
times — twice server-side (``_stop_manifest_rows``,
``_annotate_manifest_stop_links``) and twice in the driver app
(``_gather_stop_manifests``, ``manifests_by_stop``) — and the copies disagreed
about ``Pickup+Drop``. The server treated a combined stop as drop-only, so a
manifest the driver could see on a stop card could not actually be picked up.

Model follows Oracle OTM / SAP TM: a shipment contributes TWO stop events, a
pickup at its origin and a delivery at its destination. A stop's type is the
UNION of the event roles occurring at that location, derived from the shipments
actually assigned — never copied from a route template and trusted. That makes
the rule direction-agnostic: reverse legs, milk runs and combined hub stops all
fall out of the same predicate instead of needing special cases.
"""
from __future__ import annotations

import frappe

PICKUP = "Pickup"
DROP = "Drop"
PICKUP_DROP = "Pickup+Drop"

#: Every value ``CH Logistics Trip Stop.stop_type`` may hold.
STOP_TYPES = (DROP, PICKUP, PICKUP_DROP)


def combine_roles(roles) -> str | None:
    """Collapse a set of event roles into a stop_type, or None when empty.

    An empty result is meaningful: it marks a stop that serves no shipment,
    which is a planning defect rather than a legitimate stop type.
    """
    roles = {r for r in (roles or ()) if r}
    if PICKUP in roles and DROP in roles:
        return PICKUP_DROP
    if PICKUP in roles:
        return PICKUP
    if DROP in roles:
        return DROP
    return None


def roles_of(stop_type) -> set[str]:
    """Expand a stop_type back into its event roles."""
    value = (stop_type or "").strip().casefold()
    if value == PICKUP_DROP.casefold():
        return {PICKUP, DROP}
    if value == PICKUP.casefold():
        return {PICKUP}
    if value == DROP.casefold():
        return {DROP}
    return set()


def warehouse_by_store(stops, manifests) -> dict:
    """One lookup of CH Store -> Warehouse for every store on either side.

    Stops and manifests are not guaranteed to reference a location the same
    way: a stop may carry only a store while the manifest carries only that
    store's warehouse. Resolving both through this map keeps the comparison
    symmetric instead of silently failing to match.
    """
    names = set()
    for s in stops or ():
        if _get(s, "store"):
            names.add(_get(s, "store"))
    for m in manifests or ():
        for key in ("source_store", "destination_store"):
            if _get(m, key):
                names.add(_get(m, key))
    if not names or not frappe.db.exists("DocType", "CH Store"):
        return {}
    return {
        r.name: r.warehouse
        for r in frappe.get_all(
            "CH Store", filters={"name": ["in", list(names)]}, fields=["name", "warehouse"]
        )
        if r.warehouse
    }


def location_matches(stop, store, warehouse, wh_by_store=None) -> bool:
    """True when (store, warehouse) denotes the same physical place as ``stop``."""
    wh_by_store = wh_by_store or {}
    s_store, s_wh = _get(stop, "store"), _get(stop, "warehouse")
    if store and s_store and store == s_store:
        return True
    if warehouse and s_wh and warehouse == s_wh:
        return True
    if warehouse and s_store and wh_by_store.get(s_store) == warehouse:
        return True
    if store and s_wh and wh_by_store.get(store) == s_wh:
        return True
    return False


def manifest_roles_at_stop(manifest, stop, wh_by_store=None) -> set[str]:
    """The event roles a manifest contributes at a stop.

    Purely locational, deliberately: a manifest is picked up where it starts
    and dropped where it ends, whatever the trip or manifest is labelled. A
    stop that is both origin and destination (a round trip) correctly yields
    both roles rather than one arbitrarily winning.
    """
    roles = set()
    if location_matches(stop, _get(manifest, "source_store"), _get(manifest, "source_warehouse"), wh_by_store):
        roles.add(PICKUP)
    if location_matches(stop, _get(manifest, "destination_store"), _get(manifest, "destination_warehouse"), wh_by_store):
        roles.add(DROP)
    return roles


def serves(manifest, stop, wh_by_store=None) -> bool:
    """True when the driver has anything to do with this manifest here."""
    return bool(manifest_roles_at_stop(manifest, stop, wh_by_store))


def derive_stop_type(stop, manifests, wh_by_store=None) -> str | None:
    """The stop_type the attached shipments actually imply for this stop."""
    roles: set[str] = set()
    for m in manifests or ():
        roles |= manifest_roles_at_stop(m, stop, wh_by_store)
    return combine_roles(roles)


def annotate(stops, manifests) -> None:
    """Stamp pickup/drop sequences and per-stop roles onto each manifest dict.

    Mutates ``manifests`` in place, adding ``pickup_stop_sequence``,
    ``drop_stop_sequence`` and ``stop_roles`` ({sequence: stop_type}). The
    driver app consumes ``stop_roles`` instead of re-deriving membership on the
    client, so the two can no longer drift apart.
    """
    if not stops or not manifests:
        return
    wh_by_store = warehouse_by_store(stops, manifests)
    for m in manifests:
        pickup_seq = drop_seq = None
        stop_roles = {}
        for stop in stops:
            roles = manifest_roles_at_stop(m, stop, wh_by_store)
            if not roles:
                continue
            seq = _get(stop, "sequence")
            stop_roles[str(seq)] = combine_roles(roles)
            if PICKUP in roles and pickup_seq is None:
                pickup_seq = seq
            if DROP in roles and drop_seq is None:
                drop_seq = seq
        _set(m, "pickup_stop_sequence", pickup_seq)
        # stop_sequence is the legacy single-stop pointer; keep it as a
        # fallback so trips planned before role derivation still resolve.
        _set(m, "drop_stop_sequence", drop_seq if drop_seq is not None else _get(m, "stop_sequence"))
        _set(m, "stop_roles", stop_roles)


def _get(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _set(obj, key, value):
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)
