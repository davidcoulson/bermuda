"""Tests for the public advert-snapshot API in bermuda.api."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.util import slugify

from custom_components.bermuda.api import (
    SNAPSHOT_VERSION,
    async_get_advert_snapshot,
    async_get_coordinator,
    async_get_scanner_ranging,
)
from custom_components.bermuda.const import DOMAIN


def _make_hass(coordinator):
    """A hass stand-in whose bermuda config entry carries `coordinator`."""
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry] if domain == DOMAIN else [])
    )


def _make_coordinator():
    scanner_device = SimpleNamespace(last_seen=1000.0)
    advert = SimpleNamespace(
        name="Master Bedroom esp32c5 f17464",
        scanner_address="f1:74:64:00:00:01",
        scanner_device=scanner_device,
        area_id="master_bedroom",
        area_name="Master Bedroom",
        rssi_distance=3.5,
        rssi_distance_raw=4.1,
        rssi=-63,
        stamp=999.0,
    )
    device = SimpleNamespace(
        name="Meg",
        address_type="bd_addr_random_resolvable",
        area_id="catwalk",
        area_name="Catwalk",
        adverts={("aa:bb:cc:dd:ee:ff", "f1:74:64:00:00:01"): advert},
    )
    empty_device = SimpleNamespace(
        name="Nothing Heard",
        address_type="bd_addr_other",
        area_id=None,
        area_name=None,
        adverts={},
    )
    return SimpleNamespace(devices={"aa:bb:cc:dd:ee:ff": device, "11:22:33:44:55:66": empty_device})


def test_async_get_coordinator_returns_none_without_bermuda():
    """No bermuda config entry must be a clean None, not an exception."""
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: []))
    assert async_get_coordinator(hass) is None
    assert async_get_advert_snapshot(hass) is None


def test_snapshot_exposes_per_scanner_readings_without_entities():
    """The whole point: per-scanner distances are available straight from the
    coordinator, with no entity in the state machine involved."""
    hass = _make_hass(_make_coordinator())

    snapshot = async_get_advert_snapshot(hass)

    assert snapshot is not None
    assert snapshot["version"] == SNAPSHOT_VERSION
    device = snapshot["devices"]["aa:bb:cc:dd:ee:ff"]
    assert device["name"] == "Meg"
    assert device["slug"] == "meg"

    scanner = device["scanners"]["f1:74:64:00:00:01"]
    assert scanner["distance"] == 3.5
    assert scanner["distance_raw"] == 4.1
    assert scanner["rssi"] == -63
    assert scanner["area_name"] == "Master Bedroom"
    # age is derived from the advert stamp, so a scanner that has gone quiet
    # ages out even though its last value never changed.
    assert scanner["age"] is not None and scanner["age"] > 0


def test_scanner_slug_tracks_current_name_and_address_is_the_join_key():
    """`slug` is best-effort and `address` is the stable key.

    In the simple case the slug matches Bermuda's ``_distance_to_<slug>``
    entity suffix, which is convenient - but the entity_id is frozen at entity
    creation and does not follow later renames, so consumers must key off the
    scanner address instead.
    """
    hass = _make_hass(_make_coordinator())

    snapshot = async_get_advert_snapshot(hass)
    scanner = snapshot["devices"]["aa:bb:cc:dd:ee:ff"]["scanners"]["f1:74:64:00:00:01"]

    assert scanner["slug"] == slugify("Master Bedroom esp32c5 f17464")
    assert scanner["slug"] == "master_bedroom_esp32c5_f17464"
    # The address is echoed so a consumer iterating values (not items) still
    # has the stable identity to hand.
    assert scanner["address"] == "f1:74:64:00:00:01"


def test_snapshot_exposes_ids_needed_to_join_the_entity_registry():
    """A consumer migrating off entity scraping has to map its stored
    `_distance_to_<slug>` ids to scanners. Bermuda builds those entities'
    unique_ids as f"{device.unique_id}_{scanner.address_wifi_mac or
    scanner.address}_range", so the snapshot must surface both ids - otherwise
    the consumer has to hard-code Bermuda's wifi-mac fallback rule itself.
    """
    coordinator = _make_coordinator()
    scanner_addr = "f1:74:64:00:00:01"
    coordinator.devices[scanner_addr] = SimpleNamespace(
        name="Master Bedroom esp32c5 f17464",
        last_seen=1000.0,
        adverts={},
        address_type="bd_addr_other",
        area_id=None,
        area_name=None,
        # ESPHome proxies report a BLE mac but Bermuda keeps the wifi mac as
        # unique_id for entity-id stability, so these legitimately differ.
        unique_id="aa:11:22:33:44:55",
        address_wifi_mac="aa:11:22:33:44:55",
    )

    snapshot = async_get_advert_snapshot(_make_hass(coordinator))
    device = snapshot["devices"]["aa:bb:cc:dd:ee:ff"]
    scanner = device["scanners"][scanner_addr]

    assert scanner["unique_id"] == "aa:11:22:33:44:55"
    assert scanner["address_wifi_mac"] == "aa:11:22:33:44:55"
    # ...and it is NOT the same as the advert's scanner address, which is the
    # exact trap this field exists to avoid.
    assert scanner["unique_id"] != scanner["address"]
    assert "unique_id" in device


def test_scanner_name_comes_from_the_scanner_device_not_the_advert():
    """advert.name is a copy taken when the advert was created and goes stale
    when the scanner is renamed - Bermuda's own sensor.py deliberately avoids
    it for this reason. The snapshot must report the CURRENT scanner name.

    This is the real-world case where Bermuda appends a MAC to disambiguate two
    scanners that share a name: the device name changes, the advert's cached
    copy does not.
    """
    coordinator = _make_coordinator()
    scanner_addr = "f1:74:64:00:00:01"
    advert = next(iter(coordinator.devices["aa:bb:cc:dd:ee:ff"].adverts.values()))

    # Scanner has since been renamed; the advert still holds the old name.
    coordinator.devices[scanner_addr] = SimpleNamespace(
        name="Master Bedroom RRN00 4e88e0 (DC:06:75:4E:88:E2)",
        last_seen=1000.0,
        # Scanner devices live in coordinator.devices too, and carry their own
        # (usually empty) adverts dict.
        adverts={},
        address_type="bd_addr_other",
        area_id="master_bedroom",
        area_name="Master Bedroom",
    )
    assert advert.name == "Master Bedroom esp32c5 f17464"  # stale copy

    snapshot = async_get_advert_snapshot(_make_hass(coordinator))
    scanner = snapshot["devices"]["aa:bb:cc:dd:ee:ff"]["scanners"][scanner_addr]

    assert scanner["name"] == "Master Bedroom RRN00 4e88e0 (DC:06:75:4E:88:E2)"


def test_snapshot_address_filter_and_empty_devices():
    """`addresses` narrows the result; devices with no adverts are dropped
    unless explicitly requested."""
    hass = _make_hass(_make_coordinator())

    # Empty-advert device excluded by default...
    default = async_get_advert_snapshot(hass)
    assert "11:22:33:44:55:66" not in default["devices"]

    # ...but available on request.
    with_empty = async_get_advert_snapshot(hass, include_empty=True)
    assert "11:22:33:44:55:66" in with_empty["devices"]
    assert with_empty["devices"]["11:22:33:44:55:66"]["scanners"] == {}

    # Address filter is case-insensitive and limits the payload.
    filtered = async_get_advert_snapshot(hass, addresses={"AA:BB:CC:DD:EE:FF"})
    assert set(filtered["devices"]) == {"aa:bb:cc:dd:ee:ff"}


def test_snapshot_is_json_able_primitives():
    """The snapshot must not leak Bermuda's internal objects - that is what
    consumers would otherwise bind to."""
    import json

    hass = _make_hass(_make_coordinator())
    snapshot = async_get_advert_snapshot(hass)

    # Raises TypeError if any internal object leaked through.
    json.dumps(snapshot)


def test_snapshot_tolerates_missing_optional_fields():
    """A device seen but not yet measured (distance None, stamp 0) must not
    break the snapshot - that is a normal transient state in Bermuda."""
    coordinator = _make_coordinator()
    advert = next(iter(coordinator.devices["aa:bb:cc:dd:ee:ff"].adverts.values()))
    advert.rssi_distance = None
    advert.rssi = None
    advert.stamp = 0

    snapshot = async_get_advert_snapshot(_make_hass(coordinator))
    scanner = snapshot["devices"]["aa:bb:cc:dd:ee:ff"]["scanners"]["f1:74:64:00:00:01"]

    assert scanner["distance"] is None
    assert scanner["age"] is None


def test_coordinator_supports_listener_subscription():
    """Push path: consumers subscribe to the coordinator rather than needing a
    bespoke event. Guards against the coordinator losing that contract."""
    from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator

    assert hasattr(BermudaDataUpdateCoordinator, "async_add_listener")
    # sanity: MagicMock spec would accept anything, so assert on the real class
    assert callable(BermudaDataUpdateCoordinator.async_add_listener)
    _ = MagicMock  # keep import used for parity with other test modules


# --- additive features on top of SNAPSHOT_VERSION 1 ------------------------- #


def _make_tracked_coordinator():
    """Two devices with adverts: one tracked (create_sensor) and one not; plus
    two scanners in the coordinator's scanner set, one of which has never
    relayed anything (last_seen 0)."""
    coordinator = _make_coordinator()
    tracked = coordinator.devices["aa:bb:cc:dd:ee:ff"]
    tracked.create_sensor = True
    tracked.unique_id = "aa:bb:cc:dd:ee:ff"
    advert = next(iter(tracked.adverts.values()))
    bystander = SimpleNamespace(
        name="Some Passing Phone",
        address_type="bd_addr_random_resolvable",
        area_id=None,
        area_name=None,
        create_sensor=False,
        adverts={("de:ad:be:ef:00:01", advert.scanner_address): advert},
    )
    coordinator.devices["de:ad:be:ef:00:01"] = bystander
    live_scanner = SimpleNamespace(
        address="f1:74:64:00:00:01",
        name="Master Bedroom esp32c5 f17464",
        unique_id="f1:74:64:00:00:00",
        address_wifi_mac="f1:74:64:00:00:00",
        area_id="master_bedroom",
        area_name="Master Bedroom",
        is_remote_scanner=True,
        last_seen=1000.0,
    )
    silent_scanner = SimpleNamespace(
        address="ab:cd:ef:00:00:02",
        name="Garage Proxy",
        unique_id=None,
        address_wifi_mac=None,
        area_id=None,
        area_name=None,
        is_remote_scanner=True,
        last_seen=0,
    )
    coordinator.get_scanners = [live_scanner, silent_scanner]
    return coordinator


def test_snapshot_features_are_advertised():
    """Consumers feature-detect by name rather than parsing versions."""
    from custom_components.bermuda.api import SNAPSHOT_FEATURES

    assert {"tracked_only", "tracked_devices", "scanners"} <= SNAPSHOT_FEATURES


def test_tracked_only_snapshot_skips_untracked_devices():
    """A consumer that only reads tracked devices must be able to skip the
    (usually far larger) untracked majority before any per-advert work."""
    hass = _make_hass(_make_tracked_coordinator())

    everything = async_get_advert_snapshot(hass)
    tracked_only = async_get_advert_snapshot(hass, tracked_only=True)

    assert set(everything["devices"]) == {"aa:bb:cc:dd:ee:ff", "de:ad:be:ef:00:01"}
    assert set(tracked_only["devices"]) == {"aa:bb:cc:dd:ee:ff"}
    assert tracked_only["devices"]["aa:bb:cc:dd:ee:ff"]["tracked"] is True
    # Same per-device shape either way: the flag only filters. (Ages are
    # clock-relative and differ between the two calls, so compare keys.)
    a = tracked_only["devices"]["aa:bb:cc:dd:ee:ff"]
    b = everything["devices"]["aa:bb:cc:dd:ee:ff"]
    assert a.keys() == b.keys()
    assert a["scanners"].keys() == b["scanners"].keys()
    assert a["scanners"]["f1:74:64:00:00:01"]["distance"] == b["scanners"]["f1:74:64:00:00:01"]["distance"]


def test_tracked_devices_is_a_cheap_membership_view():
    """The tracked-set accessor returns only tracked devices, keyed by address,
    with the same slug the snapshot would report - and no adverts at all."""
    from custom_components.bermuda.api import async_get_tracked_devices

    hass = _make_hass(_make_tracked_coordinator())

    tracked = async_get_tracked_devices(hass)

    assert set(tracked) == {"aa:bb:cc:dd:ee:ff"}
    assert tracked["aa:bb:cc:dd:ee:ff"] == {
        "name": "Meg",
        "slug": "meg",
        "unique_id": "aa:bb:cc:dd:ee:ff",
    }
    assert async_get_tracked_devices(
        SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: []))
    ) is None


def test_scanners_expose_liveness_without_an_advert_walk():
    """Scanner liveness comes from the scanner set itself, so a proxy that is
    alive but hears no tracked device still reads as alive, and one that has
    never relayed anything reads as never seen rather than as age 0."""
    from custom_components.bermuda.api import async_get_scanners

    hass = _make_hass(_make_tracked_coordinator())

    scanners = async_get_scanners(hass)

    live = scanners["f1:74:64:00:00:01"]
    assert live["slug"] == slugify("Master Bedroom esp32c5 f17464")
    assert live["unique_id"] == "f1:74:64:00:00:00"
    assert live["is_remote"] is True
    assert live["last_seen"] == 1000.0
    assert live["last_seen_age"] is not None and live["last_seen_age"] >= 0

    silent = scanners["ab:cd:ef:00:00:02"]
    assert silent["last_seen"] is None
    assert silent["last_seen_age"] is None

    # The same map rides along in the snapshot, so one call serves both needs.
    snapshot = async_get_advert_snapshot(hass, tracked_only=True)
    assert set(snapshot["scanners"]) == {"f1:74:64:00:00:01", "ab:cd:ef:00:00:02"}
    import json

    json.dumps(snapshot)


def test_snapshot_without_a_scanner_set_still_works():
    """A coordinator (or test double) with no scanner set yields an empty
    scanners map rather than an exception."""
    from custom_components.bermuda.api import async_get_scanners

    hass = _make_hass(_make_coordinator())

    assert async_get_advert_snapshot(hass)["scanners"] == {}
    assert async_get_scanners(hass) == {}


def test_history_and_path_loss_parameters_ride_along():
    """A consumer that wants its own estimator gets the raw samples and the
    exact parameters Bermuda used, so its distances land on Bermuda's scale."""
    coordinator = _make_tracked_coordinator()
    advert = next(iter(coordinator.devices["aa:bb:cc:dd:ee:ff"].adverts.values()))
    advert.hist_rssi = [-63, -65, -70, -61]
    advert.hist_stamp = [999.0, 998.0, 996.5, 995.0]
    advert.ref_power = 0          # "use the global option"
    advert.conf_ref_power = -55.0
    advert.conf_attenuation = 3.0
    advert.conf_rssi_offset = 2
    hass = _make_hass(coordinator)

    plain = async_get_advert_snapshot(hass, tracked_only=True)
    scanner = plain["devices"]["aa:bb:cc:dd:ee:ff"]["scanners"]["f1:74:64:00:00:01"]
    assert "history" not in scanner
    assert scanner["ref_power"] == -55.0
    assert scanner["attenuation"] == 3.0
    assert scanner["rssi_offset"] == 2

    with_hist = async_get_advert_snapshot(hass, tracked_only=True, include_history=True)
    scanner = with_hist["devices"]["aa:bb:cc:dd:ee:ff"]["scanners"]["f1:74:64:00:00:01"]
    assert scanner["history"] == [[-63, 999.0], [-65, 998.0], [-70, 996.5], [-61, 995.0]]

    # A per-device ref_power override wins over the global option.
    advert.ref_power = -59.0
    again = async_get_advert_snapshot(hass, tracked_only=True)
    assert again["devices"]["aa:bb:cc:dd:ee:ff"]["scanners"]["f1:74:64:00:00:01"]["ref_power"] == -59.0

    import json

    json.dumps(with_hist)


