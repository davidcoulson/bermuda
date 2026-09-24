"""
Public, stable read API for other integrations.

Bermuda already holds every device/scanner advertisement in memory, whether or
not the corresponding `distance_to` entities are enabled. Consumers that want
those per-scanner readings have historically had to enable the entities and
scrape `sensor.<device>_distance_to_<scanner>` out of the state machine, which
is expensive at scale: on a busy install that is thousands of entities, each
one writing to the recorder and fanning `state_changed` out to every websocket
client.

This module exposes the same data directly, in-process, without creating a
single entity:

    from custom_components.bermuda.api import async_get_advert_snapshot

    snapshot = async_get_advert_snapshot(hass)

For push updates, Bermuda's coordinator is a normal `DataUpdateCoordinator`, so
callers can subscribe with `async_get_coordinator(hass).async_add_listener(cb)`
and take a fresh snapshot when it fires. No extra signal is needed.

The snapshot is deliberately plain JSON-able primitives rather than Bermuda's
internal objects, so consumers do not bind to internal class layout (which does
change). `SNAPSHOT_VERSION` is bumped if the shape changes incompatibly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import callback
from homeassistant.util import slugify

from .const import (
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    ADDR_TYPE_FINDMY,
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    ADDR_TYPE_TILE,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONFDATA_FINDMY,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import BermudaDataUpdateCoordinator

# Bumped only on an incompatible change to the snapshot shape. Consumers should
# check this and degrade gracefully rather than assume.
SNAPSHOT_VERSION = 1


def _advert_rank(advert) -> tuple[bool, float]:
    """Which of several adverts from one scanner a snapshot should report."""
    return (getattr(advert, "rssi_distance", None) is not None, getattr(advert, "stamp", None) or 0.0)

# Additive capabilities layered on SNAPSHOT_VERSION 1 without changing any
# existing key. A consumer that wants one of these should feature-detect it
# here rather than parse versions, and fall back to the v1 behaviour when the
# name is absent (an older Bermuda build).
#
#   tracked_only     async_get_advert_snapshot(..., tracked_only=True)
#   tracked_devices  async_get_tracked_devices()
#   scanners         async_get_scanners(), and a top-level "scanners" map in the snapshot
#   rssi_history     async_get_advert_snapshot(..., include_history=True) adds a
#                    per-advert "history" of recent (rssi, stamp) pairs, and every
#                    advert carries the path-loss parameters Bermuda used
#                    ("ref_power", "attenuation", "rssi_offset")
#   rssi_offsets     async_get_rssi_offsets() / async_set_rssi_offsets(): read and
#                    write Bermuda's per-scanner rssi offsets, applied live and
#                    persisted without reloading the entry
SNAPSHOT_FEATURES = frozenset(
    {
        "tile_identity",
        "tracked_only",
        "tracked_devices",
        "scanners",
        "rssi_history",
        "rssi_offsets",
        "scanner_ranging",
        "device_management",
    }
)


@callback
def _entry_and_coordinator(hass: HomeAssistant) -> tuple[Any, BermudaDataUpdateCoordinator] | tuple[None, None]:
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime_data = getattr(entry, "runtime_data", None)
        coordinator = getattr(runtime_data, "coordinator", None)
        if coordinator is not None:
            return entry, coordinator
    return None, None


@callback
def async_get_coordinator(hass: HomeAssistant) -> BermudaDataUpdateCoordinator | None:
    """
    Return Bermuda's coordinator, or None if Bermuda is not set up.

    Callers can use the returned coordinator's `async_add_listener()` to be
    notified after each update cycle, then call `async_get_advert_snapshot()`.
    """
    return _entry_and_coordinator(hass)[1]


@callback
def async_get_rssi_offsets(hass: HomeAssistant) -> dict[str, Any] | None:
    """
    Bermuda's per-scanner rssi offsets and the global path-loss parameters.

    Shape::

        {
          "offsets": {"<scanner address>": <dB>, ...},   # lower-cased addresses
          "attenuation": float,   # global option
          "ref_power": float,     # global option (per-device overrides not included)
        }

    A consumer converting its own per-receiver distance correction ``c`` into
    an offset should use ``delta_dB = -10 * attenuation * log10(c)`` and add
    it to the scanner's current offset. Returns None if Bermuda is not set up.
    """
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None
    options = coordinator.options
    return {
        "offsets": {str(k).lower(): float(v) for k, v in (options.get(CONF_RSSI_OFFSETS) or {}).items()},
        "attenuation": options.get(CONF_ATTENUATION),
        "ref_power": options.get(CONF_REF_POWER),
    }


@callback
def async_set_rssi_offsets(
    hass: HomeAssistant,
    offsets: dict[str, float],
    *,
    merge: bool = True,
    persist: bool = True,
) -> dict[str, float] | None:
    """
    Set per-scanner rssi offsets (dB), live, and persist them.

    The offsets are applied to the running coordinator immediately (every
    existing advert from a changed scanner is recomputed), then written to
    the config entry so they survive a restart - WITHOUT reloading the entry,
    which would otherwise discard every advert history for a change already
    in effect. ``merge`` False replaces the whole map; scanners left out
    revert to 0. ``persist`` False keeps the change in memory only.

    Returns the resulting full map, or None if Bermuda is not set up.
    """
    entry, coordinator = _entry_and_coordinator(hass)
    if coordinator is None:
        return None
    applied = coordinator.async_apply_rssi_offsets(offsets, merge=merge)
    if persist and entry is not None:
        new_options = {**dict(entry.options), CONF_RSSI_OFFSETS: dict(applied)}
        if new_options != dict(entry.options):
            # Announce the change to the reload listener before the entry
            # update fires it (async_update_entry schedules listeners as
            # tasks, so this ordering is safe).
            coordinator.inline_options = new_options
            hass.config_entries.async_update_entry(entry, options=new_options)
    return applied


def _slug_memo() -> Any:
    """
    A per-call slugify memo.

    Many adverts across many devices share the same handful of physical
    scanners, so slugify(scanner_name) is called far more often than there are
    distinct names. Memoize per call (not globally: a scanner rename mid-run
    should be picked up by the next snapshot, not held forever).
    """
    slug_cache: dict[str, str] = {}

    def _cached_slug(name: str) -> str:
        slug = slug_cache.get(name)
        if slug is None:
            slug = slug_cache[name] = slugify(name)
        return slug

    return _cached_slug


def _scanner_entries(coordinator: Any, nowstamp: float, cached_slug: Any) -> dict[str, Any]:
    """Build the per-scanner liveness map from the coordinator's scanner set."""
    scanners: dict[str, Any] = {}
    # get_scanners is a property returning the live set of scanner devices;
    # tolerate a coordinator (or test double) that does not carry it.
    for scanner in getattr(coordinator, "get_scanners", None) or ():
        address = getattr(scanner, "address", None)
        if not address:
            continue
        name = getattr(scanner, "name", None) or ""
        last_seen = getattr(scanner, "last_seen", None) or None
        scanners[address] = {
            "name": name,
            "slug": cached_slug(name) if name else "",
            "address": address,
            "unique_id": getattr(scanner, "unique_id", None),
            "address_wifi_mac": getattr(scanner, "address_wifi_mac", None),
            "area_id": getattr(scanner, "area_id", None),
            "area_name": getattr(scanner, "area_name", None),
            "is_remote": getattr(scanner, "is_remote_scanner", None),
            # Monotonic stamp of the newest advert this scanner has relayed,
            # for ANY device - i.e. "is this proxy alive", independent of
            # whether any tracked device is in range of it.
            "last_seen": last_seen,
            "last_seen_age": (nowstamp - last_seen) if last_seen else None,
        }
    return scanners


