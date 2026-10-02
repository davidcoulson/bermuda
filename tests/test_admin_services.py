"""Every Bermuda service refuses a signed-in user who is not an administrator."""

from __future__ import annotations

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.const import DOMAIN

CALLS = {
    "track_devices": {},
    "list_device_candidates": {},
    "add_findmy_accessory": {"accessory_json": "{}"},
    "remove_findmy_accessory": {"address": "aa:bb:cc:dd:ee:ff"},
    "set_options": {"options": {}},
    "bind_tile": {"tile_id": "tile_x", "tile_uid": "00"},
    "bind_tile_address": {"tile_id": "tile_x", "address": "aa:bb:cc:dd:ee:ff"},
    "dump_devices": {},
}


async def test_every_service_is_registered(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    assert set(hass.services.async_services_for_domain(DOMAIN)) == set(CALLS)


@pytest.mark.parametrize("service", sorted(CALLS))
async def test_non_admin_user_is_refused(
    hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry, hass_read_only_user, service: str
):
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            service,
            CALLS[service],
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )


async def test_admin_and_automation_calls_still_work(
    hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry, hass_admin_user
):
    for context in (Context(user_id=hass_admin_user.id), Context()):
        result = await hass.services.async_call(
            DOMAIN, "list_device_candidates", {}, blocking=True, return_response=True, context=context
        )
        assert isinstance(result, dict)
