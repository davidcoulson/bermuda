"""Secrets stay out of logs, diagnostics and non-admin hands.

An Identity Resolving Key resolves a phone's rotating addresses for as long
as the key lives, so it is key material like the FindMy keys: never written
out in full. The dump_devices service lists every address the house has
heard, so a signed-in caller must be an administrator.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import Unauthorized

from custom_components.bermuda.bermuda_irk import BermudaIrkManager
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator

IRK = bytes(range(16))  # 000102...0f
# A resolvable private address that IRK resolves (prand 40:00:00 -> hash).
# Computed once with the real cipher so the test does not depend on it.


def _resolving_address(irk: bytes) -> str:
    from custom_components.bermuda.bermuda_irk import get_cipher_for_irk
    from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401

    cipher = get_cipher_for_irk(irk)
    prand = bytes([0x40, 0x00, 0x01])
    encryptor = cipher.encryptor()
    block = bytes(13) + prand
    ah = (encryptor.update(block) + encryptor.finalize())[-3:]
    return ":".join(f"{b:02x}" for b in prand + ah)


def test_irk_diagnostics_and_logs_never_carry_a_full_key(caplog):
    manager = BermudaIrkManager()
    caplog.set_level(logging.DEBUG, logger="custom_components.bermuda.bermuda_irk")
    manager.add_irk(IRK)
    address = _resolving_address(IRK)
    assert manager.check_mac(address) == IRK  # the key does resolve this address
    manager.check_mac("7a:aa:bb:cc:dd:ee")  # a resolvable address it does not

    blob = json.dumps(manager.async_diagnostics_no_redactions())
    assert IRK.hex() not in blob
    assert "0001..." in blob  # enough of a prefix to tell entries apart
    assert IRK.hex() not in caplog.text
    assert "Saved NEW Macirk pair" in caplog.text


@pytest.mark.asyncio
async def test_dump_devices_refuses_non_admin_users():
    users = {"admin": SimpleNamespace(is_admin=True), "kid": SimpleNamespace(is_admin=False)}

    async def get_user(user_id):
        return users.get(user_id)

    fake = SimpleNamespace(hass=SimpleNamespace(auth=SimpleNamespace(async_get_user=get_user)), devices={})
    dump = BermudaDataUpdateCoordinator.service_dump_devices

    def call(user_id):
        return ServiceCall(fake.hass, "bermuda", "dump_devices", {}, Context(user_id=user_id))

    with pytest.raises(Unauthorized):
        await dump(fake, call("kid"))
    with pytest.raises(Unauthorized):
        await dump(fake, call("nobody"))
    assert await dump(fake, call("admin")) == {}  # admin: the (empty) dump
    assert await dump(fake, call(None)) == {}  # automations carry no user