@callback
def async_get_scanners(hass: HomeAssistant) -> dict[str, Any] | None:
    """
    Return every scanner Bermuda currently knows, keyed by address, with liveness.

    This is the cheap answer to "which of my proxies are alive?": one small
    dict built from the scanner set, with no advert walk and no JSON dump.
    Consumers previously had to call the `bermuda.dump_devices` service (which
    serialises every scanner's whole advert table) to read `last_seen` per
    scanner.

    Shape::

        {
          "<scanner address>": {
            "name": str,
            "slug": str,                 # best-effort, see snapshot caveat
            "address": str,
            "unique_id": str | None,
            "address_wifi_mac": str | None,
            "area_id": str | None,
            "area_name": str | None,
            "is_remote": bool | None,    # ESPHome/Shelly proxy vs local HCI
            "last_seen": float | None,   # monotonic, newest advert relayed
            "last_seen_age": float | None,  # seconds since last_seen
          },
          ...
        }

    Returns None if Bermuda is not set up.
    """
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None
    return _scanner_entries(coordinator, monotonic_time_coarse(), _slug_memo())


@callback
def async_get_scanner_ranging(hass: HomeAssistant, max_age: float | None = None) -> dict[str, Any] | None:
    """
    Return how every scanner hears every OTHER scanner's own advertisement.

    Proxies that advertise (an ESPHome iBeacon, a Shelly) are devices in the
    coordinator like any other, so their adverts as heard by their siblings
    are already measured - a labelled range at a known position, refreshed
    for free. A consumer can turn those into reference fingerprints or into a
    receiver calibration without taking a full snapshot (which serialises
    every device in range) or calling the dump_devices service.

    Shape::

        {
          "version": 1,
          "stamp": <monotonic seconds>,
          "scanners": {
            "<tx scanner address>": {
              "<rx scanner address>": {
                "distance": float | None,   # Bermuda's filtered estimate (m)
                "distance_raw": float | None,
                "rssi": int | None,
                "age": float | None,        # seconds since rx last heard tx
              }, ...
            }, ...
          }
        }

    ``max_age`` drops pairs not heard within that many seconds. Scanners that
    hear nobody, or that nobody hears, still appear with an empty map.
    Returns None if Bermuda is not set up.
    """
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None
    nowstamp = monotonic_time_coarse()
    scanners: dict[str, Any] = {}
    for scanner in getattr(coordinator, "get_scanners", None) or ():
        address = getattr(scanner, "address", None)
        if not address:
            continue
        heard_by: dict[str, Any] = {}
        for advert in (getattr(scanner, "adverts", None) or {}).values():
            rx = getattr(advert, "scanner_address", None)
            if not rx or rx == address:
                continue
            stamp = getattr(advert, "stamp", None) or None
            age = (nowstamp - stamp) if stamp else None
            if max_age is not None and (age is None or age > max_age):
                continue
            heard_by[rx] = {
                "distance": getattr(advert, "rssi_distance", None),
                "distance_raw": getattr(advert, "rssi_distance_raw", None),
                "rssi": getattr(advert, "rssi", None),
                "age": age,
            }
        scanners[address] = heard_by
    return {"version": SNAPSHOT_VERSION, "stamp": nowstamp, "scanners": scanners}


