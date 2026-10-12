from contextlib import suppress

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth

from entr_ble import EntrLockClient

CONNECT_ATTEMPTS = 3
CLIENT_TIMEOUT = 15


class LockNotFoundError(BleakError):
    """The lock is not currently reachable by any connectable Bluetooth scanner."""


async def async_connect(hass, address):
    """Return a connected EntrLockClient, using Home Assistant's retrying connector.

    ``establish_connection`` handles stale BlueZ state, connection slot exhaustion and
    switching to whichever adapter or ESPHome proxy currently sees the lock best. The
    caller owns the returned client and must disconnect it.
    """
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        raise LockNotFoundError(f"ENTR lock {address} is not in Bluetooth range")

    client = EntrLockClient(ble_device, timeout=CLIENT_TIMEOUT)
    bleak_client = await establish_connection(
        BleakClientWithServiceCache,
        ble_device,
        f"ENTR {address}",
        disconnected_callback=client.handle_disconnect,
        max_attempts=CONNECT_ATTEMPTS,
        ble_device_callback=lambda: (
            bluetooth.async_ble_device_from_address(hass, address, connectable=True)
            or ble_device
        ),
    )
    try:
        await client.connect(bleak_client)
    except BaseException:
        with suppress(BleakError, OSError):
            await bleak_client.disconnect()
        raise
    return client
