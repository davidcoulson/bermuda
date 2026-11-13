"""Stale iBeacons nobody tracks must be pruned."""

from __future__ import annotations

from types import SimpleNamespace

from bluetooth_data_tools import monotonic_time_coarse

from custom_components.bermuda.const import BDADDR_TYPE_NOT_MAC48, METADEVICE_IBEACON_DEVICE, PRUNE_TIME_DEFAULT
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator


def _beacon(address, last_seen, tracked=False):
    md = SimpleNamespace(
        address=address,
        metadevice_type={METADEVICE_IBEACON_DEVICE},
        create_sensor=tracked,
        last_seen=last_seen,
        metadevice_sources=["11:22:33:44:55:66"],
        address_type=BDADDR_TYPE_NOT_MAC48,
        is_scanner=False,
        adverts={},
    )
    return md


def test_stale_untracked_ibeacons_are_pruned():
    now = monotonic_time_coarse()
    stale = _beacon("aaaa_1_1", now - PRUNE_TIME_DEFAULT - 10)
    fresh = _beacon("bbbb_1_1", now - 10)
    tracked = _beacon("cccc_1_1", now - PRUNE_TIME_DEFAULT - 10, tracked=True)
    devices = {d.address: d for d in (stale, fresh, tracked)}
    coordinator = SimpleNamespace(
        stamp_last_prune=0,
        stamp_redactions_expiry=None,
        redactions={},
        irk_manager=SimpleNamespace(async_prune=lambda: None),
        metadevices=dict(devices),
        devices=dict(devices),
        scanner_list=[],
        _get_device=lambda address: None,
    )
    coordinator._prune_silent_adverts = lambda now: BermudaDataUpdateCoordinator._prune_silent_adverts(coordinator, now)
    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)
    assert set(coordinator.metadevices) == {"bbbb_1_1", "cccc_1_1"}
    assert "aaaa_1_1" not in coordinator.devices


def test_untracked_ibeacons_are_evicted_oldest_first_over_quota(monkeypatch):
    monkeypatch.setattr("custom_components.bermuda.coordinator.PRUNE_MAX_COUNT", 4)  # a cap of one untracked beacon
    now = monotonic_time_coarse()
    oldest = _beacon("aaaa_1_1", now - 30)
    newer = _beacon("bbbb_1_1", now - 10)
    tracked = _beacon("cccc_1_1", now - 100, tracked=True)
    oldest.metadevice_sources = ["aa:aa:aa:aa:aa:aa"]
    source = _beacon("aa:aa:aa:aa:aa:aa", now - 30)
    source.metadevice_type, source.metadevice_sources = set(), []
    devices = {d.address: d for d in (oldest, newer, tracked)}
    coordinator = SimpleNamespace(
        stamp_last_prune=0,
        stamp_redactions_expiry=None,
        redactions={},
        irk_manager=SimpleNamespace(async_prune=lambda: None),
        metadevices=dict(devices),
        devices={**devices, source.address: source},
        scanner_list=[],
        _get_device=lambda address: None,
    )
    coordinator._prune_silent_adverts = lambda now: BermudaDataUpdateCoordinator._prune_silent_adverts(coordinator, now)
    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)
    assert set(coordinator.metadevices) == {"bbbb_1_1", "cccc_1_1"}
    # The evicted beacon's live source goes with it, or it would never re-register.
    assert "aa:aa:aa:aa:aa:aa" not in coordinator.devices


def test_untracked_ibeacon_keeps_only_a_few_sources_outside_the_quota(monkeypatch):
    monkeypatch.setattr("custom_components.bermuda.coordinator.PRUNE_MAX_BEACON_SOURCES", 2)
    now = monotonic_time_coarse()
    beacon = _beacon("aaaa_1_1", now - 1)
    sources = [_beacon(f"aa:aa:aa:aa:aa:{n:02x}", now - 1 - n) for n in range(5)]  # newest first
    for source in sources:
        source.metadevice_type, source.metadevice_sources = set(), []
    beacon.metadevice_sources = [s.address for s in sources]
    devices = {d.address: d for d in (beacon, *sources)}
    coordinator = SimpleNamespace(
        stamp_last_prune=0,
        stamp_redactions_expiry=None,
        redactions={},
        irk_manager=SimpleNamespace(async_prune=lambda: None),
        metadevices={beacon.address: beacon},
        devices=devices,
        scanner_list=[],
        _get_device=devices.get,
    )
    coordinator._prune_silent_adverts = lambda now: BermudaDataUpdateCoordinator._prune_silent_adverts(coordinator, now)
    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)
    assert set(coordinator.devices) == {"aaaa_1_1", "aa:aa:aa:aa:aa:00", "aa:aa:aa:aa:aa:01"}
    assert beacon.metadevice_sources == ["aa:aa:aa:aa:aa:00", "aa:aa:aa:aa:aa:01"]