# --- device management (the parts of the options flow a UI wants) ----------------
#
# Bermuda's options flow is the only way to pick devices to track, add FindMy
# accessories or change the global options. These give another integration
# (or a service call) the same abilities: list what could be tracked, change
# the tracked set, manage FindMy keys, read and write the global options.
# Every write goes through hass.config_entries.async_update_entry so it is
# persisted and reloaded exactly as the flow would.

CANDIDATE_MAX_AGE = 2 * 3600.0  # like the flow: a random MAC unseen this long is useless
MANAGED_OPTIONS = frozenset(
    {
        "ref_power",
        "attenuation",
        "max_area_radius",
        "max_velocity",
        "devtracker_nothome_timeout",
        "update_interval",
        "smoothing_samples",
        "create_scanner_entities",
        "track_categories",
        "exclude_devices",
        "tile_identity_probes",
    }
)


# Apple's manufacturer-specific advert is a run of (type, length, payload)
# records; the types say what kind of device is talking without any key.
APPLE_COMPANY_ID = 0x004C
APPLE_ADV_TYPES = {
    0x02: "ibeacon",
    0x05: "airdrop",
    0x07: "proximity_pairing",   # AirPods, Beats, other accessories in their case
    0x09: "airplay_target",
    0x0A: "airplay_source",
    0x0B: "magic_switch",        # Apple Watch
    0x0C: "handoff",             # iPhone / iPad / Mac
    0x0D: "tethering_target",
    0x0E: "tethering_source",
    0x0F: "nearby_action",
    0x10: "nearby_info",         # iPhone / iPad / Mac / Watch presence
    0x12: "findmy",              # offline-finding (AirTag and FindMy-network tags)
}


def apple_advert_kinds(manufacturer_data) -> list[str]:
    """The Apple advert record types in a manufacturer_data mapping (company 0x004C), by name."""
    if not isinstance(manufacturer_data, dict):
        return []
    payload = manufacturer_data.get(APPLE_COMPANY_ID)
    if not payload:
        return []
    kinds, i, data = [], 0, bytes(payload)
    while i + 1 < len(data):
        kind, length = data[i], data[i + 1]
        name = APPLE_ADV_TYPES.get(kind, f"type_{kind:02x}")
        if name not in kinds:
            kinds.append(name)
        i += 2 + length
    return kinds


