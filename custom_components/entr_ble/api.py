import asyncio
import logging
from contextlib import suppress

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from entr_ble import EntrProtocolError

from . import connection
from .const import (
    CONF_ADDRESS,
    CONF_AES_KEY,
    CONF_APP_ID,
    CONF_BLE_EKEY,
    CONF_KDF_ID,
    CONF_ROLE,
    CONF_USER_ID,
)

_LOGGER = logging.getLogger(__name__)

# Seconds after an auto-lock unlock at which the lock is read again, stopping as soon
# as it reports itself locked. The lock re-locks on its own (after its delay or once
# the door closes), and nothing else would tell Home Assistant about it.
AUTO_LOCK_FOLLOW_UPS = (30, 60, 120, 240)

_COMMUNICATION_ERRORS = (BleakError, EntrProtocolError, OSError, TimeoutError)


class EntrDevice:
    """One ENTR lock: short BLE sessions per action, cached state, and listeners."""

    def __init__(self, hass, credentials):
        self.hass = hass
        self.credentials = credentials
        self.status = None
        self.locked = None
        self.available = True
        self.last_update_success = True
        self._listeners = []
        self._lock = asyncio.Lock()
        self._unsubscribers = []
        self._cancel_follow_up = None
        self._follow_up_generation = 0

    @property
    def address(self):
        return self.credentials[CONF_ADDRESS]

    def add_listener(self, listener):
        self._listeners.append(listener)

        def remove_listener():
            self._listeners.remove(listener)

        return remove_listener

    @callback
    def async_start(self, scan_interval=None):
        """Track Bluetooth presence and, when ``scan_interval`` is set, poll status."""
        self._unsubscribers.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_advertisement,
                bluetooth.BluetoothCallbackMatcher(
                    address=self.address, connectable=True
                ),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )
        self._unsubscribers.append(
            bluetooth.async_track_unavailable(
                self.hass, self._async_unavailable, self.address, connectable=True
            )
        )
        if scan_interval:
            self._unsubscribers.append(
                async_track_time_interval(
                    self.hass,
                    self._async_poll,
                    scan_interval,
                    name=f"ENTR {self.address} status poll",
                    cancel_on_shutdown=True,
                )
            )

    @callback
    def async_stop(self):
        self._stop_follow_up()
        while self._unsubscribers:
            self._unsubscribers.pop()()

    async def async_lock(self):
        await self._async_execute("lock")

    async def async_unlock(self):
        await self._async_execute("unlock")

    async def async_sync(self):
        await self._async_execute(None)

    async def async_refresh(self):
        """Read status in the background; failures are logged, not raised."""
        with suppress(HomeAssistantError):
            await self._async_execute(None)

    async def _async_poll(self, _now=None):
        if self._lock.locked():
            return
        await self.async_refresh()

    @callback
    def _async_advertisement(self, _service_info, _change):
        if self.available:
            return
        self.available = True
        _LOGGER.debug("ENTR lock %s is advertising again", self.address)
        self._notify_listeners()
        self.hass.async_create_background_task(
            self.async_refresh(), f"ENTR {self.address} refresh after reappearing"
        )

    @callback
    def _async_unavailable(self, _service_info):
        if not self.available:
            return
        self.available = False
        _LOGGER.debug("ENTR lock %s stopped advertising", self.address)
        self._notify_listeners()

    async def _async_execute(self, command):
        async with self._lock:
            if command is not None:
                self._stop_follow_up()
            client = None
            try:
                client = await connection.async_connect(self.hass, self.address)
                await client.kdf_resync(
                    self.credentials[CONF_KDF_ID],
                    self.credentials[CONF_ROLE],
                    bytes.fromhex(self.credentials[CONF_AES_KEY]),
                )
                self._apply_status(client.status)
                if command is not None:
                    await getattr(client, command)(
                        bytes.fromhex(self.credentials[CONF_USER_ID]),
                        bytes.fromhex(self.credentials[CONF_APP_ID]),
                        bytes.fromhex(self.credentials[CONF_BLE_EKEY]),
                    )
                    # The lock accepted the command; show it right away, then
                    # confirm with what the lock reports once the motor has stopped.
                    self.locked = command == "lock"
                    self._notify_listeners()
                    await self._async_read_back(client)
            except connection.LockNotFoundError as err:
                self.available = False
                self._record_failure(err)
                self._notify_listeners()
                raise HomeAssistantError(str(err)) from err
            except _COMMUNICATION_ERRORS as err:
                self._record_failure(err)
                self._notify_listeners()
                raise HomeAssistantError(
                    f"Unable to communicate with ENTR lock {self.address}: {err}"
                ) from err
            finally:
                if client is not None:
                    with suppress(BleakError, OSError):
                        await client.disconnect()
            self._record_success()
            if command == "unlock":
                self._schedule_follow_up(0, self._follow_up_generation)
            self._notify_listeners()

    async def _async_read_back(self, client):
        try:
            status = await client.get_device_config_when_idle()
        except _COMMUNICATION_ERRORS as err:
            _LOGGER.debug(
                "ENTR lock %s did not report its state after the command: %s",
                self.address,
                err,
            )
            return
        self._apply_status(status)

    def _apply_status(self, status):
        if status is None:
            return
        battery = status.get("battery_percentage")
        if battery is None and self.status is not None:
            battery = self.status.get("battery_percentage")
        self.status = {**status, "battery_percentage": battery}
        self.locked = status["locked"]

    def _record_failure(self, err):
        if self.last_update_success:
            _LOGGER.warning("ENTR lock %s is not responding: %s", self.address, err)
        self.last_update_success = False

    def _record_success(self):
        if not self.last_update_success:
            _LOGGER.info("ENTR lock %s is responding again", self.address)
        self.last_update_success = True
        self.available = True

    def _schedule_follow_up(self, index, generation):
        if (
            generation != self._follow_up_generation
            or index >= len(AUTO_LOCK_FOLLOW_UPS)
            or self.locked is not False
            or not (self.status or {}).get("auto_lock")
        ):
            return

        async def follow_up(_now):
            self._cancel_follow_up = None
            await self.async_refresh()
            self._schedule_follow_up(index + 1, generation)

        self._cancel_follow_up = async_call_later(
            self.hass, AUTO_LOCK_FOLLOW_UPS[index], follow_up
        )

    def _stop_follow_up(self):
        self._follow_up_generation += 1
        if self._cancel_follow_up is not None:
            self._cancel_follow_up()
            self._cancel_follow_up = None

    def _notify_listeners(self):
        for listener in list(self._listeners):
            listener()
