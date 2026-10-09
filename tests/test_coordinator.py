"""Tests for BermudaDataUpdateCoordinator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import BDADDR_TYPE_OTHER, SIGNAL_DEVICE_NEW
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator


@pytest.mark.parametrize("prunable_count, quota", [(2, 1), (0, 1), (3, 4), (3, 3), (3, 2), (3, 5)])
def test_prune_devices_quota(prunable_count, quota):
    """Prune the oldest eligible devices up to the quota, preserving tracked devices."""
    devices = {
        "tracked": MagicMock(create_sensor=True, metadevice_sources=[], adverts={}),
        "also_tracked": MagicMock(create_sensor=True, metadevice_sources=[], adverts={}),
    }
    # Insert newest first so the test also checks timestamp ordering.
    for index in reversed(range(prunable_count)):
        devices[f"device_{index}"] = MagicMock(
            create_sensor=False,
            is_scanner=False,
            address_type=BDADDR_TYPE_OTHER,
            last_seen=900 + index,
            metadevice_sources=[],
            adverts={},
        )
    coordinator = SimpleNamespace(
        devices=devices,
        metadevices={},
        scanner_list=[],
        stamp_last_prune=0,
        stamp_redactions_expiry=None,
        irk_manager=MagicMock(),
    )

    # The fork's prune_devices also sweeps long-silent adverts; wire that in.
    coordinator._prune_silent_adverts = lambda now: BermudaDataUpdateCoordinator._prune_silent_adverts(coordinator, now)
    with (
        patch("custom_components.bermuda.coordinator.monotonic_time_coarse", return_value=1000),
        patch("custom_components.bermuda.coordinator.PRUNE_MAX_COUNT", quota),
    ):
        BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)

    prune_count = min(prunable_count, max(0, prunable_count + 2 - quota))
    assert set(devices) == {"tracked", "also_tracked"} | {
        f"device_{index}" for index in range(prune_count, prunable_count)
    }


def test_handle_devreg_malformed_identifier():
    """A malformed device identifier must not crash the devreg handler.

    Regression test: Home Assistant device identifiers are expected to be
    ``(domain, id)`` 2-tuples, but a buggy integration can register a
    malformed one (observed in the wild: a Plejd device whose id string was
    stored as many single-character elements). Bermuda unpacked every
    identifier directly, so such a device raised
    ``ValueError: too many values to unpack`` and broke the entire
    ``device_registry_updated`` handler on every registry change.

    The handler must skip malformed identifiers, still process valid ones,
    and run to completion.
    """
    # A device with a non-Bermuda connection (so we reach the identifier
    # branch), one malformed identifier and one valid Bermuda identifier.
    device_entry = SimpleNamespace(
        connections={("mac", "AA:BB:CC:DD:EE:FF")},
        identifiers={
            ("plejd", "D", "8", "9", "D", "F", "D", "A"),  # malformed: not a 2-tuple
            ("bermuda", "aa:bb:cc:dd:ee:ff"),  # valid (domain, id)
        },
        name_by_user=None,
    )

    # Lightweight stand-in for the coordinator; we invoke the real (unbound)
    # handler with it as ``self`` to avoid setting up the full integration.
    coordinator = SimpleNamespace(
        devices={},
        dr=SimpleNamespace(async_get=lambda device_id: device_entry),
        _scanner_init_pending=False,
        _do_private_device_init=False,
    )

    event = SimpleNamespace(data={"action": "update", "device_id": "malformed-device", "changes": {}})

    # Previously raised ValueError: too many values to unpack (expected 2).
    BermudaDataUpdateCoordinator.handle_devreg_changes(coordinator, event)

    # Reached the end of the identifier branch without raising.
    assert coordinator._scanner_init_pending is True


def test_update_metadevices_copies_source_attributes():
    """update_metadevices must copy name/manufacturer/beacon fields from a
    source device onto its metadevice.

    Regression test: the copy loops used to iterate `source_device.items()`
    and test `val is any([...])`. BermudaDevice never actually populated
    dict storage (state lives entirely in instance attributes), so
    `.items()` was always empty and this whole block was a silent no-op -
    metadevices never picked up their source's name/manufacturer/beacon
    fields through this path. `val is any([...])` was also broken on its
    own (any() returns a bool; that's an identity check against True/False,
    not the intended membership test).
    """
    mock_coordinator = MagicMock()
    mock_coordinator.options = {}
    mock_coordinator.hass_version_min_2025_4 = True

    source = BermudaDevice(address="AA:BB:CC:DD:EE:01", coordinator=mock_coordinator)
    source.name_bt_local_name = "My Beacon"
    source.manufacturer = "Acme Corp"
    source.beacon_major = "1"
    source.beacon_minor = "2"
    source.beacon_uuid = "abc123"

    # A non-MAC, non-iBeacon-shaped address keeps _async_process_address_type
    # from classifying this as an iBeacon metadevice, which would pull in the
    # (unrelated) beacon_unique_id mismatch branch above the code under test.
    metadevice = BermudaDevice(address="test_metadevice", coordinator=mock_coordinator)
    metadevice.metadevice_sources = [source.address]

    coordinator = SimpleNamespace(
        devices={source.address: source},
        metadevices={metadevice.address: metadevice},
        _get_device=lambda address: {source.address: source}.get(address),
        _do_private_device_init=False,
        discover_private_ble_metadevices=lambda: None,
    )

    BermudaDataUpdateCoordinator.update_metadevices(coordinator)

    assert metadevice.name_bt_local_name == "My Beacon"
    assert metadevice.manufacturer == "Acme Corp"
    assert metadevice.beacon_major == "1"
    assert metadevice.beacon_minor == "2"
    assert metadevice.beacon_uuid == "abc123"


def test_update_metadevices_does_not_overwrite_existing_name_fields():
    """The 'not already set to something interesting' fields must not clobber
    an existing metadevice value, while the 'VERY interesting' beacon fields
    always take the source's latest value.
    """
    mock_coordinator = MagicMock()
    mock_coordinator.options = {}
    mock_coordinator.hass_version_min_2025_4 = True

    source = BermudaDevice(address="AA:BB:CC:DD:EE:02", coordinator=mock_coordinator)
    source.manufacturer = "New Manufacturer"
    source.beacon_major = "9"

    metadevice = BermudaDevice(address="test_metadevice_2", coordinator=mock_coordinator)
    metadevice.metadevice_sources = [source.address]
    metadevice.manufacturer = "Existing Manufacturer"
    metadevice.beacon_major = "1"

    coordinator = SimpleNamespace(
        devices={source.address: source},
        metadevices={metadevice.address: metadevice},
        _get_device=lambda address: {source.address: source}.get(address),
        _do_private_device_init=False,
        discover_private_ble_metadevices=lambda: None,
    )

    BermudaDataUpdateCoordinator.update_metadevices(coordinator)

    # manufacturer was already set on the metadevice - must be left alone.
    assert metadevice.manufacturer == "Existing Manufacturer"
    # beacon_major is "VERY interesting" - always takes the source's value.
    assert metadevice.beacon_major == "9"


def test_async_update_data_internal_single_pass_per_device():
    """calculate_data(), area refresh and entity-creation must all happen for
    every device in a single pass over self.devices, with area refresh and
    entity-creation firing only for create_sensor devices.

    Regression test for merging what used to be three separate full passes
    over self.devices (calculate_data for every device, then
    _refresh_area_by_min_distance for create_sensor devices, then the
    entity-creation check for create_sensor devices again) into one. None of
    the three reads another device's state, so they can run together
    per-device; this asserts that behaviour is preserved after the merge.
    """
    tracked = MagicMock()
    tracked.create_sensor = True
    tracked.create_all_done = False
    tracked.name = "Tracked Device"

    untracked = MagicMock()
    untracked.create_sensor = False
    untracked.create_all_done = False
    untracked.name = "Untracked Device"

    coordinator = SimpleNamespace(
        devices={"tracked": tracked, "untracked": untracked},
        options={},
        hass=MagicMock(),
        update_in_progress=False,
        _waitingfor_load_manufacturer_ids=False,
        stamp_last_update_started=0,
        stamp_last_update=0,
        last_update_success=False,
        _async_gather_advert_data=lambda: True,
        update_metadevices=lambda: None,
        _get_or_create_device=lambda addr: None,
        _seed_configured_devices_done=False,
        prune_devices=lambda: None,
        _refresh_area_by_min_distance=MagicMock(),
    )

    with patch("custom_components.bermuda.coordinator.async_dispatcher_send") as mock_dispatch:
        BermudaDataUpdateCoordinator._async_update_data_internal(coordinator)

    # Every device gets its data recalculated, regardless of create_sensor.
    tracked.calculate_data.assert_called_once()
    untracked.calculate_data.assert_called_once()

    # Only the create_sensor device gets an area refresh...
    coordinator._refresh_area_by_min_distance.assert_called_once_with(tracked)

    # ...and only the create_sensor device fires the new-entity signal.
    mock_dispatch.assert_called_once_with(coordinator.hass, SIGNAL_DEVICE_NEW, "tracked")

    assert coordinator.last_update_success is True


def test_prune_devices_tolerates_duplicate_prune_entries(monkeypatch):
    """A device listed twice in prune_list must not crash the update cycle.

    Regression test: ``prune_list`` is appended to from three independent
    places (the metadevice-source sweep, the main device sweep, and the quota
    top-up) whose selections overlap. A stale IRK source older than
    PRUNE_TIME_KNOWN_IRK (960s) satisfies both the metadevice sweep's
    ``last_seen > stamp_known_irk`` test and the main sweep's
    ``last_seen < stamp_unknown_irk`` (240s) test, so it is appended by each.

    The prune loop then used ``del self.devices[addr]``, so the second delete
    raised ``KeyError`` which propagated out of ``prune_devices`` and aborted
    the whole coordinator refresh ("Unexpected error fetching bermuda data").
    Observed in the wild on a 60-proxy install.
    """
    import custom_components.bermuda.coordinator as coordinator_module
    from custom_components.bermuda.const import BDADDR_TYPE_RANDOM_RESOLVABLE

    # A last_seen of 0 is only 960 s stale once the host has been up that
    # long; pin the clock so the test does not depend on container uptime.
    monotonic_time_coarse = lambda: 5000.0  # noqa: E731
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", monotonic_time_coarse)

    stale_irk = "73:ec:0e:56:42:9e"
    fresh_irk = "73:ec:0e:56:42:01"

    class _Dev(SimpleNamespace):
        """Device stub. Hashable, as the real BermudaDevice is."""

        def __hash__(self):
            return hash(self.address)

    # Old enough to trip BOTH the known-IRK (960s) and unknown-IRK (240s)
    # staleness tests, which is what produces the duplicate append.
    stale_device = _Dev(
        address=stale_irk,
        name="stale irk source",
        last_seen=0,
        create_sensor=False,
        is_scanner=False,
        address_type=BDADDR_TYPE_RANDOM_RESOLVABLE,
        metadevice_sources=[],
        adverts={},
    )
    # The metadevice sweep unconditionally keeps index 0, so a second, stale
    # source is required to reach the duplicate-append path.
    fresh_device = _Dev(
        address=fresh_irk,
        name="current irk source",
        last_seen=monotonic_time_coarse(),
        create_sensor=False,
        is_scanner=False,
        address_type=BDADDR_TYPE_RANDOM_RESOLVABLE,
        metadevice_sources=[],
        adverts={},
    )
    metadevice = SimpleNamespace(metadevice_sources=[fresh_irk, stale_irk], adverts={})

    devices = {fresh_irk: fresh_device, stale_irk: stale_device}

    coordinator = SimpleNamespace(
        devices=devices,
        metadevices={"irk-meta": metadevice},
        scanner_list=[],
        stamp_last_prune=0,
        redactions={},
        stamp_redactions_expiry=None,
        irk_manager=SimpleNamespace(async_prune=lambda: None),
        _get_device=lambda address: devices.get(address),
    )
    coordinator._prune_silent_adverts = lambda now: BermudaDataUpdateCoordinator._prune_silent_adverts(coordinator, now)

    # Previously raised KeyError on the second delete of the same address.
    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)

    # Pruned exactly once, the run completed, and the current source survived.
    assert stale_irk not in coordinator.devices
    assert fresh_irk in coordinator.devices


def test_prune_silent_adverts_drops_only_long_dead_ones():
    """Adverts that timed out, hold no history and have not been heard for
    PRUNE_TIME_ADVERT are dropped - from the source device and from the
    metadevice that copied the same object, so neither hands it back."""
    from custom_components.bermuda.const import PRUNE_TIME_ADVERT

    now = 100_000.0
    old = now - PRUNE_TIME_ADVERT - 1

    def ad(stamp, distance=None, hist=(), new_stamp=None):
        return SimpleNamespace(
            stamp=stamp, rssi_distance=distance, hist_distance_by_interval=list(hist), new_stamp=new_stamp
        )

    dead = ad(old)
    shared_dead = ad(old)
    recent_silent = ad(now - 30)  # timed out, but only just: kept
    live = ad(old, distance=2.5)  # still has a distance
    with_history = ad(old, hist=[3.0])  # history not yet cleared
    pending = ad(old, new_stamp=now)  # an update waiting to be processed
    never_stamped = ad(None)

    source = SimpleNamespace(
        adverts={
            ("a", "s1"): dead,
            ("a", "s2"): shared_dead,
            ("a", "s3"): recent_silent,
            ("a", "s4"): live,
            ("a", "s5"): with_history,
            ("a", "s6"): pending,
            ("a", "s7"): never_stamped,
        }
    )
    metadevice = SimpleNamespace(adverts={("a", "s2"): shared_dead, ("a", "s4"): live})
    empty = SimpleNamespace(adverts={})
    coordinator = SimpleNamespace(devices={"a": source, "meta": metadevice, "e": empty})

    dropped = BermudaDataUpdateCoordinator._prune_silent_adverts(coordinator, now)

    assert dropped == 4
    assert set(source.adverts) == {("a", "s3"), ("a", "s4"), ("a", "s5"), ("a", "s6")}
    assert set(metadevice.adverts) == {("a", "s4")}, "the shared dead advert goes from the metadevice too"


def _scanner_coordinator(scanners, known):
    """A stand-in coordinator whose bluetooth manager reports `scanners`."""
    devices = {}

    def _get_or_create_device(address):
        return devices.setdefault(address, MagicMock(area_id="kitchen", address=address))

    return SimpleNamespace(
        _manager=SimpleNamespace(async_current_scanners=lambda: set(scanners)),
        _hascanners=set(known),
        _scanner_init_pending=False,
        _async_purge_removed_scanners=MagicMock(),
        _async_manage_repair_scanners_without_areas=MagicMock(),
        _get_or_create_device=_get_or_create_device,
        devices=devices,
    )


def test_rebuild_scanner_list_force_rereads_an_unchanged_set():
    """force=True must do its work even when no scanner has come or gone.

    The early return for an unchanged scanner set ran before `force` was
    looked at, so a proxy's new area was not read until a reload
    (agittins/bermuda#856).
    """
    scanner = MagicMock(source="AA:BB:CC:DD:EE:01")
    coordinator = _scanner_coordinator({scanner}, known={scanner})

    BermudaDataUpdateCoordinator._rebuild_scanner_list(coordinator)
    assert not coordinator.devices  # unchanged and not forced: quick exit
    coordinator._async_manage_repair_scanners_without_areas.assert_not_called()

    BermudaDataUpdateCoordinator._rebuild_scanner_list(coordinator, force=True)
    (device,) = coordinator.devices.values()
    device.async_as_scanner_init.assert_called_once_with(scanner, force=True)
    # ...and the "scanners without an area" repair is re-evaluated.
    coordinator._async_manage_repair_scanners_without_areas.assert_called_once_with([])


def test_rebuild_scanner_list_still_notices_a_new_scanner_unforced():
    old, new = MagicMock(source="AA:BB:CC:DD:EE:01"), MagicMock(source="AA:BB:CC:DD:EE:02")
    coordinator = _scanner_coordinator({old, new}, known={old})

    BermudaDataUpdateCoordinator._rebuild_scanner_list(coordinator)

    assert len(coordinator.devices) == 2
    for device in coordinator.devices.values():
        assert device.async_as_scanner_init.call_args.kwargs == {"force": False}


def test_refresh_scanners_serves_a_pending_request_once():
    """A pending request forces one rebuild and is then cleared.

    It used to stay set for good. That was harmless only while `force` was
    ignored; honoured, it would re-read every scanner on every cycle.
    """
    calls = []
    coordinator = SimpleNamespace(
        _scanner_init_pending=True,
        _rebuild_scanner_list=lambda force=False: calls.append(force),
    )

    BermudaDataUpdateCoordinator._refresh_scanners(coordinator)
    BermudaDataUpdateCoordinator._refresh_scanners(coordinator)
    BermudaDataUpdateCoordinator._refresh_scanners(coordinator, force=True)

    assert calls == [True, False, True]
    assert coordinator._scanner_init_pending is False
