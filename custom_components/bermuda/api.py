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

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import callback
from homeassistant.util import slugify

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import BermudaDataUpdateCoordinator

# Bumped only on an incompatible change to the snapshot shape. Consumers should
# check this and degrade gracefully rather than assume.
SNAPSHOT_VERSION = 1

# Additive capabilities layered on SNAPSHOT_VERSION 1 without changing any
# existing key. A consumer that wants one of these should feature-detect it
# here rather than parse versions, and fall back to the v1 behaviour when the
# name is absent (an older Bermuda build).
#
#   tracked_only     async_get_advert_snapshot(..., tracked_only=True)
#   tracked_devices  async_get_tracked_devices()
#   scanners         async_get_scanners(), and a top-level "scanners" map in the snapshot
SNAPSHOT_FEATURES = frozenset({"tracked_only", "tracked_devices", "scanners"})


@callback
def async_get_coordinator(hass: HomeAssistant) -> BermudaDataUpdateCoordinator | None:
    """
    Return Bermuda's coordinator, or None if Bermuda is not set up.

    Callers can use the returned coordinator's `async_add_listener()` to be
    notified after each update cycle, then call `async_get_advert_snapshot()`.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime_data = getattr(entry, "runtime_data", None)
        coordinator = getattr(runtime_data, "coordinator", None)
        if coordinator is not None:
            return coordinator
    return None


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
def async_get_tracked_devices(hass: HomeAssistant) -> dict[str, Any] | None:
    """
    Return the devices Bermuda is configured to track, keyed by address.

    A consumer that only needs to know WHICH devices are tracked (to notice a
    device being added or removed, say) should call this rather than take a
    full snapshot: it reads one attribute per known device and builds nothing
    for the untracked majority, whereas the snapshot serialises every advert
    of every device in range before a consumer can filter.

    Shape::

        {"<device address>": {"name": str, "slug": str, "unique_id": str | None}, ...}

    Returns None if Bermuda is not set up.
    """
    coordinator = async_get_coordinator(hass)
    if coordinator is None:
        return None
    cached_slug = _slug_memo()
    tracked: dict[str, Any] = {}
    for address, device in coordinator.devices.items():
        if not getattr(device, "create_sensor", False):
            continue
        tracked[address] = {
            "name": device.name,
            "slug": cached_slug(device.name),
            "unique_id": getattr(device, "unique_id", None),
        }
    return tracked


@callback
def async_get_advert_snapshot(
    hass: HomeAssistant,
    addresses: set[str] | None = None,
    *,
    include_empty: bool = False,
    tracked_only: bool = False,
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
            }

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