# --- rssi offsets: live apply, persist without reload ------------------------ #


def _offset_fixture():
    """A coordinator with two scanners' adverts and a hass whose config-entry
    update is recorded (and whose reload is recorded separately)."""
    from types import MappingProxyType

    from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator

    def _advert(scanner, rssi=-70):
        a = SimpleNamespace(scanner_address=scanner, rssi=rssi, conf_rssi_offset=0, recomputed=0)

        def _recompute(reading_is_new=True):
            assert reading_is_new is False
            a.recomputed += 1

        a._update_raw_distance = _recompute
        return a

    adverts = {
        ("dev1", "aa:aa:aa:aa:aa:01"): _advert("aa:aa:aa:aa:aa:01"),
        ("dev1", "aa:aa:aa:aa:aa:02"): _advert("aa:aa:aa:aa:aa:02"),
        ("dev2", "aa:aa:aa:aa:aa:01"): _advert("aa:aa:aa:aa:aa:01", rssi=None),
    }
    coordinator = SimpleNamespace(
        options={"rssi_offsets": {"aa:aa:aa:aa:aa:02": 2.0}, "attenuation": 3.0, "ref_power": -55.0},
        devices={"dev1": SimpleNamespace(adverts={k: v for k, v in adverts.items() if k[0] == "dev1"}),
                 "dev2": SimpleNamespace(adverts={k: v for k, v in adverts.items() if k[0] == "dev2"})},
        inline_options=None,
    )
    coordinator.async_apply_rssi_offsets = lambda offsets, merge=True: BermudaDataUpdateCoordinator.async_apply_rssi_offsets(
        coordinator, offsets, merge=merge
    )
    entry = SimpleNamespace(
        entry_id="e1",
        options=MappingProxyType({"rssi_offsets": {"aa:aa:aa:aa:aa:02": 2.0}, "attenuation": 3.0}),
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    updates, reloads = [], []

    def _update(e, options=None, **kw):
        e.options = MappingProxyType(dict(options))
        updates.append(dict(options))
        return True

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [entry] if domain == DOMAIN else [],
            async_update_entry=_update,
            async_schedule_reload=lambda entry_id: reloads.append(entry_id),
        )
    )
    return hass, entry, coordinator, adverts, updates, reloads


