"""
Tests for BermudaDevice class in bermuda_device.py.
"""

import json
import pytest
from types import SimpleNamespace
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


def test_update_area_and_floor_reads_the_floor_level(bermuda_device):
    """floor_level comes from the floor registry entry (it used to be assigned to itself)."""
    bermuda_device.ar.async_get_area.return_value = SimpleNamespace(name="Loft", icon=None, floor_id="top")
    bermuda_device.fr.async_get_floor.return_value = SimpleNamespace(name="Top", icon=None, level=2)
    bermuda_device._update_area_and_floor("loft")
    assert (bermuda_device.area_id, bermuda_device.area_name) == ("loft", "Loft")
    assert (bermuda_device.floor_id, bermuda_device.floor_name, bermuda_device.floor_level) == ("top", "Top", 2)
    # An area without a floor clears the level along with the rest.
    bermuda_device.ar.async_get_area.return_value = SimpleNamespace(name="Yard", icon=None, floor_id=None)
    bermuda_device._update_area_and_floor("yard")
    assert (bermuda_device.floor_id, bermuda_device.floor_name, bermuda_device.floor_level) == (None, None, None)


def test_update_area_and_floor_with_a_deleted_area_clears_area_id(bermuda_device):
    """An area id that no longer resolves must not stay on the device."""
    bermuda_device.ar.async_get_area.return_value = SimpleNamespace(name="Loft", icon=None, floor_id="top")
    bermuda_device.fr.async_get_floor.return_value = SimpleNamespace(name="Top", icon=None, level=2)
    bermuda_device._update_area_and_floor("loft")
    bermuda_device.ar.async_get_area.return_value = None
    bermuda_device._update_area_and_floor("loft")
    assert bermuda_device.area is None
    assert bermuda_device.area_id is None
    assert bermuda_device.area_name.startswith("Invalid Area")
    assert (bermuda_device.floor_id, bermuda_device.floor_name, bermuda_device.floor_level) == (None, None, None)


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


def test_async_as_scanner_init_force_rereads_registry_entries(bermuda_scanner, mock_coordinator, mock_scanner):
    """A forced init on a known scanner re-reads its registry entries.

    Changing a proxy's area in Home Assistant does not replace its scanner
    object, so an init that only reacts to a new object never saw the change
    (agittins/bermuda#856).
    """
    with patch.object(bermuda_scanner, "async_as_scanner_resolve_device_entries") as resolve:
        bermuda_scanner.async_as_scanner_init(mock_scanner)
        assert resolve.call_count == 1  # first set-up

        bermuda_scanner.async_as_scanner_init(mock_scanner)
        assert resolve.call_count == 1  # same object, not forced: nothing to do

        bermuda_scanner.async_as_scanner_init(mock_scanner, force=True)
        assert resolve.call_count == 2  # forced: read the registry again

    # Re-reading must not announce the scanner as new again.
    assert mock_coordinator.scanner_list_add.call_count == 1


def test_scanner_resolves_wifi_mac_before_announcing(bermuda_scanner, mock_coordinator, mock_remote_scanner):
    """The roster-changed signal must see the scanner's resolved wifi mac.

    scanner_list_add() dispatches SIGNAL_SCANNERS_CHANGED, and the sensor platform
    skips scanners whose wifi mac is still None. A scanner that registers after
    startup gets no other signal, so announcing it before resolving left it
    without per-scanner entities until a reload.
    """
    wifi_mac = "11:22:33:44:55:64"
    mock_coordinator.dr.async_get_devices.return_value = [
        SimpleNamespace(
            id="shelly_device",
            name="Shelly",
            name_by_user=None,
            area_id=None,
            connections={("mac", wifi_mac)},
        )
    ]
    seen_at_announce = []
    mock_coordinator.scanner_list_add.side_effect = lambda scanner: seen_at_announce.append(scanner.address_wifi_mac)

    bermuda_scanner.async_as_scanner_init(mock_remote_scanner)

    assert seen_at_announce == [wifi_mac]


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


def test_make_name_fallback_slugifies_once_per_input(bermuda_device):
    """The address-based name is rebuilt only when manufacturer or address changes."""
    from custom_components.bermuda import bermuda_device as module

    with patch.object(module, "slugify", wraps=module.slugify) as slug:
        assert bermuda_device.make_name() == "bermuda_aa_bb_cc_dd_ee_ff"
        first = slug.call_count
        assert first > 0
        bermuda_device.make_name()
        bermuda_device.make_name()
        assert slug.call_count == first, "a repeat call must not slugify again"
        bermuda_device.manufacturer = "Acme Corp"
        assert bermuda_device.make_name() == "acme_corp_aa_bb_cc_dd_ee_ff"
        assert slug.call_count > first


