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
    CONF_TILE_PROBES,
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
    service_data={},  # measured: this hardware carries no service data at all
    service_uuids=[TILE_SERVICE_UUID],
    tx_power=None,
    rssi=-71,
    platform_data=(),
)
PLAIN_ADVERT = AdvertisementData(
    local_name="not a tile",
    manufacturer_data={},
    service_data={},
    service_uuids=["0000180f-0000-1000-8000-00805f9b34fb"],
    tx_power=None,
    rssi=-60,
    platform_data=(),
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
    assert device.address == "tile_2910b2bca85a"  # mac_norm leaves it alone but lower-cases
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
            scanner_address=scanner,
            hist_rssi=list(hist),
            rssi=hist[0],
            name=scanner,
            hist_stamp=[last],
            service_data=[],
            manufacturer_data=[],
            local_name=[],
            service_uuids=[TILE_SERVICE_UUID],
        )
    return SimpleNamespace(
        address=address,
        is_tile=is_tile,
        first_seen=first,
        last_seen=last,
        adverts=adverts,
        metadevice_sources=[],
        metadevice_type=set(),
        name=address,
        create_sensor=False,
        address_type="bd_addr_random_unresolvable",
        ref_power=-53.0,
    )


class _Coord:
    """The slice of the coordinator the manager touches."""

    def __init__(self, devices, configured=()):
        self.devices = dict(devices)
        self.metadevices = {}
        self.options = {CONF_DEVICES: list(configured), CONF_TILE_PROBES: True}
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
    assert manager.bindings[tile_id] == [b.address, a.address]  # B re-bound, A kept as history
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
    b.first_seen = a.last_seen + TILE_HANDOVER_WINDOW + 30  # not a handover: appeared much later
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
    assert set(by_addr) == {a.address, b.address, c.address}  # the metadevice itself is not a row
    assert by_addr[a.address]["top_bits"] == "0b00" and by_addr[a.address]["bound_to"] == tile_id
    assert by_addr[b.address]["bound_to"] == tile_id and by_addr[c.address]["bound_to"] is None
    assert by_addr[b.address]["scanners"]["s1"]["hist_rssi"] == [-69, -72, -70]
    assert by_addr[b.address]["last_seen_age"] == 5.0


# --- phase 3: identity over GATT --------------------------------------------- #


class _Hass:
    """Just enough hass for the probe worker: tasks run on the test loop."""

    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self.tasks.append(task)
        return task


def _probing_house(now=10_000.0):
    """Like _house, but B and C have the SAME RSSI pattern as A, so the
    heuristic alone would refuse (ambiguous) - only identity can decide."""
    a = _dev("29:10:b2:bc:a8:5a", now - 3600, now - 60, {"s1": [-70, -71, -70], "s2": [-80, -79, -81]})
    b = _dev("17:31:c5:0f:0c:9e", now - 55, now - 5, {"s1": [-69, -72, -70], "s2": [-78, -80, -79]})
    c = _dev("2a:fe:fc:10:70:fc", now - 50, now - 5, {"s1": [-70, -70, -70], "s2": [-79, -80, -80]})
    return a, b, c


def test_identity_resolves_a_rotation_the_heuristic_cannot():
    async def scenario():
        now = 10_000.0
        a, b, c = _probing_house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        uids = {a.address: "cafe01", b.address: "beef02", c.address: "cafe01"}
        probed = []

        async def fake_probe(hass, address):
            probed.append(address)
            return uids[address]

        manager._probe_fn = fake_probe
        manager.bindings[tile_id] = [a.address]

        manager.async_update(nowstamp=now)  # A's id is read first, then both candidates are asked
        await asyncio.gather(*coord.hass.tasks)
        assert manager.uids[tile_id] == "cafe01" and probed[0] == a.address
        assert set(probed) == {a.address, b.address, c.address}

        manager.async_update(nowstamp=now + 1)
        await asyncio.gather(*coord.hass.tasks)
        # C answered with our id: bound by identity.
        assert manager.bindings[tile_id][0] == c.address
        assert manager.last_handover["reason"] == "tile id"
        assert manager.ambiguous_handovers == 0
        diag = manager.diagnostics()
        assert diag["probes"] == 3 and diag["probe_failures"] == 0 and diag["uids"] == {tile_id: "cafe01"}

    asyncio.run(scenario())