def apple_summary(kinds: list[str], address_type: str | None) -> str | None:
    """One phrase for the heard list: what this Apple device most likely is."""
    if "findmy" in kinds:
        return "Find My tag (needs its pairing keys)"
    if "proximity_pairing" in kinds:
        return "AirPods / Beats or another accessory"
    if any(k in kinds for k in ("nearby_info", "handoff", "nearby_action", "airdrop", "tethering_source")):
        rotates = address_type == BDADDR_TYPE_RANDOM_RESOLVABLE
        return "iPhone / iPad / Mac / Watch" + (" (rotating address: track it as a Private BLE Device with its IRK)" if rotates else "")
    if "magic_switch" in kinds:
        return "Apple Watch"
    if any(k in kinds for k in ("airplay_target", "airplay_source")):
        return "AirPlay device (HomePod, Apple TV)"
    if kinds:
        return "Apple device"
    return None



@callback
def async_get_device_candidates(hass: HomeAssistant, max_age: float = CANDIDATE_MAX_AGE) -> list[dict] | None:
    """
    Every device Bermuda hears that could be tracked but is not yet, newest first.

    Mirrors the options flow's device picker: scanners, Private BLE devices
    (tracked automatically), FindMy accessories (their own list) and Tile
    source addresses already bound to a Tile are left out, as is anything
    unseen for ``max_age`` seconds. Each row carries ``config_value`` - the
    string to pass to ``async_set_tracked_devices`` - which is the address
    for ordinary devices and the metadevice id for a Tile (so it keeps being
    tracked across address rotations).

    Returns None if Bermuda is not set up.
    """
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None
    nowstamp = monotonic_time_coarse()
    tile_manager = getattr(coordinator, "tile_manager", None)
    tile_bound = tile_manager.bound_sources() if tile_manager is not None else set()
    rows: list[dict] = []
    for address, device in coordinator.devices.items():
        if getattr(device, "is_scanner", False) or getattr(device, "create_sensor", False):
            continue
        address_type = getattr(device, "address_type", None)
        if address_type in (ADDR_TYPE_PRIVATE_BLE_DEVICE, ADDR_TYPE_FINDMY):
            continue
        last_seen = getattr(device, "last_seen", None) or 0
        if not last_seen or nowstamp - last_seen > max_age:
            continue
        is_tile = bool(getattr(device, "is_tile", False))
        if is_tile and address in tile_bound:
            continue
        if address_type == ADDR_TYPE_TILE:
            kind, config_value = "tile", address.upper()
        elif is_tile:
            from .bermuda_tile import tile_metadevice_id  # noqa: PLC0415

            kind, config_value = "tile", tile_metadevice_id(address).upper()
        elif address_type == ADDR_TYPE_IBEACON:
            kind, config_value = "ibeacon", address.upper()
        else:
            kind, config_value = "device", address.upper()
        adverts = getattr(device, "adverts", None) or {}
        fresh = [a for a in adverts.values() if getattr(a, "stamp", None) and nowstamp - a.stamp <= 60]
        best_rssi = max((a.rssi for a in fresh if getattr(a, "rssi", None) is not None), default=None)
        # Which scanners heard it in the last minute, so a consumer that only
        # cares about its own placed proxies can drop what a stray one hears.
        scanner_addresses = sorted({str(getattr(a, "scanner_address", "")).lower() for a in fresh if getattr(a, "scanner_address", None)})
        latest_mfr = next((a.manufacturer_data[0] for a in fresh if getattr(a, "manufacturer_data", None)), None)
        apple_kinds = apple_advert_kinds(latest_mfr)
        heard_by = sorted(
            (
                {"address": str(a.scanner_address).lower(), "rssi": a.rssi}
                for a in fresh if getattr(a, "scanner_address", None) and getattr(a, "rssi", None) is not None
            ),
            key=lambda h: -h["rssi"],
        )
        rows.append(
            {
                "address": address,
                "config_value": config_value,
                "kind": kind,
                "name": getattr(device, "name", None) or address,
                "manufacturer": getattr(device, "manufacturer", None),
                # What kind of thing it is when the SIG lists could not say,
                # and whether that kind rotates its address (so a consumer can
                # explain why something it can name still cannot be followed).
                "family": getattr(device, "device_family", None),
                "family_rotates": bool(getattr(device, "family_rotates", False)),
                "address_type": address_type,
                "area_name": getattr(device, "area_name", None),
                "last_seen_age": nowstamp - last_seen,
                "first_seen_age": (nowstamp - device.first_seen) if getattr(device, "first_seen", None) else None,
                "scanner_addresses": scanner_addresses,
                "heard_by": heard_by,   # loudest first
                "apple_kinds": apple_kinds,
                "apple_summary": apple_summary(apple_kinds, address_type),
                "scanners": len(fresh),
                "best_rssi": best_rssi,
            }
        )
    rows.sort(key=lambda r: r["last_seen_age"])
    return rows


