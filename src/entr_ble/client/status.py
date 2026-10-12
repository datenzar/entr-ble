import asyncio
from typing import TypedDict

import entr_ble.const as const

from .fields import StatusData, decode_status
from .transport import (
    LOCK_BUSY,
    EntrLockError,
    EntrProtocolError,
    TransportClient,
    validate_response,
)


class LockSerial(TypedDict):
    """Lock serial text, numeric firmware mode, mode label and hexadecimal response
    payload.
    """

    serial: str
    firmware_mode: int
    firmware: str
    raw: str


class BasicDeviceConfig(StatusData):
    """Decoded StatusData fields and hexadecimal GET_DEVICE_CONFIG response payload."""

    raw: str


class NizDeviceConfig(BasicDeviceConfig):
    """Device configuration with raw wall-reader, integration-unit, door-direction and
    lock-type bytes.
    """

    wall_reader_status: int
    integration_unit_status: int
    door_direction: int
    lock_type: int


type DeviceConfig = BasicDeviceConfig | NizDeviceConfig


class Status(TransportClient):
    """Read lock identity and refresh cached device status."""

    async def get_lock_sn(self) -> LockSerial:
        """GET_LOCK_SN (72): return the serial number and firmware-family mode.

        Requires a connected BLE client but no encrypted session. Returns LockSerial
        with the serial text, numeric mode, known mode label (or an unknown label), and
        hexadecimal payload. A missing, unexpected or truncated response raises
        EntrProtocolError; lock rejection raises EntrLockError.
        """
        # The payload byte is an unused placeholder; the command id is in the
        # outer control frame.
        _, payload = await self._send_raw(const.GET_LOCK_SN, bytes([0]))
        payload = validate_response(payload, const.GET_LOCK_SN_RESPONSE, 3)
        length = payload[1]
        if len(payload) < 3 + length:
            raise EntrProtocolError("truncated lock serial number")
        serial = payload[2 : 2 + length].decode("ascii", errors="replace")
        firmware_mode = payload[2 + length]
        return {
            "serial": serial,
            "firmware_mode": firmware_mode,
            "firmware": const.FIRMWARE_MODES.get(
                firmware_mode, f"unknown ({firmware_mode})"
            ),
            "raw": payload.hex(),
        }

    async def get_device_config(self) -> DeviceConfig:
        """GET_DEVICE_CONFIG (30): read current settings and physical status.

        Requires an established session. Returns BasicDeviceConfig or, when the extended
        fields are present, NizDeviceConfig; battery percentage may be None and
        passcode_required is omitted when unknown. Also refreshes status and status_raw
        and acknowledges the response. A missing, unexpected or truncated response
        raises EntrProtocolError; lock rejection raises EntrLockError.
        """
        response = await self._send_encrypted(const.CMD_GET_DEVICE_CONFIG, b"")
        response = validate_response(response, const.CMD_GET_DEVICE_CONFIG_RESPONSE, 2)
        await self._send_ack(const.CMD_GET_DEVICE_CONFIG_RESPONSE_ACK)
        self.status_raw = response[1]
        config: BasicDeviceConfig = {
            **decode_status(
                response[1],
                response[2] if len(response) > 2 else None,
                response[3] if len(response) > 3 else None,
            ),
            "raw": response.hex(),
        }
        if len(response) >= 8:
            # ENTR_S/Yale firmware includes these fields. ENTR_EURO returns the
            # shorter form; its firmware handles door orientation internally.
            niz_config: NizDeviceConfig = {
                **config,
                "wall_reader_status": response[4],
                "integration_unit_status": response[5],
                "door_direction": response[6],
                "lock_type": response[7],
            }
            self.status = niz_config
            return niz_config
        self.status = config
        return config

    async def get_device_config_when_idle(
        self, attempts: int = 4, delay: float = 1.5
    ) -> DeviceConfig:
        """Read GET_DEVICE_CONFIG, retrying while the lock reports LockBusy.

        Use after LOCK/UNLOCK: the motor keeps running after the command is
        confirmed, and the lock rejects status reads with LockBusy (23) meanwhile.
        Waits ``delay`` seconds before each attempt; other errors propagate
        immediately and the last LockBusy error propagates after ``attempts`` tries.
        """
        for attempt in range(1, attempts + 1):
            await asyncio.sleep(delay)
            try:
                return await self.get_device_config()
            except EntrLockError as exc:
                if exc.detail != LOCK_BUSY or attempt == attempts:
                    raise
        raise AssertionError("unreachable")