def test_heuristic_is_the_fallback_when_nothing_can_be_read():
    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        manager.uids[tile_id] = "cafe01"  # known id, but no proxy can connect right now

        async def unavailable(hass, address):
            raise bermuda_tile.TileProbeUnavailableError("no connectable scanner")

        manager._probe_fn = unavailable
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)  # probes queued, undecided this cycle
        assert manager.bindings[tile_id][0] == a.address
        await asyncio.gather(*coord.hass.tasks)
        manager.async_update(nowstamp=now + 1)  # every candidate unreachable: RSSI heuristic decides
        assert manager.bindings[tile_id][0] == b.address
        assert manager.last_handover["reason"] == "rssi pattern"
        assert manager.diagnostics()["probe_failures"] == 2

    asyncio.run(scenario())


def test_a_tile_without_an_id_characteristic_is_never_a_successor():
    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        manager.uids[tile_id] = "cafe01"

        async def fake_probe(hass, address):
            return None if address == b.address else "other"

        manager._probe_fn = fake_probe
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)
        await asyncio.gather(*coord.hass.tasks)
        manager.async_update(nowstamp=now + 1)
        # B would have won the RSSI heuristic, but it has no id (it does not
        # rotate) and C is someone else's Tile: nothing binds.
        assert manager.bindings[tile_id][0] == a.address
        assert manager.handovers == 0

    asyncio.run(scenario())


def test_probe_results_are_not_repeated_and_failures_retry_later(monkeypatch):
    async def scenario():
        clock = {"t": 10_000.0}
        monkeypatch.setattr(bermuda_tile, "monotonic_time_coarse", lambda: clock["t"])
        dev = _dev("aa:00:00:00:00:01", clock["t"] - 100, clock["t"], {})
        coord = _Coord({dev.address: dev}, configured=[])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        calls = []

        async def flaky(hass, address):
            calls.append(address)
            if len(calls) == 1:
                raise OSError("boom")
            return "cafe01"

        manager._probe_fn = flaky
        manager._request_probe("aa:00:00:00:00:01")
        await asyncio.gather(*coord.hass.tasks)
        manager._request_probe("aa:00:00:00:00:01")  # failed 0 s ago: not retried yet
        assert calls == ["aa:00:00:00:00:01"]
        clock["t"] += bermuda_tile.TILE_PROBE_RETRY_SECS + 1
        manager._request_probe("aa:00:00:00:00:01")  # retry due, but not heard for 2 min: no probe
        assert len(calls) == 1
        dev.last_seen = clock["t"]  # heard again
        manager._request_probe("aa:00:00:00:00:01")
        await asyncio.gather(*coord.hass.tasks)
        assert len(calls) == 2
        manager._request_probe("aa:00:00:00:00:01")  # answered: never asked again
        assert len(calls) == 2 and manager._probes["aa:00:00:00:00:01"]["uid"] == "cafe01"

    asyncio.run(scenario())


def test_a_heuristic_handover_forgets_a_no_id_answer_so_the_new_address_is_asked():
    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        asked = []

        async def probe(hass, address):
            asked.append(address)
            if address == a.address:
                raise bermuda_tile.TileNoIdCharacteristicError("feed[0018,0019]")
            return "cafe01"

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)  # learns: "no id" from A; B and C asked as candidates
        await asyncio.gather(*coord.hass.tasks)
        assert manager.uids[tile_id] == "" and manager._probes[a.address]["error"] is None
        assert manager.last_probe["address"] in (b.address, c.address)
        assert manager._probes[a.address]["uid"] is None and asked[0] == a.address
        manager.async_update(nowstamp=now + 1)  # A quiet: heuristic binds B, whose answer becomes the Tile's id
        assert manager.bindings[tile_id][0] == b.address and manager.uids[tile_id] == "cafe01"
        manager.async_update(nowstamp=now + 2)  # nothing left to ask
        await asyncio.gather(*coord.hass.tasks)
        assert sorted(asked) == sorted([a.address, b.address, c.address])

    asyncio.run(scenario())


def test_a_tile_whose_id_was_never_read_still_hands_over_by_rssi():
    """Regression: with probing possible but the ID unknown, the manager used
    to wait for a read of the bound address - which had rotated away, so the
    read could never happen and the Tile was lost for good."""

    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()

        async def unavailable(hass, address):
            raise bermuda_tile.TileProbeUnavailableError("no connectable scanner")

        manager._probe_fn = unavailable
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)
        await asyncio.gather(*coord.hass.tasks)
        manager.async_update(nowstamp=now + 1)
        assert manager.bindings[tile_id][0] == b.address
        assert manager.last_handover["reason"] == "rssi pattern"
        assert tile_id not in manager.uids
        assert manager.diagnostics()["bound_age"][tile_id] is not None

    asyncio.run(scenario())


