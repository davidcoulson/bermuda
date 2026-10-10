"""Removing a device from the registry must stop Bermuda tracking it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.bermuda import async_remove_config_entry_device
from custom_components.bermuda.const import CONF_DEVICES, DOMAIN


async def test_removing_an_ibeacon_stops_tracking_it():
    """Its uuid_major_minor id has underscores; splitting it found nothing."""
    beacon_id = "0123456789abcdef0123456789abcdef_1_2"
    device = SimpleNamespace(address=beacon_id, create_sensor=True)
    coordinator = SimpleNamespace(devices={beacon_id: device})
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        options={CONF_DEVICES: [beacon_id.upper(), "AA:BB:CC:DD:EE:FF"]},
    )
    hass = MagicMock()
    device_entry = SimpleNamespace(identifiers={(DOMAIN, beacon_id)}, name="Beacon")
    assert await async_remove_config_entry_device(hass, entry, device_entry) is True
    assert device.create_sensor is False
    hass.config_entries.async_update_entry.assert_called_once()
    assert hass.config_entries.async_update_entry.call_args.kwargs["options"][CONF_DEVICES] == ["AA:BB:CC:DD:EE:FF"]