async def async_set_tracked_devices(hass: HomeAssistant, add=(), remove=()) -> list[str] | None:
    """
    Add and/or remove tracked devices; returns the new configured list.

    Values are what ``async_get_device_candidates`` reports as ``config_value``
    (an address, or a Tile metadevice id). Persisted to the config entry's
    options, which reloads Bermuda the same way the options flow does.
    Returns None if Bermuda is not set up.
    """
    entry, coordinator = _entry_and_coordinator(hass)
    if entry is None or coordinator is None:
        return None
    current = [str(a).upper() for a in entry.options.get(CONF_DEVICES, [])]
    remove_set = {str(a).upper() for a in remove}
    new = [a for a in current if a not in remove_set]
    for a in add:
        a = str(a).upper()
        if a and a not in new:
            new.append(a)
    if new != current:
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_DEVICES: new})
    return new


@callback
def async_get_findmy_accessories(hass: HomeAssistant) -> list[dict] | None:
    """The configured FindMy accessories with their current binding state, or None."""
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None
    manager = getattr(coordinator, "findmy_manager", None)
    if manager is None:
        return []
    nowstamp = monotonic_time_coarse()
    rows = []
    for acc in manager.accessories.values():
        metadevice = coordinator.devices.get(acc.address)
        sources = getattr(metadevice, "metadevice_sources", None) or []
        last_seen = getattr(metadevice, "last_seen", None) or None
        rows.append(
            {
                "address": acc.address,
                "name": acc.friendly_name,
                "model": getattr(acc, "model", None),
                "alignment_index": getattr(acc, "alignment_index", None),
                "current_source": sources[0] if sources else None,
                "last_seen_age": (nowstamp - last_seen) if last_seen else None,
            }
        )
    return rows


async def async_add_findmy_accessory(hass: HomeAssistant, accessory_json: str, name: str | None = None) -> dict | None:
    """Add a FindMy accessory from its exported key JSON, as the options flow does.

    Raises FindMyKeyError on bad input. Returns None if Bermuda is not set up.
    """
    from .bermuda_findmy import FindMyAccessoryKeys  # noqa: PLC0415

    entry, coordinator = _entry_and_coordinator(hass)
    if entry is None or coordinator is None:
        return None
    accessory = FindMyAccessoryKeys.from_json(accessory_json)
    if name and name.strip():
        accessory.name = name.strip()
    coordinator.findmy_manager.add_accessory(accessory)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONFDATA_FINDMY: coordinator.findmy_manager.dump()})
    return {"address": accessory.address, "name": accessory.friendly_name}


async def async_remove_findmy_accessory(hass: HomeAssistant, address: str) -> bool | None:
    """Remove a FindMy accessory by its metadevice address. None if Bermuda is not set up."""
    entry, coordinator = _entry_and_coordinator(hass)
    if entry is None or coordinator is None:
        return None
    removed = coordinator.findmy_manager.remove_accessory(address)
    if removed:
        save = getattr(coordinator, "async_save_findmy_alignment", None)
        if save is not None:
            await save()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONFDATA_FINDMY: coordinator.findmy_manager.dump()}
        )
    return bool(removed)


