"""Attenuation and ref_power must be usable numbers: zero used to abort the update cycle."""

from __future__ import annotations

import math

import pytest
import voluptuous as vol

from custom_components.bermuda import util
from custom_components.bermuda.config_flow import ATTENUATION_SCHEMA, REF_POWER_SCHEMA
from custom_components.bermuda.const import DEFAULT_ATTENUATION, DEFAULT_REF_POWER, DISTANCE_INFINITE


@pytest.mark.parametrize("attenuation", [0, 0.0, -1.0, math.nan, math.inf, None])
def test_rssi_to_metres_uses_the_default_for_an_unusable_attenuation(attenuation):
    """Zero used to raise ZeroDivisionError and abort the update cycle. The
    result must still be a real distance: callers store it in histories and
    the area election, where a False would be read as 0 m."""
    got = util.rssi_to_metres(-60, -55, attenuation)
    assert got == util.rssi_to_metres(-60, -55, DEFAULT_ATTENUATION)
    assert got is not False and math.isfinite(got) and got > 0


@pytest.mark.parametrize("value", [0, -2, math.nan, "nan", 50])
def test_attenuation_schema_rejects_bad_values(value):
    with pytest.raises(vol.Invalid):
        ATTENUATION_SCHEMA(value)


def test_attenuation_and_ref_power_schemas_accept_normal_values():
    assert ATTENUATION_SCHEMA("3") == 3.0
    assert REF_POWER_SCHEMA(-55) == -55.0
    with pytest.raises(vol.Invalid):
        REF_POWER_SCHEMA(math.nan)


@pytest.mark.parametrize("ref_power", [math.nan, math.inf, -math.inf, None])
def test_rssi_to_metres_uses_the_default_for_an_unusable_ref_power(ref_power):
    got = util.rssi_to_metres(-60, ref_power, 3.0)
    assert got == util.rssi_to_metres(-60, DEFAULT_REF_POWER, 3.0)
    assert got is not False and math.isfinite(got)


def test_rssi_to_metres_puts_an_unusable_rssi_far_away():
    """Never 0 m (which would win the area election) and never NaN."""
    assert util.rssi_to_metres(math.nan, -55, 3.0) == DISTANCE_INFINITE


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(0, 3.0), (-2, 3.0), (0.05, 0.1), (50, 10.0), (math.nan, 3.0), ("bad", 3.0), (None, 3.0), (2.5, 2.5)],
)
def test_a_stored_attenuation_outside_the_bounds_is_pulled_in_for_the_form(stored, expected):
    from custom_components.bermuda.config_flow import ATTENUATION_RANGE, _attenuation_usable, _form_default

    value = _form_default(stored, 3.0, ATTENUATION_RANGE, _attenuation_usable)
    assert value == expected
    # And the form then accepts it as it stands.
    assert ATTENUATION_SCHEMA(value) == expected


@pytest.mark.parametrize("stored", [0, -2, math.nan, "2.5", True, 10**400])
def test_saving_the_form_as_shown_keeps_the_distances(stored):
    """An unusable attenuation runs as the default; the form must show (and so
    save) that same default, not a clamped 0.1 that turns 1.5 m into 100 km."""
    from custom_components.bermuda.config_flow import ATTENUATION_RANGE, _attenuation_usable, _form_default

    shown = _form_default(stored, DEFAULT_ATTENUATION, ATTENUATION_RANGE, _attenuation_usable)
    assert util.rssi_to_metres(-60, -55, ATTENUATION_SCHEMA(shown)) == util.rssi_to_metres(-60, -55, stored)


@pytest.mark.parametrize("stored", [math.nan, "-60", False, -(10**400)])
def test_saving_ref_power_as_shown_keeps_the_distances(stored):
    from custom_components.bermuda.config_flow import _ref_power_default

    shown = _ref_power_default({"ref_power": stored})
    assert util.rssi_to_metres(-60, REF_POWER_SCHEMA(shown), 3.0) == util.rssi_to_metres(-60, stored, 3.0)


def test_rssi_to_metres_never_raises_on_an_extreme_attenuation():
    got = util.rssi_to_metres(-90, -55, 0.001)
    assert isinstance(got, float) and got == DISTANCE_INFINITE


def test_a_bool_and_a_number_are_not_one_cache_entry():
    """The cache must not let a cached 1.0 answer for a stray True (or the reverse)."""
    util.rssi_to_metres.cache_clear()
    numeric = util.rssi_to_metres(-60, -55, 1.0)
    as_bool = util.rssi_to_metres(-60, -55, True)
    assert as_bool == util.rssi_to_metres(-60, -55, DEFAULT_ATTENUATION) != numeric
    util.rssi_to_metres.cache_clear()
    assert util.rssi_to_metres(-60, -55, True) == as_bool
    assert util.rssi_to_metres(-60, -55, 1.0) == numeric


def test_rssi_to_metres_always_returns_a_float():
    for args in [(-60, -55, 3), (math.nan, -55, 3), (-60, None, None), (-90, -55, 0.001)]:
        assert isinstance(util.rssi_to_metres(*args), float), args