def test_the_successor_lends_its_id_to_a_tile_that_never_answered():
    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()

        async def probe(hass, address):
            if address == a.address:
                raise bermuda_tile.TileProbeUnavailableError("gone")
            return {b.address: "beef02", c.address: "cafe01"}[address]

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)
        await asyncio.gather(*coord.hass.tasks)
        manager.async_update(nowstamp=now + 1)  # RSSI picks B; B's answer is now this Tile's id
        assert manager.bindings[tile_id][0] == b.address
        assert manager.uids[tile_id] == "beef02"
        assert manager.diagnostics()["probe_results"][b.address]["uid"] == "beef02"

    asyncio.run(scenario())


def test_an_address_no_longer_heard_is_never_probed():
    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        a.last_seen = now - 1000  # rotated away long ago
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        asked = []

        async def probe(hass, address):
            asked.append(address)
            raise bermuda_tile.TileProbeUnavailableError("gone")

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)
        await asyncio.gather(*coord.hass.tasks)
        assert a.address not in asked  # nothing to connect to
        assert manager.diagnostics()["probes"] == len(asked)

    asyncio.run(scenario())


def test_an_orphaned_tile_is_recovered_by_identity():
    """The bound address rotated while Bermuda was down: no window, no
    heuristic - every live Tile is asked, and the one with our ID is bound."""

    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        b.first_seen = c.first_seen = now - 3000  # long-lived: not handover candidates
        coord = _Coord({d.address: d for d in (b, c)}, configured=[tile_id.upper()])  # A never heard
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        manager.uids[tile_id] = "cafe01"

        async def probe(hass, address):
            return {b.address: "beef02", c.address: "cafe01"}[address]

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)  # just started: not declared gone yet
        assert not coord.hass.tasks
        later = now + bermuda_tile.TILE_SILENT_SECS + 1
        b.last_seen = c.last_seen = later
        manager.async_update(nowstamp=later)  # orphan: both live Tiles asked
        await asyncio.gather(*coord.hass.tasks)
        manager.async_update(nowstamp=later + 1)
        assert manager.bindings[tile_id][0] == c.address
        # bound from inside the probe answer, or by the orphan sweep on the next cycle
        assert manager.last_handover["reason"] in ("tile id", "tile id (recovered)")

    asyncio.run(scenario())


def test_an_orphaned_tile_with_an_unknown_id_learns_its_neighbours_but_binds_nothing():
    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()

        async def probe(hass, address):
            return {b.address: "beef02", c.address: "cafe01"}[address]

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        later = now + bermuda_tile.TILE_SILENT_SECS + 1
        manager.async_update(nowstamp=now)
        b.last_seen = c.last_seen = later
        manager.async_update(nowstamp=later)
        await asyncio.gather(*coord.hass.tasks)
        manager.async_update(nowstamp=later + 1)
        assert manager.bindings[tile_id][0] == a.address  # nothing to compare against
        results = manager.diagnostics()["probe_results"]
        assert results[b.address]["uid"] == "beef02" and results[c.address]["uid"] == "cafe01"
        assert manager.diagnostics()["bound_age"][tile_id] is None

    asyncio.run(scenario())


# --- TDI over the MEP channel, post-probe rotation, connection budget -------- #


class _MepClient:
    """A bleak client stub with only the feed service's MEP characteristics."""

    def __init__(self, reply=None, *, echo_cid=True):
        self.reply, self.echo_cid = reply, echo_cid
        self.writes, self.notify_cb, self.stopped = [], None, False

    async def start_notify(self, uuid, cb):
        assert uuid == bermuda_tile.MEP_RSP_UUID
        self.notify_cb = cb

    async def stop_notify(self, uuid):
        self.stopped = True

    async def write_gatt_char(self, uuid, data, response=True):
        assert uuid == bermuda_tile.MEP_CMD_UUID and response is False
        self.writes.append(bytes(data))
        if self.reply is None:
            return
        cid = bytes(data[1:5]) if self.echo_cid else b"\xff\xff\xff\xff"
        # somebody else's channel first, then ours
        self.notify_cb(None, bytearray(b"\x00\x01\x02\x03\x04" + bytes([bermuda_tile.TOA_RSP_TDI]) + b"\x02junk"))
        self.notify_cb(None, bytearray(b"\x00" + cid + bytes([bermuda_tile.TOA_RSP_TDI]) + self.reply))


