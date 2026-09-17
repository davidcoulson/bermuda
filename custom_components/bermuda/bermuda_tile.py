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

import asyncio

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


# --- phase 3: identity over GATT ----------------------------------------------
# node-tile (lesleyxyz) reads this characteristic straight after connecting,
# before any authentication: newer "Private ID" Tiles - the ones that rotate
# their address - expose their Tile ID here. Older Tiles have no such
# characteristic and never rotate, so their MAC stays their identity.
TILE_ID_CHAR_UUID = "9d410007-35d6-f4dd-ba60-e7bd8dc491c0"
TILE_PROBE_TIMEOUT = 45.0  # seconds for one connect + read (a busy C3 proxy can take a while)
TILE_PROBE_RETRY_SECS = 120.0  # a transient (connect/read) failure is retried after this
TILE_PROBE_UNAVAILABLE_RETRY_SECS = 60.0  # ...and "no connectable scanner hears it" after this
# An address not heard for longer than this cannot be connected to (the
# Bluetooth manager drops it, and a rotated Tile is simply gone); probing
# it only burns a proxy slot for TILE_PROBE_TIMEOUT.
TILE_PROBE_MAX_AGE_SECS = 120.0
# A bound address silent this long (or never heard since Bermuda started) is
# an orphan: the Tile rotated while Bermuda was down, or the heuristic never
# found the successor. Only identity can recover it (see _orphan_handover).
TILE_ORPHAN_SECS = 300.0
TILE_PROBE_RESULT_TTL = 6 * 3600  # forget results for addresses this old


class TileProbeUnavailable(Exception):
    """No connectable scanner currently hears the address."""


class TileNoIdCharacteristic(Exception):
    """Connected, but the Tile exposes no Tile ID characteristic; str() lists what it does expose."""