def test_get_rssi_offsets_reports_map_and_path_loss_parameters():
    from custom_components.bermuda.api import async_get_rssi_offsets

    hass, *_ = _offset_fixture()
    got = async_get_rssi_offsets(hass)
    assert got == {"offsets": {"aa:aa:aa:aa:aa:02": 2.0}, "attenuation": 3.0, "ref_power": -55.0}


def test_set_rssi_offsets_applies_live_and_persists_without_reload():
    import asyncio

    from custom_components.bermuda import async_reload_entry
    from custom_components.bermuda.api import async_set_rssi_offsets

    hass, entry, coordinator, adverts, updates, reloads = _offset_fixture()

    result = async_set_rssi_offsets(hass, {"AA:AA:AA:AA:AA:01": -4.4})

    # Merged map, lower-cased, existing scanner kept.
    assert result == {"aa:aa:aa:aa:aa:01": -4.4, "aa:aa:aa:aa:aa:02": 2.0}
    assert coordinator.options["rssi_offsets"] == result
    # Every advert from the changed scanner got the offset and a recompute;
    # the one with no rssi yet got the offset but no recompute; the other
    # scanner's advert is untouched.
    a1, a2, a3 = adverts[("dev1", "aa:aa:aa:aa:aa:01")], adverts[("dev1", "aa:aa:aa:aa:aa:02")], adverts[("dev2", "aa:aa:aa:aa:aa:01")]
    assert (a1.conf_rssi_offset, a1.recomputed) == (-4.4, 1)
    assert (a2.conf_rssi_offset, a2.recomputed) == (0, 0)
    assert (a3.conf_rssi_offset, a3.recomputed) == (-4.4, 0)
    # Persisted to the entry...
    assert updates == [{"rssi_offsets": result, "attenuation": 3.0}]
    # ...and the update listener recognises the change as already live.
    asyncio.new_event_loop().run_until_complete(async_reload_entry(hass, entry))
    assert reloads == []
    assert coordinator.inline_options is None
    # A different options change (the user editing attenuation) still reloads.
    entry.options = type(entry.options)({**dict(entry.options), "attenuation": 2.5})
    asyncio.new_event_loop().run_until_complete(async_reload_entry(hass, entry))
    assert reloads == ["e1"]