def test_calculate_data_flags_a_configured_device_once_per_list(bermuda_device):
    """The configured-devices scan happens once per options list, not per cycle."""
    from custom_components.bermuda.const import CONF_DEVICES

    configured = ["AA:BB:CC:DD:EE:FF"]
    bermuda_device.options[CONF_DEVICES] = configured
    bermuda_device.calculate_data()
    assert bermuda_device.create_sensor is True
    assert bermuda_device._configured_list_seen is configured
    # A new list (a reload) is scanned again, and a device dropped from it is
    # no longer flagged (create_sensor itself stays sticky, as before).
    other = BermudaDevice(address="11:22:33:44:55:66", coordinator=bermuda_device._coordinator)
    other.options = dict(bermuda_device.options)
    other.calculate_data()
    assert other.create_sensor is False
    other.options[CONF_DEVICES] = ["11:22:33:44:55:66"]
    other.calculate_data()
    assert other.create_sensor is True


def test_process_manufacturer_data_reads_only_the_newest_entry(bermuda_device):
    """Older history entries must not re-register or overwrite the current iBeacon."""
    newest = {0x004C: b"\x02\x15" + b"\xaa" * 16 + b"\x00\x01\x00\x02\xc5"}
    oldest = {0x004C: b"\x02\x15" + b"\xbb" * 16 + b"\x00\x03\x00\x04\xc5"}
    advert = SimpleNamespace(service_uuids=[], service_data=[], manufacturer_data=[newest, oldest])
    bermuda_device._coordinator.get_manufacturer_from_id.return_value = (None, False)
    bermuda_device.process_manufacturer_data(advert)
    assert bermuda_device.beacon_uuid == "aa" * 16
    assert (bermuda_device.beacon_major, bermuda_device.beacon_minor) == ("1", "2")
    bermuda_device._coordinator.register_ibeacon_source.assert_called_once_with(bermuda_device)


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


def test_scanner_registry_match_ignores_a_neighbour_two_above(mock_coordinator, mock_remote_scanner):
    """Issue #859: two ESP32-S3 proxies with adjacent MACs. Proxy A's BLE MAC
    (..:6e) is its own WiFi MAC (..:6c) + 2 and also proxy B's WiFi MAC
    (..:70) - 2, so both ESPHome entries fall in the +-3 window. A must keep
    its own name and WiFi MAC whatever order the registry returns them in."""
    from types import SimpleNamespace

    bluetooth_a = SimpleNamespace(
        id="bt-a",
        name="Bluetooth Proxy 44696c",
        name_by_user=None,
        area_id="bedroom",
        connections={("bluetooth", "1C:69:7A:44:69:6E")},
    )
    esphome_a = SimpleNamespace(
        id="esp-a",
        name="ESP32-S3 Bluetooth Proxy 44696c",
        name_by_user=None,
        area_id="bedroom",
        connections={("mac", "1c:69:7a:44:69:6c")},
    )
    esphome_b = SimpleNamespace(
        id="esp-b",
        name="ESP32-S3 Bluetooth Proxy 446970",
        name_by_user=None,
        area_id="bathroom",
        connections={("mac", "1c:69:7a:44:69:70")},
    )
    for order in (
        [bluetooth_a, esphome_a, esphome_b],
        [esphome_b, esphome_a, bluetooth_a],
        [bluetooth_a, esphome_b, esphome_a],
    ):
        mock_coordinator.dr.async_get_devices = MagicMock(return_value=list(order))
        scanner = BermudaDevice(address="1C:69:7A:44:69:6E", coordinator=mock_coordinator)
        scanner._hascanner = mock_remote_scanner
        scanner.async_as_scanner_resolve_device_entries()
        assert scanner.address_wifi_mac == "1c:69:7a:44:69:6c", order
        assert scanner.address_ble_mac == "1c:69:7a:44:69:6e"
        assert scanner.unique_id == "1c:69:7a:44:69:6c"
        assert scanner.name_devreg == "ESP32-S3 Bluetooth Proxy 44696c"


def _with_domains(mock_coordinator, domains):
    """Give the mock coordinator a config-entry lookup: entry id -> domain."""
    from types import SimpleNamespace

    mock_coordinator.hass.config_entries.async_get_entry = MagicMock(
        side_effect=lambda entry_id: SimpleNamespace(domain=domains[entry_id]) if entry_id in domains else None
    )


