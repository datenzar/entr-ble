from dataclasses import dataclass

import entr_ble.const as const

from .fields import fixed_bytes
from .transport import EntrProtocolError, TransportClient


@dataclass(frozen=True)
class OperationStatus:
    """OP_STATUS (25) report: ``status_type_id`` and its raw value bytes.

    The firmware's status type ids are undocumented (the official app only logs them),
    so the value is kept raw. Read GET_DEVICE_CONFIG for the decoded lock state.
    """

    type_id: int
    value: bytes


class Access(TransportClient):
    """Credential-based physical lock operations over an established session."""

    async def unlock(
        self, user_id: bytes, app_id: bytes, ble_ekey: bytes, mode: int | None = None
    ) -> OperationStatus | None:
        """UNLOCK (17): unlock using a 16-byte user ID, 16-byte application ID and
        32-byte BLE key.

        Requires an established session and an active credential. Mode 0 selects
        auto-lock and mode 1 manual operation; omitted mode follows cached status,
        falling back to manual when status is unavailable. Returns the parsed
        OP_STATUS report, or None after an explicit success or an empty report; a
        missing confirmation raises EntrProtocolError and lock rejection raises
        EntrLockError. Invalid field lengths raise ValueError.
        """
        if mode is None:
            # Derive the mode from auto-lock: enabled sends 0, disabled sends 1.
            auto_lock = self.status["auto_lock"] if self.status else False
            mode = const.MODE_AUTO if auto_lock else const.MODE_MANUAL
        fields = (
            fixed_bytes(user_id, 16, "user id")
            + fixed_bytes(app_id, 16, "application id")
            + fixed_bytes(ble_ekey, 32, "lock key")
            + bytes([mode])
        )
        return await self._operate(const.CMD_UNLOCK, fields)

    async def lock(
        self, user_id: bytes, app_id: bytes, ble_ekey: bytes
    ) -> OperationStatus | None:
        """LOCK (18): lock using a 16-byte user ID, 16-byte application ID and 32-byte
        BLE key.

        Requires an established session and an active credential; the operation always
        sends mode 0. Returns the parsed OP_STATUS report, or None after an explicit
        success or an empty report; a missing confirmation raises EntrProtocolError and
        lock rejection raises EntrLockError. Invalid field lengths raise ValueError.
        """
        # Lock always uses mode 0.
        fields = (
            fixed_bytes(user_id, 16, "user id")
            + fixed_bytes(app_id, 16, "application id")
            + fixed_bytes(ble_ekey, 32, "lock key")
            + bytes([const.MODE_AUTO])
        )
        return await self._operate(const.CMD_LOCK, fields)

    async def _operate(self, command: int, fields: bytes) -> OperationStatus | None:
        outer_command, payload = await self._exchange_encrypted(command, fields)
        if outer_command == const.CMD_OP_STATUS:
            return parse_operation_status(payload)
        if not self._is_explicit_success(outer_command, payload, command):
            raise EntrProtocolError(f"lock did not confirm command {command}")
        return None


def parse_operation_status(payload: bytes | None) -> OperationStatus | None:
    """Parse ``command(1) || status_type_id(1) || value_length(1) || value``.

    Returns None for empty or truncated reports, matching the official app, which
    ignores them.
    """
    if payload is None or len(payload) < 3:
        return None
    length = payload[2]
    if length == 0 or len(payload) < 3 + length:
        return None
    return OperationStatus(type_id=payload[1], value=bytes(payload[3 : 3 + length]))