def test_set_rssi_offsets_replace_mode_and_no_op_persist():
    from custom_components.bermuda.api import async_set_rssi_offsets

    hass, entry, coordinator, adverts, updates, _ = _offset_fixture()
    # Replacing the map drops scanner 02 back to 0 (its advert is recomputed).
    result = async_set_rssi_offsets(hass, {"aa:aa:aa:aa:aa:01": 1.0}, merge=False)
    assert result == {"aa:aa:aa:aa:aa:01": 1.0}
    a2 = adverts[("dev1", "aa:aa:aa:aa:aa:02")]
    assert (a2.conf_rssi_offset, a2.recomputed) == (0.0, 1)
    # Setting the same values again changes nothing and does not touch the entry.
    n = len(updates)
    async_set_rssi_offsets(hass, {"aa:aa:aa:aa:aa:01": 1.0}, merge=False)
    assert len(updates) == n
    # Clamped to Bermuda's own +-127 dB range; None when Bermuda is absent.
    assert async_set_rssi_offsets(hass, {"aa:aa:aa:aa:aa:01": 500})["aa:aa:aa:aa:aa:01"] == 127.0
    assert async_set_rssi_offsets(
        SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda d: [])), {"x": 1}
    ) is None


def test_scanner_ranging_lists_how_scanners_hear_each_other(monkeypatch):
    import custom_components.bermuda.api as api_module
    monkeypatch.setattr(api_module, "monotonic_time_coarse", lambda: 1000.0)
    """Each scanner's own advert, as heard by its siblings: a labelled range
    at a known position, without a full snapshot or a dump_devices call."""
    heard_by_b = SimpleNamespace(scanner_address="bb:00:00:00:00:02", rssi_distance=4.0,
                                 rssi_distance_raw=4.4, rssi=-70, stamp=990.0)
    heard_by_self = SimpleNamespace(scanner_address="aa:00:00:00:00:01", rssi_distance=0.1,
                                    rssi_distance_raw=0.1, rssi=-30, stamp=999.0)
    stale = SimpleNamespace(scanner_address="cc:00:00:00:00:03", rssi_distance=9.0,
                            rssi_distance_raw=9.5, rssi=-88, stamp=100.0)
    scanner_a = SimpleNamespace(address="aa:00:00:00:00:01", adverts={
        ("aa:00:00:00:00:01", "bb:00:00:00:00:02"): heard_by_b,
        ("aa:00:00:00:00:01", "aa:00:00:00:00:01"): heard_by_self,
        ("aa:00:00:00:00:01", "cc:00:00:00:00:03"): stale,
    })
    scanner_b = SimpleNamespace(address="bb:00:00:00:00:02", adverts={})
    coordinator = SimpleNamespace(devices={}, get_scanners=[scanner_a, scanner_b])
    hass = _make_hass(coordinator)

    ranging = async_get_scanner_ranging(hass)
    assert ranging["version"] == SNAPSHOT_VERSION
    a = ranging["scanners"]["aa:00:00:00:00:01"]
    assert set(a) == {"bb:00:00:00:00:02", "cc:00:00:00:00:03"}   # never itself
    assert a["bb:00:00:00:00:02"]["distance"] == 4.0
    assert a["bb:00:00:00:00:02"]["distance_raw"] == 4.4
    assert a["bb:00:00:00:00:02"]["age"] is not None
    assert ranging["scanners"]["bb:00:00:00:00:02"] == {}          # heard by nobody, still listed

    fresh = async_get_scanner_ranging(hass, max_age=500.0)
    assert set(fresh["scanners"]["aa:00:00:00:00:01"]) == {"bb:00:00:00:00:02"}


