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