def test_tdi_read_frames_the_request_and_parses_the_tile_id():
    async def scenario():
        client = _MepClient(b"\x02" + bytes.fromhex("0011223344556677"))
        uid = await bermuda_tile._read_uid_over_mep(client)
        assert uid == "0011223344556677"
        (req,) = client.writes
        assert (
            req[0] == 0
            and len(req) == 7
            and req[5] == bermuda_tile.TOA_CMD_TDI
            and req[6] == bermuda_tile.TDI_READ_TILE_ID
        )
        assert client.stopped
        # the broadcast connectionless id is accepted too
        client = _MepClient(b"\x02" + bytes.fromhex("8877665544332211"), echo_cid=False)
        assert await bermuda_tile._read_uid_over_mep(client) == "8877665544332211"

    asyncio.run(scenario())


def test_tdi_read_reports_no_id_and_silence():
    async def scenario():
        with pytest.raises(bermuda_tile.TileNoIdCharacteristicError):
            await bermuda_tile._read_uid_over_mep(_MepClient(bytes([bermuda_tile.TDI_ERROR, 1])))
        with pytest.raises(asyncio.TimeoutError):
            await bermuda_tile._read_uid_over_mep(_MepClient(None), timeout=0.05)

    asyncio.run(scenario())


def test_the_address_a_tile_switches_to_after_a_probe_inherits_the_answer(monkeypatch):
    """Connecting makes a Tile rotate; the new address must not be connected
    to again (that would rotate it again, forever) - it inherits the answer."""

    async def scenario():
        clock = {"t": 10_000.0}
        monkeypatch.setattr(bermuda_tile, "monotonic_time_coarse", lambda: clock["t"])
        now = clock["t"]
        a, b, c = _house(now)  # B matches A's RSSI pattern, C does not
        a.last_seen = now - 1
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        asked = []

        async def probe(hass, address):
            asked.append(address)
            return {a.address: "cafe01", c.address: "beef02"}[address]

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        manager._request_probe(a.address, learn_for=tile_id, nowstamp=now)
        await asyncio.gather(*coord.hass.tasks)
        assert asked == [a.address] and manager.uids[tile_id] == "cafe01"
        # A goes quiet and B appears 3 s after the probe answered, where A was.
        clock["t"] = now + 3
        b.first_seen = b.last_seen = clock["t"]
        c.first_seen = c.last_seen = clock["t"]
        manager._request_probe(b.address, nowstamp=clock["t"])
        manager._request_probe(c.address, nowstamp=clock["t"])
        await asyncio.gather(*coord.hass.tasks)
        assert asked == [a.address, c.address]  # B inherited, C (a different pattern) was asked
        assert manager._probes[b.address]["inherited_from"] == a.address
        assert manager.bindings[tile_id][0] == b.address  # inherited id == ours: bound at once
        diag = manager.diagnostics()
        assert diag["probes_inherited"] == 1 and diag["probe_results"][b.address]["inherited_from"] == a.address

    asyncio.run(scenario())


def test_the_connection_budget_caps_probing(monkeypatch):
    async def scenario():
        clock = {"t": 10_000.0}
        monkeypatch.setattr(bermuda_tile, "monotonic_time_coarse", lambda: clock["t"])
        devices = {}
        for i in range(bermuda_tile.TILE_PROBE_BUDGET + 3):
            d = _dev(
                f"aa:00:00:00:00:{i:02x}",
                clock["t"] - 100,
                clock["t"],
                {"s1": [-60 - i, -60 - i, -60 - i], "s2": [-90, -90, -90]},
            )
            devices[d.address] = d
        coord = _Coord(devices, configured=[])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        asked = []

        async def probe(hass, address):
            asked.append(address)
            raise bermuda_tile.TileProbeUnavailableError("busy")

        manager._probe_fn = probe
        for address in devices:
            manager._request_probe(address, nowstamp=clock["t"])
        await asyncio.gather(*coord.hass.tasks)
        assert len(asked) == bermuda_tile.TILE_PROBE_BUDGET
        assert manager.diagnostics()["probe_budget_left"] == 0
        clock["t"] += bermuda_tile.TILE_PROBE_BUDGET_SECS + 1  # the hour passes: the budget refills
        for d in devices.values():
            d.last_seen = clock["t"]
        manager._request_probe(list(devices)[-1], nowstamp=clock["t"])
        await asyncio.gather(*coord.hass.tasks)
        assert len(asked) == bermuda_tile.TILE_PROBE_BUDGET + 1

    asyncio.run(scenario())