def test_scanner_ranging_is_none_without_bermuda_and_advertised_as_a_feature():
    from custom_components.bermuda.api import SNAPSHOT_FEATURES
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: []))
    assert async_get_scanner_ranging(hass) is None
    assert "scanner_ranging" in SNAPSHOT_FEATURES


# --- device management ---------------------------------------------------------


def _mgmt_hass(configured=("AA:AA:AA:AA:AA:01",), devices=None):
    from custom_components.bermuda.const import CONF_DEVICES

    entry = SimpleNamespace(options={CONF_DEVICES: list(configured)}, data={},
                            runtime_data=SimpleNamespace(coordinator=SimpleNamespace(devices=devices or {})))
    updates = []

    def async_update_entry(e, options=None, data=None):
        if options is not None:
            e.options = options
        if data is not None:
            e.data = data
        updates.append((options, data))

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [entry] if domain == DOMAIN else [],
            async_update_entry=async_update_entry,
        )
    )
    return hass, entry, updates


def test_set_tracked_devices_adds_removes_and_persists():
    import asyncio
    from custom_components.bermuda.api import async_set_tracked_devices

    hass, entry, updates = _mgmt_hass()
    new = asyncio.run(async_set_tracked_devices(hass, add=["tile_24d1093b0211", "AA:AA:AA:AA:AA:01"], remove=[]))
    assert new == ["AA:AA:AA:AA:AA:01", "TILE_24D1093B0211"]          # upper-cased, de-duplicated
    assert entry.options["configured_devices"] == new and len(updates) == 1
    new = asyncio.run(async_set_tracked_devices(hass, remove=["aa:aa:aa:aa:aa:01"]))
    assert new == ["TILE_24D1093B0211"] and len(updates) == 2
    # A no-op change does not touch the entry (no needless reload).
    asyncio.run(async_set_tracked_devices(hass, add=["TILE_24D1093B0211"]))
    assert len(updates) == 2