def test_scanner_registry_match_prefers_the_scanner_integration_over_a_router_with_the_same_mac(
    mock_coordinator, mock_remote_scanner
):
    """Issue #722: a router integration (TP-Link here) registers the same proxy
    under the same WiFi MAC, named "linux" and with no area. Both entries are
    the same distance from the BLE address, so registry order used to decide:
    router first meant no area (the repair) and the router's name. The
    ESPHome entry must win whatever the order, and with the core Bluetooth
    entry present the name must still be ESPHome's, not the router's."""
    from types import SimpleNamespace

    _with_domains(mock_coordinator, {"e-esp": "esphome", "e-tp": "tplink", "e-bt": "bluetooth"})
    esphome = SimpleNamespace(
        id="esp",
        name="TECHO5 proxy",
        name_by_user=None,
        area_id="office",
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        config_entries={"e-esp"},
    )
    router = SimpleNamespace(
        id="tplink",
        name="linux",
        name_by_user=None,
        area_id=None,
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        config_entries={"e-tp"},
    )
    bluetooth = SimpleNamespace(
        id="bt",
        name="Bluetooth proxy",
        name_by_user=None,
        area_id="office",
        connections={("bluetooth", "AA:BB:CC:DD:EE:12")},
        config_entries={"e-bt"},
    )
    for order, entry in (
        ([esphome, router], "esp"),
        ([router, esphome], "esp"),
        ([bluetooth, router, esphome], "bt"),
        ([router, esphome, bluetooth], "bt"),
    ):
        mock_coordinator.dr.async_get_devices = MagicMock(return_value=list(order))
        scanner = BermudaDevice(address="AA:BB:CC:DD:EE:12", coordinator=mock_coordinator)
        scanner._hascanner = mock_remote_scanner
        scanner.async_as_scanner_resolve_device_entries()
        ids = [d.id for d in order]
        assert scanner.entry_id == entry, ids
        assert scanner.area_id == "office", ids
        assert scanner.name_devreg == "TECHO5 proxy", ids
        assert scanner.address_wifi_mac == "aa:bb:cc:dd:ee:10", ids


def test_scanner_area_comes_from_another_entry_for_the_same_hardware(mock_coordinator, mock_remote_scanner):
    """The workaround people use for #722 - giving the router's entry an area -
    keeps working: the scanner's own entry has none, the same MAC's other
    entry has one, and that area is the scanner's."""
    from types import SimpleNamespace

    _with_domains(mock_coordinator, {"e-esp": "esphome", "e-tp": "tplink"})
    esphome = SimpleNamespace(
        id="esp",
        name="TECHO5 proxy",
        name_by_user=None,
        area_id=None,
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        config_entries={"e-esp"},
    )
    router = SimpleNamespace(
        id="tplink",
        name="linux",
        name_by_user=None,
        area_id="office",
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        config_entries={"e-tp"},
    )
    neighbour = SimpleNamespace(
        id="light",
        name="Hall light",
        name_by_user=None,
        area_id="hall",
        connections={("mac", "aa:bb:cc:dd:ee:14")},
        config_entries={"e-esp"},
    )
    for order in ([esphome, router, neighbour], [neighbour, router, esphome]):
        mock_coordinator.dr.async_get_devices = MagicMock(return_value=list(order))
        scanner = BermudaDevice(address="AA:BB:CC:DD:EE:12", coordinator=mock_coordinator)
        scanner._hascanner = mock_remote_scanner
        scanner.async_as_scanner_resolve_device_entries()
        assert scanner.entry_id == "esp" and scanner.name_devreg == "TECHO5 proxy"
        # The router's area (same MAC), never the neighbour light's (different MAC).
        assert scanner.area_id == "office", [d.id for d in order]


def test_a_re_resolve_replaces_an_earlier_winners_entry_id(mock_coordinator, mock_remote_scanner):
    """A scanner first resolved to the router's entry (the scanner
    integration's entry not registered yet), then re-resolved once it is,
    must carry the new winner's entry_id, not keep the router's."""
    from types import SimpleNamespace

    _with_domains(mock_coordinator, {"e-esp": "esphome", "e-tp": "tplink"})
    esphome = SimpleNamespace(
        id="esp",
        name="TECHO5 proxy",
        name_by_user=None,
        area_id="office",
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        config_entries={"e-esp"},
    )
    router = SimpleNamespace(
        id="tplink",
        name="linux",
        name_by_user=None,
        area_id=None,
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        config_entries={"e-tp"},
    )
    scanner = BermudaDevice(address="AA:BB:CC:DD:EE:12", coordinator=mock_coordinator)
    scanner._hascanner = mock_remote_scanner
    mock_coordinator.dr.async_get_devices = MagicMock(return_value=[router])
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.entry_id == "tplink"
    mock_coordinator.dr.async_get_devices = MagicMock(return_value=[router, esphome])
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.entry_id == "esp" and scanner.name_devreg == "TECHO5 proxy" and scanner.area_id == "office"


