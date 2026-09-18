"""Naming the adverts the SIG lists cannot name.

The cases here are adverts actually seen on a house's proxies, with the
bytes recorded next to each.
"""

from __future__ import annotations

from custom_components.bermuda.device_family import SERVICE_UUIDS, Family, _uuid16, identify


def test_a_manufacturer_id_that_is_not_the_company_it_was_assigned_to():
    # Govee_H7038_5A57: manufacturer id 0x8843, payload ec00020100
    family = identify(manufacturer_data={0x8843: b"\xec\x00\x02\x01\x00"}, name="Govee_H7038_5A57")
    assert family == Family("Govee sensor") and not family.rotates
    # TAP-723A: manufacturer id 0x4269, payload spells "TigoCC"
    assert identify(manufacturer_data={0x4269: b"TigoCC"}, name="TAP-723A").name == "Tigo optimiser"


def test_a_service_uuid_names_the_kind_not_the_company():
    for uuid in ("0000feed-0000-1000-8000-00805f9b34fb", "feed", "FEED", 0xFEED):
        assert identify(service_uuids=[uuid]).name == "Tile tracker", uuid
    smarttag = identify(service_uuids=["fd5a"])
    assert smarttag.name == "Samsung SmartTag" and smarttag.rotates
    # Service data is keyed by the same UUIDs and counts just as much.
    assert identify(service_data={"0000feed-0000-1000-8000-00805f9b34fb": b""}).name == "Tile tracker"


def test_the_name_is_the_last_resort_and_is_case_insensitive():
    assert identify(name="EasyStart_9215").name == "EasyStart soft starter"
    assert identify(name="GOVEE_H5075").name == "Govee sensor"
    assert identify(name="Tractive GPS").name == "Tractive pet tracker"
    # A service uuid wins over a name that says something else.
    assert identify(service_uuids=["feed"], name="Govee_H7038").name == "Tile tracker"


def test_what_is_not_recognised_stays_unrecognised():
    # A custom 128-bit service uuid (FB0815 on the test house) is nobody's.
    assert identify(service_uuids=["57b40210-2528-d6bc-b043-b49af0ec06c1"]) is None
    assert identify(service_data={"0000fcb2-0000-1000-8000-00805f9b34fb": b"\x01\x01\xa4\x01"}) is None
    assert identify() is None and identify(name="") is None
    assert identify(manufacturer_data={0x004C: b"\x02\x15"}) is None  # Apple: the SIG list names that one


def test_uuid16_only_accepts_a_16_bit_id():
    assert _uuid16("0000feed-0000-1000-8000-00805f9b34fb") == 0xFEED
    assert _uuid16("feed") == 0xFEED and _uuid16(0xFEED) == 0xFEED
    assert _uuid16("0xfeed") == 0xFEED
    assert _uuid16("1eeb698c-d313-40f5-bb29-51c656299c5d") is None   # a custom 128-bit uuid
    assert _uuid16("1234feed-0000-1000-8000-00805f9b34fb") is None   # 32-bit in the SIG base
    assert _uuid16(None) is None and _uuid16("") is None and _uuid16(-1) is None and _uuid16(0x10000) is None
    assert _uuid16("zzzz") is None


def test_every_rotating_family_is_one_that_really_rotates():
    """A family marked rotating cannot be followed by address alone; the flag
    exists so a caller can say so rather than offering to track it."""
    rotating = {f.name for f in SERVICE_UUIDS.values() if f.rotates}
    assert rotating == {"Tile tracker", "Samsung SmartTag", "Exposure notification"}