def test_device_candidates_mirror_the_flows_picker(monkeypatch):
    import custom_components.bermuda.api as api_module
    from custom_components.bermuda.api import async_get_device_candidates
    from custom_components.bermuda.const import ADDR_TYPE_PRIVATE_BLE_DEVICE

    monkeypatch.setattr(api_module, "monotonic_time_coarse", lambda: 10_000.0)
    adv = SimpleNamespace(stamp=9_990.0, rssi=-66)

    def dev(address, **kw):
        base = dict(name=address, is_scanner=False, create_sensor=False, address_type="bd_addr_other",
                    last_seen=9_995.0, first_seen=9_000.0, adverts={"x": adv}, is_tile=False, manufacturer=None,
                    area_name=None)
        base.update(kw)
        return SimpleNamespace(address=address, **base)

    devices = {
        "aa:00:00:00:00:01": dev("aa:00:00:00:00:01", name="Beacon"),
        "aa:00:00:00:00:02": dev("aa:00:00:00:00:02", is_scanner=True),
        "aa:00:00:00:00:03": dev("aa:00:00:00:00:03", create_sensor=True),
        "aa:00:00:00:00:04": dev("aa:00:00:00:00:04", address_type=ADDR_TYPE_PRIVATE_BLE_DEVICE),
        "aa:00:00:00:00:05": dev("aa:00:00:00:00:05", last_seen=1.0),                   # too old
        "24:d1:09:3b:02:11": dev("24:d1:09:3b:02:11", is_tile=True, manufacturer="Tile"),
        "15:09:c2:45:24:28": dev("15:09:c2:45:24:28", is_tile=True),                     # bound: hidden
    }
    tile_manager = SimpleNamespace(bound_sources=lambda: {"15:09:c2:45:24:28"})
    hass, entry, _ = _mgmt_hass(devices=devices)
    entry.runtime_data.coordinator.tile_manager = tile_manager

    rows = async_get_device_candidates(hass)
    assert [r["address"] for r in rows] == ["aa:00:00:00:00:01", "24:d1:09:3b:02:11"]
    beacon, tile = rows
    assert beacon["config_value"] == "AA:00:00:00:00:01" and beacon["kind"] == "device"
    assert tile["config_value"] == "TILE_24D1093B0211" and tile["kind"] == "tile"
    assert tile["scanners"] == 1 and tile["best_rssi"] == -66 and tile["last_seen_age"] == 5.0


