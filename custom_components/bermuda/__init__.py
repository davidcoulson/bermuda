"""
Custom integration to integrate Bermuda BLE Trilateration with Home Assistant.

For more details about this integration, please refer to
https://github.com/agittins/bermuda
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_registry import async_migrate_entries
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store

from .const import (
    _LOGGER,
    CONF_DEVICES,
    DOMAIN,
    FINDMY_STORAGE_KEY,
    FINDMY_STORAGE_VERSION,
    PLATFORMS,
    STARTUP_MESSAGE,
    TILE_STORAGE_KEY,
    TILE_STORAGE_VERSION,
)
from .coordinator import BermudaDataUpdateCoordinator
from .util import mac_math_offset, mac_norm

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

type BermudaConfigEntry = ConfigEntry[BermudaData]


@dataclass
class BermudaData:
    """Holds global data for Bermuda."""

    coordinator: BermudaDataUpdateCoordinator


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _import_code(package: str) -> None:
    """Import the package and the platforms it forwards; runs in the import executor.

    After an unload Home Assistant still lists "<domain>.<platform>" as loaded,
    so it would import the new platform modules in the event loop (and warn);
    once they are in sys.modules its own import is a cache hit.
    """
    # The new module's list: the old one may name a platform the new code dropped.
    for platform in importlib.import_module(package).PLATFORMS:
        importlib.import_module(f"{package}.{platform}")


def _forget_code(hass: HomeAssistant, package: str, domain: str) -> None:
    """Drop a package's modules so the next setup imports them from disk again.

    Home Assistant keeps its own copy of each imported integration module
    (loader.py: hass.data["components"], keyed "<domain>" and
    "<domain>.<platform>") next to Python's sys.modules; both have to go.
    """
    for cache, root in (
        (sys.modules, package),
        (hass.data.get("components", {}), domain),
        (hass.data.get("missing_platforms", {}), domain),
    ):
        for name in [n for n in cache if n == root or n.startswith(root + ".")]:
            del cache[name]
    importlib.invalidate_caches()  # or a file added since the last import may not be found


def _register_reload_code(hass: HomeAssistant) -> None:
    """Dev: bermuda.reload_code loads the Python files from disk without a restart."""

    async def _reload_code(call: ServiceCall) -> None:
        # A second call during the first one's awaits would evict the caches again and race the setup.
        if hass.data.get("bermuda_reloading"):
            raise HomeAssistantError(f"{DOMAIN} is already reloading its code")
        hass.data["bermuda_reloading"] = True
        try:
            await _reload_code_once()
        finally:
            hass.data.pop("bermuda_reloading", None)

    async def _reload_code_once() -> None:
        # Disabled and ignored entries are never set up, so they are not ours to reload.
        entries = hass.config_entries.async_entries(
            DOMAIN, include_ignore=False, include_disabled=False
        )
        for entry in entries:
            if not await hass.config_entries.async_unload(entry.entry_id):
                raise HomeAssistantError(f"{DOMAIN} did not unload; restart Home Assistant instead")
        _forget_code(hass, __name__, DOMAIN)
        try:
            # Bermuda has no async_setup: importing is the check for a syntax error.
            await hass.async_add_import_executor_job(_import_code, __name__)
        except Exception as err:
            # This handler stays registered, so the action works again once the code is fixed.
            raise HomeAssistantError(
                f"{DOMAIN} is unloaded: the new code did not load ({err}). Fix it and call {DOMAIN}.reload_code again"
            ) from err
        failed = [e.title for e in entries if not await hass.config_entries.async_setup(e.entry_id)]
        if failed:
            raise HomeAssistantError(f"{DOMAIN} did not set up again for {failed}; see the log")

    async_register_admin_service(hass, DOMAIN, "reload_code", _reload_code)


async def async_setup_entry(hass: HomeAssistant, entry: BermudaConfigEntry) -> bool:
    """Set up this integration using UI."""
    _register_reload_code(hass)
    if hass.data.get(DOMAIN) is None:
        _LOGGER.info(STARTUP_MESSAGE)
    coordinator = BermudaDataUpdateCoordinator(hass, entry)
    entry.runtime_data = BermudaData(coordinator)
    # Tile metadevice bindings persist across restarts (see bermuda_tile.py).
    await coordinator.tile_manager.async_load()

    async def on_failure():
        _LOGGER.debug("Coordinator last update failed, rasing ConfigEntryNotReady")
        raise ConfigEntryNotReady

    try:
        await coordinator.async_refresh()
    except Exception as ex:  # noqa: BLE001
        _LOGGER.exception(ex)
        await on_failure()
    if not coordinator.last_update_success:
        await on_failure()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # One-shot migration: earlier versions let metadevices fall through to the
    # generic device_info branch, which registered their id as a bluetooth
    # connection. Runs here rather than in the update loop - it is a migration,
    # not per-cycle work.
    coordinator.async_purge_invalid_bluetooth_connections()

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: BermudaConfigEntry) -> bool:
    """Migrate previous config entries."""
    _LOGGER.debug("Migrating config from version %s.%s", config_entry.version, config_entry.minor_version)
    _oldversion = f"{config_entry.version}.{config_entry.minor_version}"

    if config_entry.version == 3:  # it won't be.
        # Bogus version for now, wanted to placeholder the migrate_entries / unique_id thing.
        # If we need to manage unique_id of sensors, we probably just need
        # to manage the callback, but not worry about the hass update.
        #
        # This is lifted from the discussion at https://community.home-assistant.io/t/migrating-unique-ids/348512
        #
        # Also worth looking at https://github.com/home-assistant/core/pull/115265/files for an example
        # of migrating unique_ids from one form to another.
        #
        old_unique_id = config_entry.unique_id
        new_unique_id = mac_math_offset(old_unique_id, 3)

        @callback
        def update_unique_id(entity_entry):
            """Update unique_id of an entity."""
            return {"new_unique_id": entity_entry.unique_id.replace(old_unique_id, new_unique_id)}

        if old_unique_id != new_unique_id:
            await async_migrate_entries(hass, config_entry.entry_id, update_unique_id)
            hass.config_entries.async_update_entry(config_entry, unique_id=new_unique_id)

        return False

    if f"{config_entry.version}.{config_entry.minor_version}" != _oldversion:
        _LOGGER.info("Migrated config entry to version %s.%s", config_entry.version, config_entry.minor_version)

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: BermudaConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Implements user-deletion of devices from device registry."""
    coordinator: BermudaDataUpdateCoordinator = config_entry.runtime_data.coordinator
    address = None
    for domain, ident in device_entry.identifiers:
        if domain == DOMAIN:
            # The identifier is the device's unique_id: a MAC address, an IRK,
            # or an iBeacon's uuid_major_minor - which has underscores of its
            # own, so it must be matched whole, not split.
            address = ident
    if address is not None:
        # Older versions added a per-sensor suffix ("_range") to the id. Strip
        # trailing "_part"s one at a time until something matches, so an
        # iBeacon's own underscores ("uuid_major_minor_range") survive.
        device = None
        candidate = address
        while device is None:
            device = coordinator.devices.get(mac_norm(candidate))
            if "_" not in candidate:
                break
            candidate = candidate.rsplit("_", 1)[0]
        if device is None:
            _LOGGER.warning("Failed to locate device entry for %s", address)
            return True
        device.create_sensor = False
        # calculate_data() turns create_sensor back on every cycle for any
        # address in CONF_DEVICES, so a removed device has to leave that list
        # too, or its entities come straight back.
        tracked = config_entry.options.get(CONF_DEVICES, [])
        # Compared normalised: a stored address may use "-" or "_" separators.
        target = mac_norm(device.address)
        keep = [a for a in tracked if mac_norm(a) != target]
        if len(keep) != len(tracked):
            hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, CONF_DEVICES: keep})
            # The reload that follows is scheduled, not immediate: update the
            # live options too (every BermudaDevice shares this dict), or a
            # cycle in between turns create_sensor straight back on.
            coordinator.options[CONF_DEVICES] = keep
        return True
    # Even if we don't know this address it probably just means it's stale or from
    # a previous version that used weirder names. Allow it.
    _LOGGER.warning(
        "Didn't find address for %s but allowing deletion to proceed.",
        device_entry.name,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BermudaConfigEntry) -> bool:
    """Handle removal of an entry."""
    # Alignment is throttled, so a dirty value may have no pending write behind
    # it. Flush before tearing down or a reload silently loses it.
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        await coordinator.coordinator.async_flush_findmy_alignment()
        await coordinator.coordinator.async_shutdown_background_writers()
    if unload_result := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        _LOGGER.debug("Unloaded platforms.")
    return unload_result


