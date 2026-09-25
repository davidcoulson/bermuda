"""
Tests for BermudaDevice class in bermuda_device.py.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from homeassistant.components.bluetooth import BaseHaScanner, BaseHaRemoteScanner
from homeassistant.helpers.json import JSONEncoder
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import ICON_DEFAULT_AREA, ICON_DEFAULT_FLOOR


@pytest.fixture
def mock_coordinator():
    """Fixture for mocking BermudaDataUpdateCoordinator."""
    coordinator = MagicMock()
    coordinator.options = {}
    coordinator.hass_version_min_2025_4 = True
    return coordinator


@pytest.fixture
def mock_scanner():
    """Fixture for mocking BaseHaScanner."""
    scanner = MagicMock(spec=BaseHaScanner)
    scanner.time_since_last_detection.return_value = 5.0
    scanner.source = "mock_source"
    return scanner


@pytest.fixture
def mock_remote_scanner():
    """Fixture for mocking BaseHaRemoteScanner."""
    scanner = MagicMock(spec=BaseHaRemoteScanner)
    scanner.time_since_last_detection.return_value = 5.0
    scanner.source = "mock_source"
    return scanner


@pytest.fixture
def bermuda_device(mock_coordinator):
    """Fixture for creating a BermudaDevice instance."""
    return BermudaDevice(address="AA:BB:CC:DD:EE:FF", coordinator=mock_coordinator)


@pytest.fixture
def bermuda_scanner(mock_coordinator):
    """Fixture for creating a BermudaDevice Scanner instance."""
    return BermudaDevice(address="11:22:33:44:55:66", coordinator=mock_coordinator)


def test_bermuda_device_initialization(bermuda_device):
    """Test BermudaDevice initialization."""
    assert bermuda_device.address == "aa:bb:cc:dd:ee:ff"
    assert bermuda_device.name.startswith("bermuda_")
    assert bermuda_device.area_icon == ICON_DEFAULT_AREA
    assert bermuda_device.floor_icon == ICON_DEFAULT_FLOOR
    assert bermuda_device.zone == "not_home"


def test_async_as_scanner_init(bermuda_scanner, mock_scanner):
    """Test async_as_scanner_init method."""
    bermuda_scanner.async_as_scanner_init(mock_scanner)
    assert bermuda_scanner._hascanner == mock_scanner
    assert bermuda_scanner.is_scanner is True
    assert bermuda_scanner.is_remote_scanner is False


def test_async_as_scanner_update(bermuda_scanner, mock_scanner):
    """Test async_as_scanner_update method."""
    bermuda_scanner.async_as_scanner_update(mock_scanner)
    assert bermuda_scanner.last_seen > 0


def test_async_as_scanner_get_stamp(bermuda_scanner, mock_scanner, mock_remote_scanner):
    """Test async_as_scanner_get_stamp method."""
    bermuda_scanner.async_as_scanner_init(mock_scanner)
    bermuda_scanner.stamps = {"AA:BB:CC:DD:EE:FF": 123.45}

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:bb:CC:DD:EE:FF")
    assert stamp is None

    bermuda_scanner.async_as_scanner_init(mock_remote_scanner)

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:bb:CC:DD:EE:FF")
    assert stamp == 123.45

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:BB:CC:DD:E1:FF")
    assert stamp is None


def test_make_name(bermuda_device):
    """Test make_name method."""
    bermuda_device.name_by_user = "Custom Name"
    name = bermuda_device.make_name()
    assert name == "Custom Name"
    assert bermuda_device.name == "Custom Name"


def test_process_advertisement(bermuda_device, bermuda_scanner):
    """Test process_advertisement method."""
    advertisement_data = MagicMock()
    bermuda_device.process_advertisement(bermuda_scanner, advertisement_data)
    assert len(bermuda_device.adverts) == 1


# def test_process_manufacturer_data(bermuda_device):
#     """Test process_manufacturer_data method."""
#     mock_advert = MagicMock()
#     mock_advert.service_uuids = ["0000abcd-0000-1000-8000-00805f9b34fb"]
#     mock_advert.manufacturer_data = [{"004C": b"\x02\x15"}]
#     bermuda_device.process_manufacturer_data(mock_advert)
#     assert bermuda_device.manufacturer == "Apple Inc."


def test_to_dict(bermuda_device):
    """Test to_dict method."""
    device_dict = bermuda_device.to_dict()
    assert isinstance(device_dict, dict)
    assert device_dict["address"] == "aa:bb:cc:dd:ee:ff"


def test_to_dict_is_json_serialisable_with_area_advert(bermuda_device, bermuda_scanner):
    """to_dict() output must be JSON-serialisable, including area_advert.

    Regression test: to_dict() passed the area_advert BermudaAdvert object
    through unconverted. That happened to survive JSON encoding only because
    BermudaAdvert subclassed dict (it serialised as a useless empty {}).
    Once that unused dict base was removed, the dump_devices service call and
    the config-entry diagnostics download - which both serialise this output -
    failed outright with "Unable to serialize to JSON. Bad data found at
    $.service_response.<device>.area_advert".
    """
    advertisement_data = MagicMock()
    bermuda_device.process_advertisement(bermuda_scanner, advertisement_data)
    advert = next(iter(bermuda_device.adverts.values()))
    bermuda_device.area_advert = advert

    device_dict = bermuda_device.to_dict()

    # The advert must be reduced to an identifying string, not passed through
    # as a BermudaAdvert object (which the JSON encoder cannot handle, and
    # which the old dict-subclass behaviour silently rendered as an empty {}).
    assert isinstance(device_dict["area_advert"], str)
    assert device_dict["area_advert"] == repr(advert)

    # That field must survive the encoder HA serialises service responses with.
    json.dumps(device_dict["area_advert"], cls=JSONEncoder)


def test_repr(bermuda_device):
    """Test __repr__ method."""
    repr_str = repr(bermuda_device)
    assert repr_str == f"{bermuda_device.name} [{bermuda_device.address}]"


def test_scanner_registry_match_prefers_own_device_over_a_mac_neighbour(mock_coordinator, mock_remote_scanner):
    """Regression: the +-3 octet registry search also matches a NEIGHBOUR.

    An ESPHome proxy at BLE ..:4a (WiFi ..:48) and an unrelated ESPHome light
    at WiFi ..:4c both fall inside the window, and the last entry the
    registry returned used to win - so the proxy took the light's name,
    unique_id and wifi mac (and could never be placed as the receiver it is).
    The entry whose bluetooth connection IS the scanner address must win,
    whatever order the registry returns them in.
    """
    from types import SimpleNamespace

    proxy = SimpleNamespace(
        id="dev-proxy",
        name="Eilee Bedroom RRN00 4e8948",
        name_by_user=None,
        area_id="eilee_bedroom",
        connections={("mac", "dc:06:75:4e:89:48"), ("bluetooth", "dc:06:75:4e:89:4a")},
    )
    light = SimpleNamespace(
        id="dev-light",
        name="Basement Lumary 4e894c",
        name_by_user=None,
        area_id="basement",
        connections={("mac", "dc:06:75:4e:89:4c")},
    )
    for order in ([proxy, light], [light, proxy]):
        mock_coordinator.dr.async_get_devices = MagicMock(return_value=list(order))
        scanner = BermudaDevice(address="DC:06:75:4E:89:4A", coordinator=mock_coordinator)
        scanner._hascanner = mock_remote_scanner
        scanner.async_as_scanner_resolve_device_entries()
        assert scanner.unique_id == "dc:06:75:4e:89:48", order
        assert scanner.address_wifi_mac == "dc:06:75:4e:89:48"
        assert scanner.address_ble_mac == "dc:06:75:4e:89:4a"
        assert scanner.entry_id == "dev-proxy"
        assert scanner.name_devreg == "Eilee Bedroom RRN00 4e8948"


def test_scanner_registry_match_falls_back_to_the_wifi_plus_two_rule(mock_coordinator, mock_remote_scanner):
    """With no bluetooth entry at all (older cores), the espressif BLE = WiFi + 2
    neighbour beats any other offset."""
    from types import SimpleNamespace

    proxy = SimpleNamespace(
        id="p", name="Proxy", name_by_user=None, area_id=None, connections={("mac", "dc:06:75:4e:89:48")}
    )
    light = SimpleNamespace(
        id="l", name="Light", name_by_user=None, area_id=None, connections={("mac", "dc:06:75:4e:89:4c")}
    )
    mock_coordinator.dr.async_get_devices = MagicMock(return_value=[light, proxy])
    scanner = BermudaDevice(address="dc:06:75:4e:89:4a", coordinator=mock_coordinator)
    scanner._hascanner = mock_remote_scanner
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.unique_id == "dc:06:75:4e:89:48"
    assert scanner.name_devreg == "Proxy"


@pytest.mark.parametrize(
    ("first_char", "expected"),
    [(c, "bd_addr_random_unresolvable") for c in "0123"]
    + [(c, "bd_addr_random_resolvable") for c in "4567"]
    + [(c, "reserved") for c in "89ab"]
    + [(c, "bd_addr_random_static") for c in "cdef"],
)
def test_address_type_classifier_uses_the_top_two_bits(mock_coordinator, first_char, expected):
    """Regression: the classifier tested the top two bits with `&` where it
    meant `==`, so 0b00 addresses stayed unknown, every random-static (0b11)
    address was labelled resolvable and handed to the IRK resolver, and
    BDADDR_TYPE_RANDOM_STATIC was never assigned at all."""
    device = BermudaDevice(address=f"{first_char}a:bb:cc:dd:ee:ff", coordinator=mock_coordinator)
    assert device.address_type == expected
    if expected == "bd_addr_random_resolvable":
        mock_coordinator.irk_manager.check_mac.assert_called_once_with(f"{first_char}a:bb:cc:dd:ee:ff")
    else:
        mock_coordinator.irk_manager.check_mac.assert_not_called()