# --- identities, binding by ID, sweep pacing ---------------------------------- #


def test_identities_and_bind_by_uid(monkeypatch):
    async def scenario():
        clock = {"t": 10_000.0}
        monkeypatch.setattr(bermuda_tile, "monotonic_time_coarse", lambda: clock["t"])
        now = clock["t"]
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({d.address: d for d in (b, c)}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()

        async def probe(hass, address):
            return {b.address: "beef02", c.address: "cafe01"}[address]

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)
        later = now + bermuda_tile.TILE_SILENT_SECS + 1
        clock["t"] = later
        b.last_seen = c.last_seen = later
        manager.async_update(nowstamp=later)  # orphan sweep: both asked
        await asyncio.gather(*coord.hass.tasks)
        ids = manager.identities()
        assert set(ids) == {"beef02", "cafe01"}
        assert ids["cafe01"]["addresses"] == [c.address] and ids["cafe01"]["tile_id"] is None
        assert ids["cafe01"]["strongest"]["scanner"] == "s2"  # C is loudest on s2 (-60)
        assert ids["beef02"]["strongest"]["scanner"] == "s1"
        # The user says: my Tile is cafe01.
        assert manager.bind_by_uid(tile_id.upper(), "CAFE01") == c.address
        assert manager.bindings[tile_id][0] == c.address and manager.uids[tile_id] == "cafe01"
        assert manager.last_handover["reason"] == "tile id (user)"
        assert manager.identities()["cafe01"]["tile_id"] == tile_id
        with pytest.raises(ValueError):
            manager.bind_by_uid("tile_000000000000", "cafe01")  # not a configured Tile
        other = "tile_aaaaaaaaaaaa"
        coord.options[CONF_DEVICES].append(other.upper())
        with pytest.raises(ValueError):
            manager.bind_by_uid(other, "cafe01")  # already declared as ours
        assert manager.bind_by_uid(other, "deadbeef00000000") is None  # remembered, nothing live carries it

    asyncio.run(scenario())


def test_orphan_sweeps_are_paced():
    async def scenario():
        now = 10_000.0
        a, b, c = _house(now)
        tile_id = tile_metadevice_id(a.address)
        coord = _Coord({b.address: b}, configured=[tile_id.upper()])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        asked = []

        async def probe(hass, address):
            asked.append(address)
            return "beef02"

        manager._probe_fn = probe
        manager.bindings[tile_id] = [a.address]
        manager.async_update(nowstamp=now)
        t1 = now + bermuda_tile.TILE_SILENT_SECS + 1
        b.last_seen = t1
        manager.async_update(nowstamp=t1)  # first sweep asks B
        await asyncio.gather(*coord.hass.tasks)
        assert asked == [b.address]
        coord.devices[c.address] = c  # a new Tile shows up a minute later
        t2 = t1 + 60
        b.last_seen = c.last_seen = t2
        manager.async_update(nowstamp=t2)
        await asyncio.gather(*coord.hass.tasks)
        assert asked == [b.address]  # not asked: the sweep ran a minute ago
        t3 = t1 + bermuda_tile.TILE_ORPHAN_SWEEP_SECS + 1
        b.last_seen = c.last_seen = t3
        manager.async_update(nowstamp=t3)
        await asyncio.gather(*coord.hass.tasks)
        assert asked == [b.address, c.address]  # next pass: only the unanswered one

    asyncio.run(scenario())


