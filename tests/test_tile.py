"""Tile trackers: fingerprinting (phase 1), the rotation re-bind heuristic
(phase 2) and the phase-0 capture."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bleak.backends.scanner import AdvertisementData

from custom_components.bermuda import bermuda_tile
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.bermuda_tile import (
    BermudaTileManager,
    handover_score,
    mac_from_tile_id,
    tile_capture,
    tile_metadevice_id,
)
from custom_components.bermuda.const import (
    ADDR_TYPE_TILE,
    CONF_DEVICES,
    METADEVICE_TILE_DEVICE,
    METADEVICE_TYPE_TILE_SOURCE,
    TILE_HANDOVER_WINDOW,
    TILE_REF_POWER,
    TILE_SERVICE_UUID,
    TILE_SILENT_SECS,
)

TILE_ADVERT = AdvertisementData(
    local_name=None,
    manufacturer_data={},
    service_data={},          # measured: this hardware carries no service data at all
    service_uuids=[TILE_SERVICE_UUID],
    tx_power=None,
    rssi=-71,
    platform_data=(),
)
PLAIN_ADVERT = AdvertisementData(
    local_name="not a tile", manufacturer_data={}, service_data={},
    service_uuids=["0000180f-0000-1000-8000-00805f9b34fb"], tx_power=None, rssi=-60, platform_data=(),
)


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.options = {}
    coordinator.hass_version_min_2025_4 = True
    return coordinator


# --- phase 1: fingerprinting ------------------------------------------------ #


def test_tile_advert_marks_device_calibrates_and_names_it(mock_coordinator):
    scanner = BermudaDevice(address="11:22:33:44:55:66", coordinator=mock_coordinator)
    device = BermudaDevice(address="29:10:b2:bc:a8:5a", coordinator=mock_coordinator)
    assert device.is_tile is False

    device.process_advertisement(scanner, TILE_ADVERT)

    assert device.is_tile is True
    assert device.manufacturer == "Tile"
    assert device.ref_power == TILE_REF_POWER == -53.0
    assert device.name == "tile_29_10_b2_bc_a8_5a"
    assert device.first_seen and device.first_seen == device.last_seen


def test_user_ref_power_override_survives_tile_detection(mock_coordinator):
    scanner = BermudaDevice(address="11:22:33:44:55:66", coordinator=mock_coordinator)
    device = BermudaDevice(address="29:10:b2:bc:a8:5a", coordinator=mock_coordinator)
    device.set_ref_power(-58.0)  # what the Number entity does
    device.process_advertisement(scanner, TILE_ADVERT)
    assert device.is_tile and device.ref_power == -58.0


def test_non_tile_advert_changes_nothing(mock_coordinator):
    scanner = BermudaDevice(address="11:22:33:44:55:66", coordinator=mock_coordinator)
    device = BermudaDevice(address="aa:bb:cc:dd:ee:01", coordinator=mock_coordinator)
    device.process_advertisement(scanner, PLAIN_ADVERT)
    assert device.is_tile is False and device.ref_power == 0 and device.manufacturer is None


def test_tile_metadevice_address_is_recognised(mock_coordinator):
    device = BermudaDevice(address="TILE_2910B2BCA85A", coordinator=mock_coordinator)
    assert device.address == "tile_2910b2bca85a"          # mac_norm leaves it alone but lower-cases
    assert device.address_type == ADDR_TYPE_TILE
    assert METADEVICE_TILE_DEVICE in device.metadevice_type
    assert device.is_tile is True
    assert device.make_name()  # nameable (not BDADDR_TYPE_NOT_MAC48)


def test_tile_id_round_trips():
    assert tile_metadevice_id("29:10:B2:BC:A8:5A") == "tile_2910b2bca85a"
    assert mac_from_tile_id("tile_2910b2bca85a") == "29:10:b2:bc:a8:5a"
    assert mac_from_tile_id("tile_junk") is None


# --- phase 2: the re-bind heuristic ----------------------------------------- #


def _dev(address, first, last, rssi_by_scanner, *, is_tile=True):
    """A device stub with one advert per scanner; hist_rssi newest first."""
    adverts = {}
    for scanner, hist in rssi_by_scanner.items():
        adverts[(address, scanner)] = SimpleNamespace(
            scanner_address=scanner, hist_rssi=list(hist), rssi=hist[0], name=scanner,
            hist_stamp=[last], service_data=[], manufacturer_data=[], local_name=[],
            service_uuids=[TILE_SERVICE_UUID],
        )
    return SimpleNamespace(
        address=address, is_tile=is_tile, first_seen=first, last_seen=last, adverts=adverts,
        metadevice_sources=[], metadevice_type=set(), name=address, create_sensor=False,
        address_type="bd_addr_random_unresolvable", ref_power=-53.0,
    )


class _Coord:
    """The slice of the coordinator the manager touches."""

    def __init__(self, devices, configured=()):
        self.devices = dict(devices)
        self.metadevices = {}
        self.options = {CONF_DEVICES: list(configured)}
        self.hass = None

    def _get_device(self, address):
        return self.devices.get(address.lower())

    def _get_or_create_device(self, address):
        address = address.lower()
        if address not in self.devices:
            self.devices[address] = _dev(address, 0, 0, {}, is_tile=address.startswith("tile_"))
        return self.devices[address]


def _house(now=10_000.0):
    """Tile A bound and quiet since now-60; B appeared right after with the
    same per-scanner RSSI on two scanners; C is a different tag elsewhere."""
    a = _dev("29:10:b2:bc:a8:5a", now - 3600, now - 60, {"s1": [-70, -71, -70], "s2": [-80, -79, -81]})
    b = _dev("17:31:c5:0f:0c:9e", now - 55, now - 5, {"s1": [-69, -72, -70], "s2": [-78, -80, -79]})
    c = _dev("2a:fe:fc:10:70:fc", now - 50, now - 5, {"s1": [-90, -91, -90], "s2": [-60, -61, -60]})
    return a, b, c


def test_handover_score_needs_shared_scanners():
    a, b, _ = _house()
    score, n = handover_score(a, b)
    assert n == 2 and score < 2.0
    lone = _dev("x", 0, 0, {"s3": [-70]})
    assert handover_score(a, lone) is None


def test_manager_seeds_and_rebinds_across_a_rotation():
    now = 10_000.0
    a, b, c = _house(now)
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
    manager = BermudaTileManager(coord)

    manager.async_update(nowstamp=now)

    meta = coord.metadevices[tile_id]
    assert meta.create_sensor is True
    assert manager.bindings[tile_id] == [b.address, a.address]     # B re-bound, A kept as history
    assert meta.metadevice_sources[0] == b.address
    assert METADEVICE_TYPE_TILE_SOURCE in b.metadevice_type
    assert manager.handovers == 1 and manager.ambiguous_handovers == 0
    assert manager.last_handover["to"] == b.address
    assert c.address not in manager.bindings[tile_id]


def test_manager_does_not_rebind_while_the_bound_address_is_still_heard():
    now = 10_000.0
    a, b, c = _house(now)
    a.last_seen = now - 5  # A still talking
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id])
    manager = BermudaTileManager(coord)
    manager.async_update(nowstamp=now)
    assert manager.bindings[tile_id] == [a.address]


def test_manager_refuses_an_ambiguous_handover():
    """Two candidates that both look like A: bind nothing, count it, log it."""
    now = 10_000.0
    a, b, _ = _house(now)
    b2 = _dev("34:a2:ec:0a:c1:f5", now - 52, now - 4, {"s1": [-71, -70, -71], "s2": [-79, -81, -80]})
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b, b2)}, configured=[tile_id])
    manager = BermudaTileManager(coord)
    manager.async_update(nowstamp=now)
    assert manager.bindings[tile_id] == [a.address]
    assert manager.ambiguous_handovers == 1 and manager.handovers == 0
    assert manager.last_ambiguity["tile"] == tile_id and len(manager.last_ambiguity["candidates"]) == 2


def test_manager_rejects_a_candidate_that_appeared_too_late_or_too_far():
    now = 10_000.0
    a, b, _ = _house(now)
    b.first_seen = a.last_seen + TILE_HANDOVER_WINDOW + 30       # not a handover: appeared much later
    far = _dev("2a:fe:fc:10:70:fc", now - 50, now - 5, {"s1": [-88, -89, -88], "s2": [-95, -96, -95]})
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b, far)}, configured=[tile_id])
    manager = BermudaTileManager(coord)
    manager.async_update(nowstamp=now)
    assert manager.bindings[tile_id] == [a.address]
    assert manager.handovers == 0 and manager.ambiguous_handovers == 0


def test_bind_is_public_for_an_external_source_of_truth():
    now = 10_000.0
    a, b, _ = _house(now)
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b)}, configured=[tile_id])
    manager = BermudaTileManager(coord)
    manager.bind(tile_id, b.address.upper())
    assert manager.bindings[tile_id] == [b.address]
    assert coord.metadevices[tile_id].metadevice_sources[0] == b.address


def test_bindings_persist_and_restore():
    now = 10_000.0
    a, b, _ = _house(now)
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b)}, configured=[tile_id])
    manager = BermudaTileManager(coord)
    saved = {}

    class _FakeStore:
        def async_delay_save(self, data_func, delay):
            saved.update(data_func())

        async def async_load(self):
            return dict(saved)

    manager._store = _FakeStore()
    manager.async_update(nowstamp=now)
    assert saved["bindings"][tile_id] == [b.address, a.address]

    # "Restart": a fresh manager loads the bindings and puts B back as the source.
    coord2 = _Coord({d.address: d for d in (a, b)}, configured=[tile_id])
    manager2 = BermudaTileManager(coord2)
    manager2._store = _FakeStore()
    asyncio.new_event_loop().run_until_complete(manager2.async_load())
    assert manager2.bindings[tile_id] == [b.address, a.address]
    manager2.async_update(nowstamp=now + 1)
    assert coord2.metadevices[tile_id].metadevice_sources[0] == b.address


def test_manager_is_a_no_op_without_tiles():
    coord = _Coord({}, configured=["AA:BB:CC:DD:EE:FF"])
    manager = BermudaTileManager(coord)
    manager.async_update(nowstamp=1.0)
    assert manager.bindings == {} and coord.metadevices == {}


# --- phase 0: capture --------------------------------------------------------- #


def test_tile_capture_reports_each_raw_tile_address():
    now = 10_000.0
    a, b, c = _house(now)
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id])
    coord.tile_manager = BermudaTileManager(coord)
    coord.tile_manager.async_update(nowstamp=now)
    orig = bermuda_tile.monotonic_time_coarse
    bermuda_tile.monotonic_time_coarse = lambda: now
    try:
        rows = tile_capture(coord)
    finally:
        bermuda_tile.monotonic_time_coarse = orig
    by_addr = {r["address"]: r for r in rows}
    assert set(by_addr) == {a.address, b.address, c.address}          # the metadevice itself is not a row
    assert by_addr[a.address]["top_bits"] == "0b00" and by_addr[a.address]["bound_to"] == tile_id
    assert by_addr[b.address]["bound_to"] == tile_id and by_addr[c.address]["bound_to"] is None
    assert by_addr[b.address]["scanners"]["s1"]["hist_rssi"] == [-69, -72, -70]
    assert by_addr[b.address]["last_seen_age"] == 5.0
