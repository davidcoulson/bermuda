"""A ref_power change must bypass the sensor cache straight away."""

from __future__ import annotations

from types import SimpleNamespace

from bluetooth_data_tools import monotonic_time_coarse

from custom_components.bermuda.entity import BermudaEntity


def test_cache_is_bypassed_right_after_a_ref_power_change():
    """The check compared against now + 2 s, so it could never pass."""
    now = monotonic_time_coarse()
    entity = SimpleNamespace(
        bermuda_update_interval=10,
        bermuda_last_stamp=now,  # cache is fresh
        bermuda_last_state=1.0,
        _device=SimpleNamespace(ref_power_changed=now),
    )
    # A rising distance would normally be held back by the cache.
    assert BermudaEntity._cached_ratelimit(entity, 2.0) == 2.0
    entity._device.ref_power_changed = now - 60
    entity.bermuda_last_stamp = now
    assert BermudaEntity._cached_ratelimit(entity, 3.0) == 2.0