def test_options_are_read_and_written_within_the_managed_set():
    import asyncio
    import pytest
    from custom_components.bermuda.api import async_get_options, async_set_options

    hass, entry, updates = _mgmt_hass()
    entry.options.update({"ref_power": -55, "attenuation": 3.0, "rssi_offsets": {"x": 1}})
    assert async_get_options(hass) == {"ref_power": -55, "attenuation": 3.0}   # rssi_offsets has its own API
    out = asyncio.run(async_set_options(hass, {"attenuation": 2.5}))
    assert out["attenuation"] == 2.5 and entry.options["rssi_offsets"] == {"x": 1} and len(updates) == 1
    with pytest.raises(ValueError):
        asyncio.run(async_set_options(hass, {"configured_devices": []}))


def test_management_is_advertised_as_a_feature():
    from custom_components.bermuda.api import SNAPSHOT_FEATURES
    assert "device_management" in SNAPSHOT_FEATURES


def test_apple_advert_kinds_and_summary():
    from custom_components.bermuda import api
    from custom_components.bermuda.const import BDADDR_TYPE_RANDOM_RESOLVABLE, BDADDR_TYPE_RANDOM_STATIC

    # Nearby Info (0x10, 5 bytes) followed by Handoff (0x0c, 14 bytes) as an iPhone sends them.
    phone = {0x004C: bytes([0x10, 0x05, 1, 2, 3, 4, 5, 0x0C, 0x0E]) + bytes(14)}
    assert api.apple_advert_kinds(phone) == ["nearby_info", "handoff"]
    assert "Private BLE Device" in api.apple_summary(api.apple_advert_kinds(phone), BDADDR_TYPE_RANDOM_RESOLVABLE)
    assert api.apple_summary(api.apple_advert_kinds(phone), BDADDR_TYPE_RANDOM_STATIC) == "iPhone / iPad / Mac / Watch"
    pods = {0x004C: bytes([0x07, 0x19]) + bytes(25)}
    assert api.apple_summary(api.apple_advert_kinds(pods), None).startswith("AirPods")
    tag = {0x004C: bytes([0x12, 0x19]) + bytes(25)}
    assert api.apple_summary(api.apple_advert_kinds(tag), None).startswith("Find My")
    assert api.apple_advert_kinds({0x0059: b"\x01"}) == [] and api.apple_summary([], None) is None
    assert api.apple_advert_kinds(None) == []
