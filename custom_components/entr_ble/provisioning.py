import os
from contextlib import suppress

from bleak.exc import BleakError

from entr_ble import EntrProtocolError
from entr_ble.client import build_lock_name, user_id_bytes
from entr_ble.const import ROLE_OWNER

from . import connection
from .const import (
    CONF_ADDRESS,
    CONF_AES_KEY,
    CONF_APP_ID,
    CONF_BLE_EKEY,
    CONF_COMM_VERSION,
    CONF_KDF_ID,
    CONF_LOCK_NAME,
    CONF_ROLE,
    CONF_USER_ID,
)


async def async_provision(hass, address, method, code, lock_name=None):
    if method not in {"initialize", "take_over", "user_key"}:
        raise ValueError(f"Unknown setup method: {method}")
    if len(code) != 6 or not code.isascii():
        raise ValueError("A six-character ASCII code is required")
    encoded_name = b""
    if method == "initialize":
        if not lock_name:
            raise ValueError("A lock name is required")
        encoded_name = build_lock_name(lock_name)

    app_id = os.urandom(16)
    client = await connection.async_connect(hass, address)
    try:
        await client.fetch_comm_version()
        await client.pair()
        await client.handshake(app_id)
        if method == "initialize":
            user_id = user_id_bytes("homeassistant")
            result = await client.set_owner(code, app_id, user_id, encoded_name, 4)
            role = ROLE_OWNER
        elif method == "take_over":
            result = await client.recover_owner(code, app_id)
            user_id = result["user_id"]
            role = ROLE_OWNER
        else:
            result = await client.get_new_key(code, app_id)
            user_id = result["user_id"]
            role = result["role"]

        if client.session is None:
            raise EntrProtocolError("Pairing did not create an encrypted session")
        data = {
            CONF_ADDRESS: address,
            CONF_APP_ID: app_id.hex(),
            CONF_USER_ID: user_id.hex(),
            CONF_BLE_EKEY: result["ble_ekey"].hex(),
            CONF_KDF_ID: result["kdf_id"],
            CONF_AES_KEY: client.session.key.hex(),
            CONF_COMM_VERSION: client.comm_version,
            CONF_ROLE: role,
        }
        if lock_name:
            data[CONF_LOCK_NAME] = lock_name
        return data
    finally:
        with suppress(BleakError, OSError):
            await client.disconnect()
