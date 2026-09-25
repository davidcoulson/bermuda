"""Tests for the sensor platform's per-scanner entity creation.

These exercise `async_setup_entry`'s real dispatcher-driven closures directly
(the same signals the coordinator sends in production) against lightweight
fake coordinator/device objects, rather than running a full config-entry
setup with real Bluetooth data.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.bermuda import sensor
from custom_components.bermuda.const import (
    CONF_CREATE_SCANNER_ENTITIES,
    SIGNAL_DEVICE_NEW,
)

DEVICE_ADDRESS = "aa:bb:cc:dd:ee:01"


def _fake_scanner(address, *, is_remote_scanner=False, address_wifi_mac=None):
    return SimpleNamespace(
        address=address,
        is_remote_scanner=is_remote_scanner,
        address_wifi_mac=address_wifi_mac,
    )


def _fake_coordinator(hass, devices, scanners):
    return SimpleNamespace(
        hass=hass,
        devices=devices,
        have_floors=False,
        scanner_list={s.address for s in scanners},
        get_scanners=list(scanners),
        sensor_created=lambda address: None,
    )


def _fake_entry(coordinator, options):
    return SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        options=options,
        async_on_unload=lambda cb: None,
    )


async def _setup_and_fire(hass, coordinator, options, address=DEVICE_ADDRESS):
    entry = _fake_entry(coordinator, options)
    added: list = []

    def capture(entities, update_before_add=False):
        added.extend(entities)

    await sensor.async_setup_entry(hass, entry, capture)
    async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, address)
    await hass.async_block_till_done()
    return added


def _scanner_range_entities(added):
    return [e for e in added if isinstance(e, (sensor.BermudaSensorScannerRange, sensor.BermudaSensorScannerRangeRaw))]


async def test_scanner_entities_created_by_default(hass: HomeAssistant):
    """Default behaviour is unchanged: per-scanner range/range_raw entities are created."""
    scanner = _fake_scanner("11:22:33:44:55:66", address_wifi_mac="11:22:33:44:55:60")
    devices = {
        DEVICE_ADDRESS: SimpleNamespace(name="Test Device", unique_id=DEVICE_ADDRESS),
        scanner.address: SimpleNamespace(
            name="Test Scanner",
            unique_id=scanner.address,
            address_wifi_mac=scanner.address_wifi_mac,
            address=scanner.address,
        ),
    }
    coordinator = _fake_coordinator(hass, devices, [scanner])

    added = await _setup_and_fire(hass, coordinator, options={})

    assert len(_scanner_range_entities(added)) == 2  # Range + RangeRaw for the one scanner


async def test_scanner_entities_skipped_when_disabled(hass: HomeAssistant):
    """CONF_CREATE_SCANNER_ENTITIES=False must skip the O(devices x scanners)
    entities, since nothing needs to read them once a consumer uses the direct
    API (custom_components.bermuda.api), but must not touch the constant,
    per-device entities Bermuda always creates."""
    scanner = _fake_scanner("11:22:33:44:55:66", address_wifi_mac="11:22:33:44:55:60")
    devices = {
        DEVICE_ADDRESS: SimpleNamespace(name="Test Device", unique_id=DEVICE_ADDRESS),
        scanner.address: SimpleNamespace(
            name="Test Scanner",
            unique_id=scanner.address,
            address_wifi_mac=scanner.address_wifi_mac,
            address=scanner.address,
        ),
    }
    coordinator = _fake_coordinator(hass, devices, [scanner])

    added = await _setup_and_fire(hass, coordinator, options={CONF_CREATE_SCANNER_ENTITIES: False})

    assert _scanner_range_entities(added) == []
    # BermudaSensorRssi is never a per-scanner entity, so its presence proves
    # the always-created per-device batch survived.
    assert any(isinstance(e, sensor.BermudaSensorRssi) for e in added)


async def test_one_unresolved_scanner_does_not_block_the_others(hass: HomeAssistant):
    """Regression test for a production incident: a single BLE-scanning device
    whose wifi mac never resolves (a kiosk/panel acting as a scanner, not an
    ESPHome/Shelly proxy) must not freeze entity creation for every OTHER
    already-resolved scanner. Previously `create_scanner_entities` returned
    immediately the moment ANY scanner was unresolved, which silently capped
    every tracked device's registered scanners at whatever existed the moment
    such a scanner first appeared - worst for whichever device was tracked
    most recently, since it never caught up at all."""
    ready_scanner = _fake_scanner("11:22:33:44:55:66", address_wifi_mac="11:22:33:44:55:60")
    unresolved_scanner = _fake_scanner("77:88:99:aa:bb:cc", is_remote_scanner=True, address_wifi_mac=None)
    devices = {
        DEVICE_ADDRESS: SimpleNamespace(name="Test Device", unique_id=DEVICE_ADDRESS),
        ready_scanner.address: SimpleNamespace(
            name="Ready Scanner",
            unique_id=ready_scanner.address,
            address_wifi_mac=ready_scanner.address_wifi_mac,
            address=ready_scanner.address,
        ),
        unresolved_scanner.address: SimpleNamespace(
            name="Unresolved Scanner",
            unique_id=unresolved_scanner.address,
            address_wifi_mac=None,
            address=unresolved_scanner.address,
        ),
    }
    coordinator = _fake_coordinator(hass, devices, [ready_scanner, unresolved_scanner])

    added = await _setup_and_fire(hass, coordinator, options={})

    scanner_entities = _scanner_range_entities(added)
    # Only the ready scanner's pair was created - the unresolved one is
    # skipped, not blocking, so exactly one Range + one RangeRaw exist.
    assert len(scanner_entities) == 2
    assert all(e._scanner.address == ready_scanner.address for e in scanner_entities)
