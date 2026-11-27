"""Test Bermuda BLE Trilateration setup process."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

# from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.const import DOMAIN, IrkTypes
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator

from .const import MOCK_CONFIG
from homeassistant.config_entries import ConfigEntryState

# from pytest_homeassistant_custom_component.common import AsyncMock


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_setup_unload_and_reload_entry(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry: MockConfigEntry
):
    """Test entry setup and unload."""

    # Reload the entry and assert that the data from above is still there
    assert await hass.config_entries.async_reload(setup_bermuda_entry.entry_id)
    assert setup_bermuda_entry.state == ConfigEntryState.LOADED

    assert set(IrkTypes.unresolved()) == {
        IrkTypes.ADRESS_NOT_EVALUATED.value,
        IrkTypes.NO_KNOWN_IRK_MATCH.value,
        IrkTypes.NOT_RESOLVABLE_ADDRESS.value,
    }

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_unload(setup_bermuda_entry.entry_id)
    assert setup_bermuda_entry.state == ConfigEntryState.NOT_LOADED


async def test_setup_entry_exception(hass, error_on_get_data):
    """Test ConfigEntryNotReady when API raises an exception during entry setup."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")

    assert config_entry is not None

    # In this case we are testing the condition where async_setup_entry raises
    # ConfigEntryNotReady using the `error_on_get_data` fixture which simulates
    # an error.

    # Hmmm... this doesn't seem to be how this works. The super's _async_refresh might
    # handle exceptions, in which it then sets self.last_update_status, which is what
    # async_setup_entry checks in order to raise ConfigEntryNotReady, but I don't think
    # anything will "catch" our over-ridded async_refresh's exception.
    #  with pytest.raises(ConfigEntryNotReady):
    #     assert await async_setup_entry(hass, config_entry)


async def test_reload_code_replaces_the_modules(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry: MockConfigEntry):
    """bermuda.reload_code re-imports the package and sets the entry up again."""
    old = {n: m for n, m in sys.modules.items() if n.startswith("custom_components.bermuda")}
    old_flow = config_entries.HANDLERS[DOMAIN]

    class _EmptyYaml:
        """Stands in for the manufacturer yaml files, which the test config dir does not have."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self):
            return '{"uuids": [], "company_identifiers": []}'

    try:
        # The conftest patches name the old module objects, so they do not cover the re-imported ones.
        with patch("aiofiles.open", return_value=_EmptyYaml()):
            await hass.services.async_call(DOMAIN, "reload_code", {}, blocking=True)
            await hass.async_block_till_done()
        assert setup_bermuda_entry.state == ConfigEntryState.LOADED
        assert sys.modules["custom_components.bermuda"] is not old["custom_components.bermuda"]
        coordinator_module = sys.modules["custom_components.bermuda.coordinator"]
        assert coordinator_module is not old["custom_components.bermuda.coordinator"]
        assert type(setup_bermuda_entry.runtime_data.coordinator) is coordinator_module.BermudaDataUpdateCoordinator
        assert hass.services.has_service(DOMAIN, "reload_code")
        assert hass.services.has_service(DOMAIN, "dump_devices")
    finally:
        # Later tests patch by module path, so put the original modules back.
        for name in [n for n in sys.modules if n.startswith("custom_components.bermuda")]:
            del sys.modules[name]
        sys.modules.update(old)
        sys.modules["custom_components"].bermuda = old["custom_components.bermuda"]
        config_entries.HANDLERS[DOMAIN] = old_flow  # the re-import registered its own flow class


async def test_reload_clears_only_marketplaces_running_version_repairs(hass, monkeypatch):
    mod = sys.modules["custom_components.bermuda"]
    monkeypatch.setattr(mod, "_CODE_VERSION", "1.2.3")
    for domain, issue_id, issue_domain in (
        ("marketplace", "restart_required_1_tags/1.2.3", "bermuda"),
        ("marketplace", "restart_required_2_tags/v1.2.3", "bermuda"),
        ("marketplace", "restart_required_3_tags/1.2.4", "bermuda"),
        ("marketplace", "restart_required_4_tags/1.2.3", "ssh"),
        ("marketplace", "critical_x/1.2.3", "bermuda"),
        ("bermuda", "restart_required_x/1.2.3", "bermuda"),
    ):
        ir.async_create_issue(
            hass, domain, issue_id, is_fixable=False, issue_domain=issue_domain,
            severity=ir.IssueSeverity.WARNING, translation_key="x",
        )
    mod._clear_marketplace_restart(hass)
    assert set(ir.async_get(hass).issues) == {
        ("marketplace", "restart_required_3_tags/1.2.4"),
        ("marketplace", "restart_required_4_tags/1.2.3"),
        ("marketplace", "critical_x/1.2.3"),
        ("bermuda", "restart_required_x/1.2.3"),
    }


def test_code_version_is_the_manifest_version():
    mod = sys.modules["custom_components.bermuda"]
    manifest = json.loads((Path(mod.__file__).parent / "manifest.json").read_text())
    assert mod._CODE_VERSION == manifest["version"]