# What a value must look like to be written. The options flow coerces the
# same way; a raw string or a zero written straight into the entry made the
# reload's coordinator init raise, on this start and every one after it.
_POSITIVE_NUMBER = vol.All(vol.Coerce(float), vol.Range(min=0, min_included=False))
_MANAGED_OPTION_SCHEMA = vol.Schema(
    {
        vol.Optional("ref_power"): vol.Coerce(float),
        vol.Optional("attenuation"): _POSITIVE_NUMBER,
        vol.Optional("max_area_radius"): _POSITIVE_NUMBER,
        vol.Optional("max_velocity"): _POSITIVE_NUMBER,
        vol.Optional("devtracker_nothome_timeout"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("update_interval"): _POSITIVE_NUMBER,
        vol.Optional("smoothing_samples"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("create_scanner_entities"): vol.Coerce(bool),
        vol.Optional("tile_identity_probes"): vol.Coerce(bool),
        vol.Optional("track_categories"): [str],
        vol.Optional("exclude_devices"): [str],
    }
)


@callback
def async_get_options(hass: HomeAssistant) -> dict | None:
    """
    The global options a UI may edit (see MANAGED_OPTIONS), defaults included.

    The entry only holds what the user has set, so a fresh install answered
    {} and a UI could not show the values in force; the coordinator's
    options carry the defaults.
    """
    entry, coordinator = _entry_and_coordinator(hass)
    if entry is None:
        return None
    current = {**(getattr(coordinator, "options", None) or {}), **entry.options}
    return {k: v for k, v in current.items() if k in MANAGED_OPTIONS}


async def async_set_options(hass: HomeAssistant, changes: dict) -> dict | None:
    """
    Change global options. Only MANAGED_OPTIONS keys are accepted (ValueError
    otherwise); the entry is updated and Bermuda reloads as after the flow.
    Returns the new managed options, or None if Bermuda is not set up.
    """
    entry, _coordinator = _entry_and_coordinator(hass)
    if entry is None:
        return None
    unknown = sorted(k for k in changes if k not in MANAGED_OPTIONS)
    if unknown:
        raise ValueError(f"not a managed option: {', '.join(unknown)}")
    try:
        changes = _MANAGED_OPTION_SCHEMA(dict(changes))
    except vol.Invalid as err:
        msg = f"invalid option value: {err}"
        raise ValueError(msg) from err
    new_options = {**entry.options, **changes}
    if new_options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=new_options)
    return {k: v for k, v in new_options.items() if k in MANAGED_OPTIONS}


@callback
def async_get_tracked_devices(hass: HomeAssistant) -> dict[str, Any] | None:
    """
    Return the devices Bermuda is configured to track, keyed by address.

    A consumer that only needs to know WHICH devices are tracked (to notice a
    device being added or removed, say) should call this rather than take a
    full snapshot: it reads one attribute per known device and builds nothing
    for the untracked majority, whereas the snapshot serialises every advert
    of every device in range before a consumer can filter.

    Shape::

        {"<device address>": {"name": str, "slug": str, "unique_id": str | None, "last_seen_age": float | None}, ...}

    Returns None if Bermuda is not set up.
    """
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None
    cached_slug = _slug_memo()
    nowstamp = monotonic_time_coarse()
    tracked: dict[str, Any] = {}
    for address, device in coordinator.devices.items():
        if not getattr(device, "create_sensor", False):
            continue
        last_seen = getattr(device, "last_seen", None)
        tracked[address] = {
            "name": device.name,
            "slug": cached_slug(device.name),
            "unique_id": getattr(device, "unique_id", None),
            "last_seen_age": (nowstamp - last_seen) if last_seen else None,
        }
    return tracked


@callback
def async_get_advert_snapshot(
    hass: HomeAssistant,
    addresses: set[str] | None = None,
    *,
    include_empty: bool = False,
    tracked_only: bool = False,
    include_history: bool = False,
) -> dict[str, Any] | None:
    """
    Return a point-in-time snapshot of every device/scanner advertisement.

    `addresses` optionally limits the result to those device addresses
    (lower-cased MAC, or an iBeacon/IRK metadevice address). `include_empty`
    keeps devices that currently have no adverts at all. `tracked_only` limits
    the result to devices the user has configured Bermuda to track (the
    `tracked` flag below), skipping the walk over every other device in range;
    a consumer that only ever reads tracked devices should always pass it, as
    that is usually the difference between a dozen devices and several hundred.

    `include_history` adds each advert's recent raw samples as
    ``"history": [[rssi, stamp], ...]`` (newest first, at most HIST_KEEP_COUNT
    entries). Bermuda's own ``distance`` is a running-minimum-biased average
    designed for "which scanner is nearest"; a consumer fitting a position
    from several scanners at once may prefer its own, symmetric estimator over
    the raw samples, and needs the same path-loss parameters Bermuda would
    apply - so every advert also carries ``ref_power`` (the effective value,
    per-device override or global), ``attenuation`` and the per-scanner
    ``rssi_offset``. Bermuda's own conversion is
    ``10 ** ((ref_power - (rssi + rssi_offset)) / (10 * attenuation))``.

    Returns None if Bermuda is not set up.

    Shape::

        {
          "version": 1,
          "stamp": <monotonic seconds>,
          "scanners": { <scanner address>: {...}, ... },  # see async_get_scanners()
          "devices": {
            "<device address>": {
              "name": str,
              "slug": str,          # matches Bermuda's entity naming
              "tracked": bool,      # user has Bermuda tracking this device
              "address_type": str,
              "area_id": str | None,
              "area_name": str | None,
              "scanners": {
                "<scanner address>": {
                  "name": str,
                  "slug": str,      # best-effort; see caveat below
                  "address": str,   # stable join key
                  "area_id": str | None,
                  "area_name": str | None,
                  "distance": float | None,   # smoothed, METRES
                  "distance_raw": float | None,
                  "rssi": float | None,
                  "stamp": float,   # monotonic, when this advert was received
                  "age": float,     # seconds since that advert
                },
                ...
              },
            },
            ...
          },
        }

    Notes for consumers:

    - `distance` is metres, always. Bermuda's *entities* may render feet
      depending on the user's unit settings; this API does not.
    - `distance` is Bermuda's smoothed value and is None once the reading has
      timed out, which is the "scanner can no longer hear this device" signal.
    - `age` is seconds since this scanner last actually heard the device. That
      is strictly better than an entity's `last_updated` for spotting a stuck
      reading, because it does not depend on the value having changed.
    - Key scanners by their ADDRESS (the dict key / `address` field), not by
      `slug`. Bermuda's `_distance_to_<slug>` entity_ids are frozen at entity
      creation and do not follow later scanner renames, so a slug derived from
      the current name can differ from the historical entity_id. A consumer
      migrating off entity scraping should resolve its stored slugs to scanner
      addresses once via the entity registry (disabled entities are still
      registered, and Bermuda's unique_id embeds the scanner MAC).
    """
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None

    nowstamp = monotonic_time_coarse()
    wanted = {addr.lower() for addr in addresses} if addresses is not None else None
    _cached_slug = _slug_memo()

    devices: dict[str, Any] = {}
    for address, device in coordinator.devices.items():
        if wanted is not None and address.lower() not in wanted:
            continue
        # Cheapest test first: one attribute read drops the untracked majority
        # before any of the per-advert work below.
        if tracked_only and not getattr(device, "create_sensor", False):
            continue
        # Defensive: this walks shared coordinator state that other code owns,
        # and a public API must not be the thing that raises.
        device_adverts = getattr(device, "adverts", None) or {}
        if not device_adverts and not include_empty:
            continue

        scanners: dict[str, Any] = {}
        for advert in device_adverts.values():
            # A device that rotates its address - a phone, a watch, a Find My
            # tag, a Tile - reaches here as a metadevice holding the adverts of
            # EVERY address it has used, so one scanner can appear several
            # times: once live, and once per old address that scanner heard
            # before the rotation, long since timed out. This used to keep
            # whichever came last in the dict, which is insertion history, not
            # freshness - a dead advert could shadow the live one, and the
            # consumer saw a scanner that "cannot hear" a device it was hearing
            # every second. Keep the one with a distance, then the newest.
            kept = scanners.get(advert.scanner_address)
            if kept is not None and _advert_rank(advert) <= (kept["distance"] is not None, kept["stamp"] or 0.0):
                continue
            scanner_device = coordinator.devices.get(advert.scanner_address) or advert.scanner_device
            # The scanner DEVICE's name is authoritative; advert.name is a copy
            # taken when the advert was created and goes stale if the scanner is
            # renamed. sensor.py makes the same distinction deliberately.
            scanner_name = getattr(scanner_device, "name", None) or advert.name
            scanners[advert.scanner_address] = {
                "name": scanner_name,
                # Best-effort only. This is NOT a reliable join key: Bermuda's
                # `_distance_to_<slug>` entity_ids are fixed when the entity is
                # first created and do not follow later renames (Bermuda appends
                # a MAC to disambiguate duplicate scanner names, which changes
                # the name but not the existing entity_id). Consumers migrating
                # from entity scraping should map their stored slug to a scanner
                # address ONCE via the entity registry - disabled entities are
                # still registered - and then key off the address below.
                "slug": _cached_slug(scanner_name),
                "address": advert.scanner_address,
                # Bermuda builds its per-scanner entity unique_ids as
                # f"{device.unique_id}_{scanner.address_wifi_mac or scanner.address}_range",
                # so a consumer resolving stored entity slugs via the entity
                # registry needs these to join on. Exposing both avoids the
                # consumer having to know that wifi-mac fallback rule.
                "unique_id": getattr(scanner_device, "unique_id", None),
                "address_wifi_mac": getattr(scanner_device, "address_wifi_mac", None),
                "area_id": advert.area_id,
                "area_name": advert.area_name,
                "distance": advert.rssi_distance,
                "distance_raw": getattr(advert, "rssi_distance_raw", None),
                "rssi": advert.rssi,
                "stamp": advert.stamp,
                "age": nowstamp - advert.stamp if advert.stamp else None,
                "scanner_last_seen_age": (nowstamp - scanner_device.last_seen if scanner_device.last_seen else None),
                # The path-loss parameters Bermuda applied to THIS advert, so a
                # consumer converting raw rssi itself lands on the same scale.
                # A device-level ref_power of 0 means "use the global option".
                "ref_power": (getattr(advert, "ref_power", 0) or getattr(advert, "conf_ref_power", None)),
                "attenuation": getattr(advert, "conf_attenuation", None),
                "rssi_offset": getattr(advert, "conf_rssi_offset", 0) or 0,
            }
            if include_history:
                hist_rssi = getattr(advert, "hist_rssi", None) or ()
                hist_stamp = getattr(advert, "hist_stamp", None) or ()
                # Both lists are pushed together (newest first) in
                # update_advertisement, so zip() pairs each sample with its
                # own stamp; strict=False tolerates a transient length skew.
                scanners[advert.scanner_address]["history"] = [
                    [rssi, stamp]
                    for rssi, stamp in zip(hist_rssi, hist_stamp, strict=False)
                    if rssi is not None and stamp
                ]

        devices[address] = {
            "name": device.name,
            "slug": _cached_slug(device.name),
            "unique_id": getattr(device, "unique_id", None),
            # True for devices the user has configured Bermuda to track. This
            # is exactly the set Bermuda creates sensors (including the
            # per-scanner distance_to entities) for, so a consumer that used to
            # discover trackable devices by enumerating those entities can use
            # this instead and get the same answer with them all disabled.
            "tracked": bool(getattr(device, "create_sensor", False)),
            "address_type": device.address_type,
            "area_id": device.area_id,
            "area_name": device.area_name,
            "scanners": scanners,
        }

    return {
        "version": SNAPSHOT_VERSION,
        "stamp": nowstamp,
        "devices": devices,
        # Scanner liveness rides along (it is a few dozen entries built from
        # attributes, not an advert walk) so a consumer taking a snapshot
        # anyway does not need a second call for it.
        "scanners": _scanner_entries(coordinator, nowstamp, _cached_slug),
    }


# --- Tile identity ----------------------------------------------------------


def async_get_tile_identities(hass: HomeAssistant) -> dict[str, Any] | None:
    """Every Tile ID Bermuda has read, with where that Tile is now (see
    BermudaTileManager.identities). None if Bermuda is not set up."""
    coordinator = async_get_coordinator(hass)
    manager = getattr(coordinator, "tile_manager", None)
    return None if manager is None else manager.identities()


async def async_bind_tile(hass: HomeAssistant, tile_id: str, uid: str) -> dict[str, Any] | None:
    """Declare that configured Tile ``tile_id`` is the tag with Tile ID ``uid``
    and bind its live address now if one is known. Raises ValueError for an
    unknown Tile or an ID already declared as another Tile's. None if Bermuda
    is not set up."""
    coordinator = async_get_coordinator(hass)
    manager = getattr(coordinator, "tile_manager", None)
    if manager is None:
        return None
    address = manager.bind_by_uid(tile_id, uid)
    return {"tile_id": tile_id.lower(), "uid": uid.lower(), "address": address}


async def async_bind_tile_address(hass: HomeAssistant, tile_id: str, address: str) -> dict[str, Any] | None:
    """Declare that configured Tile ``tile_id`` is the tag at ``address`` now
    (the user identified it by where it is). Raises ValueError for an unknown
    Tile or address. None if Bermuda is not set up."""
    coordinator = async_get_coordinator(hass)
    manager = getattr(coordinator, "tile_manager", None)
    if manager is None:
        return None
    bound = manager.bind_address(tile_id, address)
    return {"tile_id": tile_id.lower(), "address": bound}
