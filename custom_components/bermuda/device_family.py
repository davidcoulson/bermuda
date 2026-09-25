"""
What kind of thing an advert came from, when the SIG lists cannot say.

Bermuda resolves a manufacturer from the Bluetooth SIG's company and member
UUID lists (see ``BermudaDataUpdateCoordinator.get_manufacturer_from_id``).
That covers anyone who registered, but plenty of what a house hears did not:
a Govee sensor squats on a company id it does not own, a Tigo optimiser
writes its name into the payload, and a tracking tag is better described by
what it *is* than by the legal entity that made it.

So this is the opinionated layer under the SIG lists: a small table of
signatures seen in the wild, each with the family it identifies. It is only
ever a label. Whether a device can actually be *followed* is a different
question, answered by whether its address holds still (or by an IRK, a Tile
binding, or FindMy keys) - ``rotates`` records that, so a caller can say why
a device it can name still cannot be tracked.

Every entry should come from an advert someone has actually seen; the
comment against each says what it was.
"""

from __future__ import annotations

from typing import NamedTuple


class Family(NamedTuple):
    """A recognised kind of device."""

    name: str
    rotates: bool = False  # the address changes, so the family is a label, not an identity


# Manufacturer ids that are not the company the SIG assigned them to, or that
# are unassigned and used anyway. Keyed by the id in the advert.
MANUFACTURER_IDS: dict[int, Family] = {
    0x8843: Family("Govee sensor"),  # Govee_H7038: mfr id 0x8843, payload ec00020100
    0x4269: Family("Tigo optimiser"),  # TAP-723A: mfr id 0x4269, payload "TigoCC"
}

# 16-bit service UUIDs. The SIG says who registered them; these say what the
# device is, which is what someone reading a list of adverts wants.
SERVICE_UUIDS: dict[int, Family] = {
    0xFEED: Family("Tile tracker", rotates=True),  # Bermuda's own Tile detection, see const.py
    0xFD5A: Family("Samsung SmartTag", rotates=True),  # SmartThings Find tags
    0xFEAA: Family("Eddystone beacon"),
    0xFD6F: Family("Exposure notification", rotates=True),  # COVID contact tracing, rotates by design
    0xFE2C: Family("Google Fast Pair"),
    0xFE95: Family("Xiaomi sensor"),
    0xFCD2: Family("BTHome sensor"),  # already named by the SIG list; kept for the family view
}

# Local-name prefixes, for devices whose name is the only clue. Matched
# case-insensitively against the start of the advertised name.
NAME_PREFIXES: tuple[tuple[str, Family], ...] = (
    ("govee_", Family("Govee sensor")),
    ("gvh", Family("Govee sensor")),
    ("easystart_", Family("EasyStart soft starter")),  # Micro-Air, on an air conditioner
    ("tap-", Family("Tigo optimiser")),
    ("tractive", Family("Tractive pet tracker")),
    ("chipolo", Family("Chipolo tag", rotates=True)),
    ("itag", Family("iTag tag")),
    ("mi band", Family("Mi Band")),
    ("amazfit", Family("Amazfit wearable")),
)


def _uuid16(uuid) -> int | None:
    """
    The 16-bit id of a service UUID, or None if it is not a 16-bit one.

    Adverts carry these either as the bare id or expanded into the SIG's base
    UUID (0000xxxx-0000-1000-8000-00805f9b34fb).
    """
    if isinstance(uuid, int):
        return uuid if 0 <= uuid <= 0xFFFF else None
    if not isinstance(uuid, str):
        return None
    text = uuid.strip().lower().replace("0x", "")
    if text.endswith("-0000-1000-8000-00805f9b34fb"):
        text = text[:8]
        if text[:4] != "0000":
            return None  # a 32-bit id in the SIG base; not one of ours
        text = text[4:]
    text = text.replace(":", "").replace("-", "")
    if len(text) != 4:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def identify(service_uuids=None, service_data=None, manufacturer_data=None, name=None) -> Family | None:
    """
    The family this advert belongs to, or None when nothing recognises it.

    Signatures are tried strongest first: a service UUID is deliberate, a
    manufacturer id is nearly so, and a name is whatever someone typed.
    """
    for source in (service_uuids or (), (service_data or {}).keys()):
        for uuid in source:
            found = SERVICE_UUIDS.get(_uuid16(uuid))
            if found:
                return found

    for company in manufacturer_data or {}:
        try:
            found = MANUFACTURER_IDS.get(int(company))
        except (TypeError, ValueError):
            continue
        if found:
            return found

    if isinstance(name, str) and name:
        lowered = name.strip().lower()
        for prefix, family in NAME_PREFIXES:
            if lowered.startswith(prefix):
                return family
    return None