def test_a_known_id_follows_a_natural_rotation_without_a_connection(monkeypatch):
    async def scenario():
        clock = {"t": 10_000.0}
        monkeypatch.setattr(bermuda_tile, "monotonic_time_coarse", lambda: clock["t"])
        now = clock["t"]
        a, b, c = _house(now)  # B continues A's RSSI pattern; C does not
        a.last_seen = now - 1
        coord = _Coord({a.address: a}, configured=[])
        manager = BermudaTileManager(coord)
        coord.hass = manager._hass = _Hass()
        asked = []

        async def probe(hass, address):
            asked.append(address)
            return "cafe01"

        manager._probe_fn = probe
        manager._request_probe(a.address, nowstamp=now)
        await asyncio.gather(*coord.hass.tasks)
        assert asked == [a.address]
        # Forty minutes later A goes quiet and B appears where A was: a natural rotation, long after the probe.
        later = now + 2400
        a.last_seen = later - 8
        b.first_seen = later - 3
        b.last_seen = later
        c.first_seen = later - 3
        c.last_seen = later
        coord.devices[b.address] = b
        coord.devices[c.address] = c
        clock["t"] = later
        manager.async_update(nowstamp=later)
        await asyncio.gather(*coord.hass.tasks)
        assert asked == [a.address]  # no new connection
        assert (
            manager._probes[b.address]["uid"] == "cafe01" and manager._probes[b.address]["inherited_from"] == a.address
        )
        assert c.address not in manager._probes  # a different pattern: not ours
        ids = manager.identities()["cafe01"]
        assert ids["addresses"] == [a.address, b.address] and ids["heard_by"][0]["scanner"] == "s1"

    asyncio.run(scenario())


def test_probes_are_off_unless_the_option_is_on():
    a, b, c = _house()
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (a, b, c)}, configured=[tile_id.upper()])
    coord.options[CONF_TILE_PROBES] = False
    manager = BermudaTileManager(coord)
    coord.hass = manager._hass = _Hass()
    assert not manager._can_probe()
    manager.bindings[tile_id] = [a.address]
    manager.async_update(nowstamp=10_000.0)
    assert not coord.hass.tasks and manager.probes == 0


def test_a_tile_lost_across_a_restart_is_adopted_by_its_remembered_pattern():
    """Bermuda restarts after the bound address rotated: no departed readings
    to compare against, but the persisted pattern says where the Tile was."""
    now = 10_000.0
    a, b, c = _house(now)
    tile_id = tile_metadevice_id(a.address)
    # Before the restart: A bound and heard, its pattern remembered.
    coord = _Coord({a.address: a}, configured=[tile_id.upper()])
    coord.options[CONF_TILE_PROBES] = False
    manager = BermudaTileManager(coord)
    manager.bindings[tile_id] = [a.address]
    a.last_seen = now
    manager.async_update(nowstamp=now)
    assert set(manager.patterns[tile_id]) == {"s1", "s2"}
    saved = manager._data()
    # After the restart: A is gone, B (same readings) and C (different) are live.
    coord2 = _Coord({b.address: b, c.address: c}, configured=[tile_id.upper()])
    coord2.options[CONF_TILE_PROBES] = False
    fresh = BermudaTileManager(coord2)
    fresh.bindings = saved["bindings"]
    fresh.patterns = saved["patterns"]
    t1 = now + 1000
    b.last_seen = c.last_seen = t1
    fresh.async_update(nowstamp=t1)  # just started: not declared gone yet
    assert fresh.bindings[tile_id][0] == a.address
    t2 = t1 + bermuda_tile.TILE_SILENT_SECS + 1
    b.last_seen = c.last_seen = t2
    fresh.async_update(nowstamp=t2)
    assert fresh.bindings[tile_id][0] == b.address
    assert fresh.last_handover["reason"] == "rssi pattern (recovered)"


def test_bind_address_is_the_users_word():
    now = 10_000.0
    a, b, c = _house(now)
    tile_id = tile_metadevice_id(a.address)
    coord = _Coord({d.address: d for d in (b, c)}, configured=[tile_id.upper()])
    manager = BermudaTileManager(coord)
    manager.bindings[tile_id] = [a.address]
    assert manager.bind_address(tile_id.upper(), c.address.upper()) == c.address
    assert manager.bindings[tile_id][0] == c.address and manager.last_handover["reason"] == "user"
    assert abs(manager.patterns[tile_id]["s2"] + 60.33) < 0.1  # C reads -60/-61/-60 on s2
    with pytest.raises(ValueError):
        manager.bind_address("tile_000000000000", c.address)
    with pytest.raises(ValueError):
        manager.bind_address(tile_id, "ff:ff:ff:ff:ff:ff")


def test_bindings_of_untracked_tiles_are_forgotten():
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
    assert tile_id in manager.bindings and tile_id in coord.metadevices
    # The user untracks it: the binding, pattern, ID and metadevice go, and the store is told.
    coord.options[CONF_DEVICES] = []
    manager.patterns[tile_id] = {"s1": -60.0}
    manager.uids[tile_id] = "abc"
    manager.async_update(nowstamp=now + 1)
    assert manager.bindings == {} and manager.patterns == {} and manager.uids == {}
    assert tile_id not in coord.metadevices
    assert saved["bindings"] == {}