async def async_remove_entry(hass: HomeAssistant, entry: BermudaConfigEntry) -> None:
    """Delete the FindMy alignment and Tile binding stores when the integration is removed."""
    await Store(hass, FINDMY_STORAGE_VERSION, FINDMY_STORAGE_KEY).async_remove()
    # Left behind, old Tile bindings and IDs would come back on a fresh re-add.
    await Store(hass, TILE_STORAGE_VERSION, TILE_STORAGE_KEY).async_remove()
    _LOGGER.debug("Removed FindMy alignment and Tile binding stores.")


async def async_reload_entry(hass: HomeAssistant, entry: BermudaConfigEntry) -> None:
    """
    Reload config entry - unless the change is already live in memory.

    api.async_set_rssi_offsets applies new offsets to the running coordinator
    and then persists them to the entry so they survive a restart. That
    persist fires this listener; reloading would tear down and rebuild the
    coordinator (dropping every advert history) for a change it is already
    carrying, so when the entry's options are exactly what the coordinator
    said it applied, there is nothing to do.
    """
    coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
    pending = getattr(coordinator, "inline_options", None)
    if pending is not None and dict(entry.options) == pending:
        coordinator.inline_options = None
        _LOGGER.debug("Options change already applied in memory; skipping reload")
        return
    hass.config_entries.async_schedule_reload(entry.entry_id)
