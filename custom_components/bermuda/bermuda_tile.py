"""
Tile tracker support.

Fingerprinting (recognising a Tile and calibrating it) lives in
``BermudaDevice.process_tile``. This module holds the part ESPresense never
had: a *metadevice* for a Tile that survives the tag rotating its Bluetooth
address.

What a Tile transmits, measured on this fork's reference install and matching
the community reports: a service UUID of 0xFEED, no manufacturer data, no local
name and - on the hardware seen here - no service data at all. Newer Tiles
rotate their address together with whatever payload they do carry, and the
schedule is seeded at pairing and held by Tile's app and servers, so unlike
IRK devices there is nothing in the advert that can be resolved back to the
tag. The only continuity available is physical: a tag that just went quiet on
one address and reappeared on another, in the same place, as seen by the same
scanners at about the same signal strength.

That is what ``BermudaTileManager`` matches on, and it is deliberately a
HEURISTIC with an ambiguity guard rather than an identity: a house with one
Tile re-binds cleanly across every rotation; two Tiles rotating together in
the same drawer bind nothing, log it, and count it in diagnostics, because a
wrong bind silently reports the wrong tag's location. ``bind()`` is public so
that an external source of truth (an integration holding the Tile account,
per agittins/bermuda#412) can later call it with certainty instead of a score.

The metadevice's address is ``tile_<12 hex>`` of the address it was first
configured from; that string is only an identifier and stops meaning "current
MAC" the moment the tag rotates. Bindings persist in a Store so a restart does
not orphan the tag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from .const import (
    _LOGGER,
    _LOGGER_SPAM_LESS,
    CONF_DEVICES,
    METADEVICE_TYPE_TILE_SOURCE,
    TILE_HANDOVER_WINDOW,
    TILE_METADEVICE_PREFIX,
    TILE_MIN_SCANNERS,
    TILE_RSSI_MARGIN,
    TILE_RSSI_TOLERANCE,
    TILE_SILENT_SECS,
    TILE_SOURCE_HISTORY,
    TILE_STORAGE_KEY,
    TILE_STORAGE_SAVE_DELAY,
    TILE_STORAGE_VERSION,
)

if TYPE_CHECKING:
    from .bermuda_device import BermudaDevice
    from .coordinator import BermudaDataUpdateCoordinator


def tile_metadevice_id(address: str) -> str:
    """The metadevice id for a Tile first seen at ``address``: ``tile_<12 hex>``."""
    return TILE_METADEVICE_PREFIX + address.lower().replace(":", "").replace("-", "").replace("_", "")


def mac_from_tile_id(tile_id: str) -> str | None:
    """The address a ``tile_<12 hex>`` id was made from, or None if malformed."""
    tail = tile_id[len(TILE_METADEVICE_PREFIX):].lower()
    if len(tail) != 12 or any(c not in "0123456789abcdef" for c in tail):
        return None
    return ":".join(tail[i : i + 2] for i in range(0, 12, 2))


def _mean(values) -> float | None:
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _rssi_by_scanner(device: BermudaDevice, *, latest: bool) -> dict[str, float]:
    """Per-scanner signal for a device: its newest few readings (``latest``,
    for the departing address) or its oldest kept ones (for a candidate, so a
    tag that has since been carried away still matches where it first
    appeared)."""
    out: dict[str, float] = {}
    for advert in (getattr(device, "adverts", None) or {}).values():
        hist = list(getattr(advert, "hist_rssi", None) or [])
        if not hist:
            continue
        sample = hist[:3] if latest else hist[-3:]
        value = _mean(sample)
        if value is not None:
            out[advert.scanner_address] = value
    return out


def handover_score(departing: BermudaDevice, candidate: BermudaDevice) -> tuple[float, int] | None:
    """Mean absolute per-scanner RSSI delta between a departing address's last
    readings and a candidate's first, over the scanners that saw both.

    None when fewer than TILE_MIN_SCANNERS scanners saw both: one scanner's
    RSSI says "about this far from one point", which every tag in the house
    can satisfy somewhere.
    """
    last = _rssi_by_scanner(departing, latest=True)
    first = _rssi_by_scanner(candidate, latest=False)
    shared = set(last) & set(first)
    if len(shared) < TILE_MIN_SCANNERS:
        return None
    return sum(abs(last[s] - first[s]) for s in shared) / len(shared), len(shared)


class BermudaTileManager:
    """Owns Tile metadevices and re-binds them across address rotations."""

    def __init__(self, coordinator: BermudaDataUpdateCoordinator) -> None:
        self._coordinator = coordinator
        # tile_id -> source addresses, newest first. The head is the bound MAC.
        self.bindings: dict[str, list[str]] = {}
        self.ambiguous_handovers = 0
        self.handovers = 0
        self.last_ambiguity: dict[str, Any] | None = None
        self.last_handover: dict[str, Any] | None = None
        hass = getattr(coordinator, "hass", None)
        self._store: Store | None = Store(hass, TILE_STORAGE_VERSION, TILE_STORAGE_KEY) if hass is not None else None

    # --- persistence ---------------------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        bindings = data.get("bindings")
        if isinstance(bindings, dict):
            self.bindings = {
                str(tile_id): [str(a).lower() for a in sources if a]
                for tile_id, sources in bindings.items()
                if isinstance(sources, list)
            }
        self.handovers = int(data.get("handovers") or 0)
        self.ambiguous_handovers = int(data.get("ambiguous_handovers") or 0)

    def _data(self) -> dict[str, Any]:
        return {
            "bindings": self.bindings,
            "handovers": self.handovers,
            "ambiguous_handovers": self.ambiguous_handovers,
        }

    def _schedule_save(self) -> None:
        if self._store is not None:
            self._store.async_delay_save(self._data, TILE_STORAGE_SAVE_DELAY)

    # --- per-cycle -----------------------------------------------------------

    @callback
    def async_update(self, nowstamp: float | None = None) -> None:
        """Run once per update cycle, before update_metadevices copies adverts.

        Ensures a metadevice exists for every configured Tile (seeded from the
        address in its id), keeps ``metadevice_sources`` in step with the
        persisted bindings, and looks for a successor address for any Tile
        whose bound address has gone quiet.
        """
        coordinator = self._coordinator
        nowstamp = monotonic_time_coarse() if nowstamp is None else nowstamp
        configured = {str(a).lower() for a in coordinator.options.get(CONF_DEVICES, [])}
        tile_ids = {a for a in configured if a.startswith(TILE_METADEVICE_PREFIX)} | set(self.bindings)
        if not tile_ids:
            return
        dirty = False
        for tile_id in sorted(tile_ids):
            metadevice = coordinator._get_or_create_device(tile_id)
            if metadevice.address not in coordinator.metadevices:
                coordinator.metadevices[metadevice.address] = metadevice
            if tile_id in configured:
                metadevice.create_sensor = True
            sources = self.bindings.setdefault(tile_id, [])
            if not sources:
                seed = mac_from_tile_id(tile_id)
                if seed:
                    sources.append(seed)
                    dirty = True
            for address in reversed(sources):
                if address not in metadevice.metadevice_sources:
                    metadevice.metadevice_sources.insert(0, address)
                source = coordinator._get_device(address)
                if source is not None:
                    source.metadevice_type.add(METADEVICE_TYPE_TILE_SOURCE)
                    source.is_tile = True
            if sources and metadevice.metadevice_sources[:1] != sources[:1]:
                # Bindings decide which address is current.
                if sources[0] in metadevice.metadevice_sources:
                    metadevice.metadevice_sources.remove(sources[0])
                metadevice.metadevice_sources.insert(0, sources[0])
            if self._maybe_handover(metadevice, tile_id, nowstamp):
                dirty = True
        if dirty:
            self._schedule_save()

    def bound_sources(self) -> set[str]:
        return {a for sources in self.bindings.values() for a in sources}

    def _maybe_handover(self, metadevice: BermudaDevice, tile_id: str, nowstamp: float) -> bool:
        coordinator = self._coordinator
        sources = self.bindings.get(tile_id) or []
        bound = coordinator._get_device(sources[0]) if sources else None
        if bound is None or not bound.last_seen:
            return False
        quiet_for = nowstamp - bound.last_seen
        if quiet_for < TILE_SILENT_SECS:
            return False
        taken = self.bound_sources()
        window_start = bound.last_seen - TILE_SILENT_SECS
        window_end = bound.last_seen + TILE_HANDOVER_WINDOW
        candidates = [
            device
            for device in coordinator.devices.values()
            if getattr(device, "is_tile", False)
            and device.address != bound.address
            and device.address not in taken
            and not device.metadevice_sources  # not itself a metadevice
            and window_start <= (device.first_seen or 0) <= window_end
            and nowstamp - device.last_seen <= TILE_HANDOVER_WINDOW
        ]
        scored = []
        for candidate in candidates:
            score = handover_score(bound, candidate)
            if score is not None:
                scored.append((score[0], score[1], candidate))
        if not scored:
            return False
        scored.sort(key=lambda item: item[0])
        best_score, best_n, best = scored[0]
        if best_score > TILE_RSSI_TOLERANCE:
            return False
        if len(scored) > 1 and scored[1][0] - best_score < TILE_RSSI_MARGIN:
            self.ambiguous_handovers += 1
            self.last_ambiguity = {
                "tile": tile_id,
                "from": bound.address,
                "candidates": [(c.address, round(s, 1), n) for s, n, c in scored[:4]],
                "stamp": nowstamp,
            }
            _LOGGER_SPAM_LESS.warning(
                f"tile_ambiguous_{tile_id}",
                "Tile %s went quiet on %s and %d addresses could be its successor (best %.1f dB vs %.1f dB); "
                "not re-binding. It will show as away until the match is unambiguous.",
                metadevice.name,
                bound.address,
                len(scored),
                best_score,
                scored[1][0],
            )
            return True  # counters changed
        self.bind(tile_id, best.address, score=best_score, scanners=best_n)
        return True

    def bind(self, tile_id: str, address: str, *, score: float | None = None, scanners: int | None = None) -> None:
        """Make ``address`` the current source of Tile ``tile_id``.

        Public on purpose: a future integration that knows a tag's address for
        certain can call this directly instead of going through the heuristic.
        """
        coordinator = self._coordinator
        address = address.lower()
        sources = self.bindings.setdefault(tile_id, [])
        if address in sources:
            sources.remove(address)
        sources.insert(0, address)
        del sources[TILE_SOURCE_HISTORY:]
        metadevice = coordinator._get_or_create_device(tile_id)
        if metadevice.address not in coordinator.metadevices:
            coordinator.metadevices[metadevice.address] = metadevice
        if address in metadevice.metadevice_sources:
            metadevice.metadevice_sources.remove(address)
        metadevice.metadevice_sources.insert(0, address)
        source = coordinator._get_or_create_device(address)
        source.metadevice_type.add(METADEVICE_TYPE_TILE_SOURCE)
        source.is_tile = True
        self.handovers += 1
        self.last_handover = {"tile": tile_id, "to": address, "score": score, "scanners": scanners,
                              "stamp": monotonic_time_coarse()}
        _LOGGER.info(
            "Tile %s re-bound to %s%s",
            metadevice.name,
            address,
            f" (mean RSSI delta {score:.1f} dB over {scanners} scanners)" if score is not None else "",
        )
        self._schedule_save()

    # --- diagnostics ---------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        return {
            "bindings": self.bindings,
            "handovers": self.handovers,
            "ambiguous_handovers": self.ambiguous_handovers,
            "last_handover": self.last_handover,
            "last_ambiguity": self.last_ambiguity,
        }


def tile_capture(coordinator: BermudaDataUpdateCoordinator) -> list[dict[str, Any]]:
    """Phase-0 capture: everything Bermuda has seen from Tile adverts.

    Meant to be read from a diagnostics download by whoever designs the next
    step: per address, the address class (top two bits of octet 0), when it
    was first and last heard, and per scanner the RSSI history with stamps,
    the service-data payloads seen (hex), whether manufacturer data or a
    local name ever appeared, and which Tile metadevice (if any) it is bound
    to.
    """
    nowstamp = monotonic_time_coarse()
    bound_to = {
        address: tile_id
        for tile_id, sources in coordinator.tile_manager.bindings.items()
        for address in sources
    }
    out = []
    for device in coordinator.devices.values():
        if not getattr(device, "is_tile", False) or device.metadevice_sources:
            continue
        try:
            top_bits = int(device.address[0], 16) >> 2
        except (ValueError, IndexError):
            top_bits = None
        scanners = {}
        for advert in device.adverts.values():
            payloads = []
            for row in advert.service_data:
                for uuid, data in row.items():
                    payloads.append({"uuid": uuid, "hex": data.hex() if isinstance(data, bytes) else str(data)})
            scanners[advert.scanner_address] = {
                "scanner": advert.name,
                "rssi": advert.rssi,
                "hist_rssi": list(advert.hist_rssi),
                "hist_stamp": [round(s, 3) for s in advert.hist_stamp],
                "service_data": payloads,
                "manufacturer_data_seen": any(bool(row) for row in advert.manufacturer_data),
                "local_name": advert.local_name[0][0] if advert.local_name else None,
                "service_uuids": list(advert.service_uuids),
            }
        out.append({
            "address": device.address,
            "address_type": device.address_type,
            "top_bits": f"0b{top_bits:02b}" if top_bits is not None else None,
            "first_seen_age": round(nowstamp - device.first_seen, 1) if device.first_seen else None,
            "last_seen_age": round(nowstamp - device.last_seen, 1) if device.last_seen else None,
            "name": device.name,
            "ref_power": device.ref_power,
            "bound_to": bound_to.get(device.address),
            "scanners": scanners,
        })
    out.sort(key=lambda row: row["last_seen_age"] if row["last_seen_age"] is not None else 1e12)
    return out