async def async_read_tile_uid(hass, address: str) -> str | None:
    """Connect to a Tile through Home Assistant's bluetooth stack and read its Tile ID.

    Returns the ID as hex, or None when the Tile has no Tile ID characteristic
    (a non-rotating model). Raises TileProbeUnavailable when no connectable
    path exists right now (no active proxy hears it), and whatever bleak
    raises when the connection or read fails.
    """
    from homeassistant.components import bluetooth  # noqa: PLC0415
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection  # noqa: PLC0415

    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), connectable=True)
    if ble_device is None:
        raise TileProbeUnavailable(f"no connectable scanner hears {address}")
    client = await establish_connection(BleakClientWithServiceCache, ble_device, f"Tile {address}", max_attempts=2)
    try:
        char = client.services.get_characteristic(TILE_ID_CHAR_UUID)
        if char is None:
            # What the Tile does expose, so a model that keeps its ID
            # somewhere else can be recognised from the diagnostics.
            seen = []
            for service in client.services:
                chars = ",".join(c.uuid[4:8] if c.uuid.endswith("-0000-1000-8000-00805f9b34fb") else c.uuid for c in service.characteristics)
                seen.append(f"{service.uuid[4:8] if service.uuid.endswith('-0000-1000-8000-00805f9b34fb') else service.uuid}[{chars}]")
            raise TileNoIdCharacteristic(" ".join(seen) or "no services")
        return bytes(await client.read_gatt_char(char)).hex()
    finally:
        await client.disconnect()


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
        self._hass = hass
        self._store: Store | None = Store(hass, TILE_STORAGE_VERSION, TILE_STORAGE_KEY) if hass is not None else None
        # Phase 3: tile_id -> Tile ID read over GATT ("" = probed, the Tile has
        # no ID characteristic and does not rotate). Persisted with the bindings.
        self.uids: dict[str, str] = {}
        # address -> {"uid": str | None, "error": str | None, "stamp": float}
        # for every address probed; None uid with an error is a failed probe.
        self._probes: dict[str, dict[str, Any]] = {}
        self._pending: set[str] = set()
        self._queue: list[tuple[str, str | None]] = []
        self._worker: asyncio.Task | None = None
        self._probe_fn = async_read_tile_uid
        self.probes = 0
        self.probe_failures = 0
        self.last_probe: dict[str, Any] | None = None
        self._started: float | None = None  # first async_update stamp: scanners need a moment after a restart

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
        uids = data.get("uids")
        if isinstance(uids, dict):
            self.uids = {str(k): str(v) for k, v in uids.items()}

    def _data(self) -> dict[str, Any]:
        return {
            "bindings": self.bindings,
            "handovers": self.handovers,
            "ambiguous_handovers": self.ambiguous_handovers,
            "uids": self.uids,
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
        if self._started is None:
            self._started = nowstamp
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
            # Learn this Tile's ID from whichever address it is bound to now,
            # once; a rotation is then resolved by reading the successor's.
            sources = self.bindings.get(tile_id) or []
            if sources and tile_id not in self.uids:
                known = self._probes.get(sources[0])
                if known and not known.get("error") and known.get("uid"):
                    # Already read (as a handover candidate): that answer is this Tile's ID.
                    self.uids[tile_id] = known["uid"]
                    dirty = True
                    _LOGGER.info("Tile %s identified: Tile ID %s", tile_id, known["uid"])
                else:
                    self._request_probe(sources[0], learn_for=tile_id, nowstamp=nowstamp)
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

    def _orphan_handover(self, tile_id: str, nowstamp: float) -> bool:
        """Recover a Tile whose bound address is long gone.

        There is no handover window to reason about any more, and with several
        Tiles in the house an RSSI guess would be a coin toss, so only identity
        counts: every live Tile address bound to nothing is asked for its ID
        and the one answering with this Tile's ID is bound. With the ID still
        unknown nothing binds, but the answers are kept (diagnostics
        ``probe_results``), so the right address can be re-added by hand and
        every later rotation is resolved by identity.
        """
        if not self._can_probe() or self._started is None or nowstamp - self._started < TILE_SILENT_SECS:
            return False  # give the scanners a moment after a restart before declaring it gone
        taken = self.bound_sources()
        uid = self.uids.get(tile_id)
        for device in list(self._coordinator.devices.values()):
            if (
                not getattr(device, "is_tile", False)
                or device.address in taken
                or device.metadevice_sources
                or not self._probe_possible(device, nowstamp)
            ):
                continue
            self._request_probe(device.address, nowstamp=nowstamp)
            result = self._probes.get(device.address)
            if uid and result is not None and result.get("uid") == uid:
                self.bind(tile_id, device.address, reason="tile id (recovered)")
                return True
        return False

    def _maybe_handover(self, metadevice: BermudaDevice, tile_id: str, nowstamp: float) -> bool:
        coordinator = self._coordinator
        sources = self.bindings.get(tile_id) or []
        bound = coordinator._get_device(sources[0]) if sources else None
        if not sources:
            return False
        if bound is None or not bound.last_seen or nowstamp - bound.last_seen > TILE_ORPHAN_SECS:
            return self._orphan_handover(tile_id, nowstamp)
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
        # Phase 3: when this Tile's ID is known and probing is possible, ask
        # each candidate who it is. A match binds outright; a mismatch (or a
        # Tile with no ID characteristic - it does not rotate, so it cannot be
        # a successor) drops out of the heuristic below; while probes are
        # still pending or retryable nothing is decided yet.
        uid = self.uids.get(tile_id)
        if self._can_probe():
            ordered = sorted(candidates, key=lambda d: -(d.last_seen or 0))
            if uid:
                for candidate in ordered:
                    result = self._probes.get(candidate.address)
                    if result is not None and result.get("uid") == uid:
                        self.bind(tile_id, candidate.address, reason="tile id")
                        return True
            # Ask every candidate that can still be reached, whether or not
            # this Tile's own ID is known yet: a candidate that answers with
            # NO ID characteristic does not rotate and cannot be a successor,
            # one that answers with another Tile's ID is that Tile, and the
            # answer of whichever candidate the heuristic then picks becomes
            # this Tile's ID (learned by inheritance, see below). Waiting only
            # happens while a probe is pending or still possible - a Tile
            # whose bound address rotated away before its ID was ever read
            # must not wait forever for a probe that can never run.
            undecided = False
            for candidate in ordered:
                self._request_probe(candidate.address, nowstamp=nowstamp)
                result = self._probes.get(candidate.address)
                if candidate.address in self._pending:
                    undecided = True
                elif result is None:
                    if self._probe_possible(candidate, nowstamp):
                        undecided = True
                elif result.get("error") and result.get("error") != "unavailable":
                    undecided = True  # transient failure: retried after TILE_PROBE_RETRY_SECS
            if undecided:
                return False
            candidates = [c for c in candidates if self._could_be(tile_id, self._probes.get(c.address))]
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
        # The successor answered a probe with an ID this Tile did not have yet:
        # that is its ID (learned by inheritance), so the next rotation is
        # resolved by identity instead of by RSSI pattern.
        if not self.uids.get(tile_id) and (r := self._probes.get(best.address)) and r.get("uid"):
            self.uids[tile_id] = r["uid"]
            _LOGGER.info("Tile %s identified through its successor %s: Tile ID %s", tile_id, best.address, r["uid"])
        self.bind(tile_id, best.address, score=best_score, scanners=best_n, reason="rssi pattern")
        # It rotated, so it IS a Private-ID Tile: a "no ID characteristic"
        # answer was wrong (or read from a stale service cache). Forget it so
        # the new address gets asked, with the services it exposes recorded.
        if self.uids.get(tile_id) == "":
            del self.uids[tile_id]
            self._schedule_save()
        return True

    # --- phase 3: probing --------------------------------------------------------

    def _can_probe(self) -> bool:
        return self._hass is not None

    def _probe_possible(self, device: BermudaDevice | None, nowstamp: float | None = None) -> bool:
        """Whether ``device`` was heard recently enough for a connection to be attempted."""
        if device is None or not device.last_seen:
            return False
        nowstamp = monotonic_time_coarse() if nowstamp is None else nowstamp
        return nowstamp - device.last_seen <= TILE_PROBE_MAX_AGE_SECS

    def _could_be(self, tile_id: str, result: dict[str, Any] | None) -> bool:
        """Whether a probe answer leaves an address eligible as this Tile's successor."""
        if result is None or result.get("error"):
            return True  # never asked, or could not be asked: the heuristic decides
        if not result.get("uid"):
            return False  # answered with no ID characteristic: it does not rotate
        uid = self.uids.get(tile_id)
        return not uid or result["uid"] == uid

    def _request_probe(self, address: str, learn_for: str | None = None, nowstamp: float | None = None) -> None:
        """Queue a Tile ID read for ``address`` unless one is pending, already
        answered, or the address is no longer heard (nothing to connect to)."""
        if not self._can_probe():
            return
        address = address.lower()
        if address in self._pending or any(a == address for a, _ in self._queue):
            return
        if not self._probe_possible(self._coordinator._get_device(address), nowstamp):
            return
        result = self._probes.get(address)
        if result is not None:
            if result.get("error") is None:
                return  # definitive answer
            retry = TILE_PROBE_UNAVAILABLE_RETRY_SECS if result.get("error") == "unavailable" else TILE_PROBE_RETRY_SECS
            if monotonic_time_coarse() - result["stamp"] < retry:
                return
        self._pending.add(address)
        self._queue.append((address, learn_for))
        if self._worker is None or self._worker.done():
            self._worker = self._hass.async_create_task(self._run_probes())

    async def _run_probes(self) -> None:
        """One probe at a time: a Tile holds one connection, and so does a proxy slot."""
        while self._queue:
            address, learn_for = self._queue.pop(0)
            try:
                uid = await asyncio.wait_for(self._probe_fn(self._hass, address), TILE_PROBE_TIMEOUT)
            except TileProbeUnavailable as err:
                self._record_probe(address, None, "unavailable", str(err))
            except TileNoIdCharacteristic as err:
                self._record_probe(address, None, None, str(err))
                self._on_probe_result(address, None, learn_for)
            except Exception as err:  # noqa: BLE001 - bleak raises a zoo of exceptions
                self._record_probe(address, None, "failed", f"{type(err).__name__}: {err}")
            else:
                self._record_probe(address, uid, None, None)
                self._on_probe_result(address, uid, learn_for)
            finally:
                self._pending.discard(address)

    def _record_probe(self, address: str, uid: str | None, error: str | None, detail: str | None) -> None:
        stamp = monotonic_time_coarse()
        self._probes[address] = {"uid": uid, "error": error, "stamp": stamp}
        self.probes += 1
        if error:
            self.probe_failures += 1
            _LOGGER.debug("Tile probe of %s %s: %s", address, error, detail)
        self.last_probe = {"address": address, "uid": uid, "error": error, "detail": detail, "stamp": stamp}
        # Forget answers for addresses that rotated away long ago.
        for old in [a for a, r in self._probes.items() if stamp - r["stamp"] > TILE_PROBE_RESULT_TTL]:
            del self._probes[old]

    def _on_probe_result(self, address: str, uid: str | None, learn_for: str | None) -> None:
        if learn_for is not None:
            self.uids[learn_for] = uid or ""
            _LOGGER.info(
                "Tile %s %s", learn_for,
                f"identified: Tile ID {uid}" if uid else "has no Tile ID characteristic (it will not rotate)",
            )
            self._schedule_save()
        if not uid:
            return
        # Any configured Tile with this ID that is not already bound here.
        for tile_id, known in self.uids.items():
            if known == uid and (self.bindings.get(tile_id) or [None])[0] != address:
                self.bind(tile_id, address, reason="tile id")

    def bind(self, tile_id: str, address: str, *, score: float | None = None, scanners: int | None = None,
             reason: str | None = None) -> None:
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
                              "reason": reason, "stamp": monotonic_time_coarse()}
        _LOGGER.info(
            "Tile %s re-bound to %s%s%s",
            metadevice.name,
            address,
            f" by {reason}" if reason else "",
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
            "uids": self.uids,
            "probes": self.probes,
            "probe_failures": self.probe_failures,
            "probes_pending": sorted(self._pending),
            "last_probe": self.last_probe,
            "probe_results": {
                a: {"uid": r.get("uid"), "error": r.get("error"), "age": round(monotonic_time_coarse() - r["stamp"], 1)}
                for a, r in self._probes.items()
            },
            "bound_age": {
                tile_id: (
                    None if not sources or (d := self._coordinator._get_device(sources[0])) is None or not d.last_seen
                    else round(monotonic_time_coarse() - d.last_seen, 1)
                )
                for tile_id, sources in self.bindings.items()
            },
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