def test_scanner_integration_is_read_from_config_entry_id_on_current_cores(mock_coordinator):
    """From Home Assistant 2026.10 a device has one ``config_entry_id`` and
    reading ``config_entries`` is deprecated: use the new attribute, and do
    not touch the old one when the new one is there."""
    from types import SimpleNamespace

    class NewCoreDevice:
        config_entry_id = "e-esp"

        @property
        def config_entries(self):
            raise AssertionError("config_entries read on a core that has config_entry_id")

    _with_domains(mock_coordinator, {"e-esp": "esphome", "e-tp": "tplink"})
    scanner = BermudaDevice(address="AA:BB:CC:DD:EE:12", coordinator=mock_coordinator)
    assert scanner._from_scanner_integration(NewCoreDevice()) is True
    # An older core: no config_entry_id, the set of entries is read instead.
    assert scanner._from_scanner_integration(SimpleNamespace(config_entries={"e-tp"})) is False
    assert scanner._from_scanner_integration(SimpleNamespace(config_entries={"e-tp", "e-esp"})) is True


def test_settled_adverts_are_not_recalculated(mock_coordinator):
    """BermudaDevice.calculate_data skips exactly the adverts whose own
    calculate_data would return at once: away, cleared, nothing new."""
    from custom_components.bermuda.bermuda_advert import BermudaAdvert, advert_is_settled

    class Counting(BermudaAdvert):
        def __init__(self, new_stamp, rssi_distance, hist):  # pylint: disable=super-init-not-called
            self.new_stamp, self.rssi_distance, self.hist_distance_by_interval = new_stamp, rssi_distance, hist
            self.calls = 0

        def calculate_data(self):
            self.calls += 1

    settled = Counting(None, None, [])
    new_reading = Counting(123.0, None, [])
    still_near = Counting(None, 2.5, [2.5])
    clearing = Counting(None, None, [3.0])
    device = BermudaDevice(address="AA:BB:CC:DD:EE:01", coordinator=mock_coordinator)
    device.adverts = {("a", "1"): settled, ("a", "2"): new_reading, ("a", "3"): still_near, ("a", "4"): clearing}
    device.calculate_data()
    assert [settled.calls, new_reading.calls, still_near.calls, clearing.calls] == [0, 1, 1, 1]
    assert [advert_is_settled(a) for a in (settled, new_reading, still_near, clearing)] == [True, False, False, False]


def test_an_advert_shared_with_a_metadevice_is_calculated_once_per_cycle(mock_coordinator):
    """update_metadevices copies advert references into the metadevice, so the
    same object sits in two devices' dicts: it must be calculated once a cycle
    (whichever device comes first), and again next cycle."""
    from custom_components.bermuda.bermuda_advert import BermudaAdvert

    class Counting(BermudaAdvert):
        def __init__(self):  # pylint: disable=super-init-not-called
            self.new_stamp, self.rssi_distance, self.hist_distance_by_interval = None, 2.5, [2.5]
            self.calls = 0

        def calculate_data(self):
            self.calls += 1

    shared = Counting()
    source = BermudaDevice(address="AA:BB:CC:DD:EE:01", coordinator=mock_coordinator)
    metadevice = BermudaDevice(address="AA:BB:CC:DD:EE:01", coordinator=mock_coordinator)
    source.adverts = metadevice.adverts = {("a", "1"): shared}

    mock_coordinator.update_cycle = 1
    for device in (metadevice, source, metadevice):
        device.calculate_data()
    assert shared.calls == 1

    mock_coordinator.update_cycle = 2
    source.calculate_data()
    metadevice.calculate_data()
    assert shared.calls == 2


async def test_a_shared_real_advert_gets_one_history_entry_per_cycle(hass):
    """The visible symptom of the double call: hist_distance_by_interval grew by
    two each cycle for every metadevice's adverts, halving its smoothing window."""
    from bleak.backends.scanner import AdvertisementData

    from custom_components.bermuda.bermuda_advert import BermudaAdvert
    from custom_components.bermuda.const import CONF_MAX_VELOCITY, CONF_SMOOTHING_SAMPLES
    from bluetooth_data_tools import monotonic_time_coarse

    coordinator = SimpleNamespace(hass=hass, options={}, update_cycle=0)
    source = BermudaDevice("aa:bb:cc:dd:ee:ff", coordinator)
    metadevice = BermudaDevice("aa:bb:cc:dd:ee:ff", coordinator)
    scanner = BermudaDevice("11:22:33:44:55:66", coordinator)
    options = {CONF_MAX_VELOCITY: 3, CONF_SMOOTHING_SAMPLES: 20}
    advert = BermudaAdvert(source, AdvertisementData("Tracked", {}, {}, [], None, -70, ()), options, scanner)
    advert.rssi_distance = advert.rssi_distance_raw = 2.5
    advert.stamp = monotonic_time_coarse()
    advert.hist_distance_by_interval = [2.5]
    source.adverts = metadevice.adverts = {("a", "1"): advert}

    for cycle in (1, 2, 3):
        coordinator.update_cycle = cycle
        source.calculate_data()
        metadevice.calculate_data()
        assert len(advert.hist_distance_by_interval) == 1 + cycle
