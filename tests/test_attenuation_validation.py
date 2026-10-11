"""Attenuation and ref_power must be usable numbers: zero used to abort the update cycle."""

from __future__ import annotations

import math

import pytest
import voluptuous as vol

from custom_components.bermuda import util
from custom_components.bermuda.config_flow import ATTENUATION_SCHEMA, REF_POWER_SCHEMA


@pytest.mark.parametrize("attenuation", [0, 0.0, -1.0, math.nan, math.inf])
def test_rssi_to_metres_rejects_unusable_attenuation(attenuation):
    """Zero used to raise ZeroDivisionError and abort the update cycle."""
    assert util.rssi_to_metres(-60, -55, attenuation) is False


@pytest.mark.parametrize("value", [0, -2, math.nan, "nan", 50])
def test_attenuation_schema_rejects_bad_values(value):
    with pytest.raises(vol.Invalid):
        ATTENUATION_SCHEMA(value)


def test_attenuation_and_ref_power_schemas_accept_normal_values():
    assert ATTENUATION_SCHEMA("3") == 3.0
    assert REF_POWER_SCHEMA(-55) == -55.0
    with pytest.raises(vol.Invalid):
        REF_POWER_SCHEMA(math.nan)


@pytest.mark.parametrize("ref_power", [math.nan, math.inf, -math.inf])
def test_rssi_to_metres_rejects_unusable_ref_power(ref_power):
    assert util.rssi_to_metres(-60, ref_power, 3.0) is False


def test_rssi_to_metres_rejects_unusable_rssi():
    assert util.rssi_to_metres(math.nan, -55, 3.0) is False


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(0, 0.1), (-2, 0.1), (50, 10.0), (math.nan, 3.0), ("bad", 3.0), (None, 3.0), (2.5, 2.5)],
)
def test_a_stored_attenuation_outside_the_bounds_is_pulled_in_for_the_form(stored, expected):
    from custom_components.bermuda.config_flow import ATTENUATION_RANGE, _form_default

    value = _form_default(stored, 3.0, ATTENUATION_RANGE)
    assert value == expected
    # And the form then accepts it as it stands.
    assert ATTENUATION_SCHEMA(value) == expected
