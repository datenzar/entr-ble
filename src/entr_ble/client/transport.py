import asyncio

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

import entr_ble.const as const
import entr_ble.crypto as crypto

from ..framing import (
    LOCATION_INLINE,
    LOCATION_PRIMARY,
    MAX_INLINE_PAYLOAD,
    ChunkAssembler,
    build_control_frame,
    build_payload_chunks,
    parse_control_frame,
)
from ..session_crypto import SessionCrypto
from .fields import StatusData

RESPONSE_TIMEOUT = 8.0
# Allow the lock time to process a response before acknowledging it.
ACK_DELAY = 0.4

# Protocol error detail codes.
ERROR_DETAILS = {
    1: "WrongPublicKeyLength",
    2: "CantSendPublicKey",
    3: "UnsupportedCommand",
    4: "UnsupportedCmdInBuff",
    5: "WrongAppIDRandLength",
    6: "WrongSetOwnerLength",
    7: "WrongOwnerCode",
    8: "CantSendSetOwnerResp",
    9: "WrongKDF",
    10: "WrongCreateKeyLength",
    11: "CantSendCreateKeyResp",
    12: "WrongGetKeyLength",
    13: "WrongUserID",
    14: "WrongPIN",
    15: "KeyGenerationError",
    16: "WrongUnlockLength",
    17: "WrongEKey",
    18: "WrongConfigLength",
    19: "WrongCfgParamLength",
    20: "UnsupportedConfig",
    21: "CantSendConfigResp",
    22: "CantSendOpSuccess",
    23: "LockBusy",
    24: "LockMainCommError",
    25: "LockError",
    26: "LockInqEarlyTermination",
    27: "LockTimeout",
    28: "CommandSuspended",
    31: "CalibErrorKeysLimitReached",
    32: "CalibErrorInvalidLockLength",
    33: "ErrorBatteryLow",
    34: "CalibErrorMagnetometerCommError",
    35: "CalibErrorMagneticRadiusWeak",
    36: "CalibErrorLockConfigUndefinedError",
    37: "CalibErrorMagConfigUndefinedError",
    38: "BLUETOOTH_IS_TURNED_OFF",
    39: "LOCATION_IS_TURNED_OFF",
    41: "MAXIMUM_PENDING_USERS",
}

LOCK_BUSY = 23

ERROR_CATEGORIES = {
    1: "BadInput",
    2: "InsufficientResources",
    3: "CommunicationError",
    4: "InternalError",
    5: "AppLegitimateError",
}


class EntrProtocolError(Exception):
    """A malformed or unexpected response, or an unmet protocol/session prerequisite."""


class EntrLockError(EntrProtocolError):
    """OP_ERROR (102): a lock-reported failure with numeric category and detail
    attributes.
    """

    def __init__(self, category: int, detail: int):
        """Create an error from the lock's category and detail codes, preserving unknown
        codes numerically.
        """
        self.category = category
        self.detail = detail
        category_name = ERROR_CATEGORIES.get(category, f"category {category}")
        detail_name = ERROR_DETAILS.get(detail, f"detail {detail}")
        super().__init__(f"lock returned error: {category_name} / {detail_name}")


class EntrConnectionLostError(EntrProtocolError):
    """The BLE link dropped before or while waiting for a response."""


class TransportClient:
    """BLE connection and session state shared by the command mixins.

    Keep one command in flight per client. Command exchanges can raise EntrLockError for
    a lock rejection, EntrProtocolError for malformed responses or missing sessions,
    TimeoutError after eight seconds without a response, and backend BLE exceptions for
    connection or GATT failures.
    """

    def __init__(self, device, timeout=10.0):
        """Create a disconnected client for a Bluetooth address or Bleak BLEDevice.

        timeout is the Bleak connection timeout in seconds, separate from the
        eight-second protocol response timeout. Generates a fresh private_key for
        provisioning; session is initially None, comm_version is empty, and status and
        status_raw are None. Call connect() before commands and disconnect() when
        finished.
        """
        self.address = device.address if isinstance(device, BLEDevice) else device
        self.client = BleakClient(
            device, disconnected_callback=self.handle_disconnect, timeout=timeout
        )
        self._connected = False
        self.private_key = crypto.generate_keypair()
        self.session: SessionCrypto | None = None
        self.comm_version = ""
        self.status: StatusData | None = None
        # Raw DEVICE_STATUS byte, kept aside because OP_DEVICE_CONFIG rebuilds
        # the volume/auto-lock bits on top of the current reading.
        self.status_raw: int | None = None

        # A single request can draw several responses in a row (GetKeys pushes
        # one frame per batch), so completed responses are queued rather than
        # resolved into a one-shot future.
        self._responses: asyncio.Queue[tuple[int | None, bytes] | Exception] = (
            asyncio.Queue()
        )
        self._pending_command: int | None = None
        self._primary = ChunkAssembler()
        self.fota_available = False

    async def connect(self, client: BleakClient | None = None) -> None:
        """Connect and subscribe to lock responses; return None.

        Pass an already connected client (for example one returned by
        bleak_retry_connector.establish_connection, created with
        disconnected_callback=self.handle_disconnect) to reuse it instead of the
        internal BleakClient. Sets fota_available according to whether optional FOTA
        notifications can be enabled. Failure to enable those optional notifications
        does not prevent ordinary commands; connection and required notification
        failures propagate from Bleak.
        """
        if client is not None:
            self.client = client
        if not self.client.is_connected:
            await self.client.connect()
        self._connected = True
        await self.client.start_notify(const.RESPONSE_CONTROL, self._on_control)
        await self.client.start_notify(const.RESPONSE_PRIMARY_PAYLOAD, self._on_primary)
        # The FOTA service carries device info / errors / firmware updates; it
        # is optional on old firmwares, and its absence must not kill the link.
        try:
            await self.client.start_notify(
                const.FOTA_RESPONSE_CONTROL, self._on_control
            )
            await self.client.start_notify(
                const.FOTA_RESPONSE_PAYLOAD, self._on_primary
            )
            self.fota_available = True
        except BleakError, OSError:
            self.fota_available = False

    async def disconnect(self) -> None:
        """Close the BLE connection and return None; backend errors propagate.

        Cached session and status remain on this object. Restore the session with
        kdf_resync() after reconnecting before sending encrypted commands.
        """
        self._connected = False
        await self.client.disconnect()

    def handle_disconnect(self, _client: BleakClient | None = None) -> None:
        """Bleak disconnected_callback: fail the in-flight command immediately.

        Without this, a dropped link would only surface after the response timeout.
        """
        if not self._connected:
            return
        self._connected = False
        self._pending_command = None
        self._queue(EntrConnectionLostError(f"lost connection to {self.address}"))

    def _on_control(self, _char, data: bytes | bytearray) -> None:
        try:
            parsed = parse_control_frame(bytes(data))
        except ValueError as exc:
            self._pending_command = None
            self._queue(EntrProtocolError(str(exc)))
            return
        if parsed.payload_location == LOCATION_INLINE:
            self._queue((parsed.command, parsed.payload or b""))
        elif parsed.payload_location == LOCATION_PRIMARY:
            # Arm the assembler synchronously: the lock can start sending
            # payload chunks before our coroutine resumes.
            self._pending_command = parsed.command
            self._primary.reset(parsed.payload_checksum, parsed.payload_length)
        else:
            self._queue(
                EntrProtocolError(
                    f"unsupported response payload location {parsed.payload_location}"
                )
            )

    def _on_primary(self, _char, data: bytes | bytearray) -> None:
        if self._pending_command is None:
            self._queue(EntrProtocolError("payload arrived without a control frame"))
            return
        try:
            result = self._primary.feed(bytes(data))
        except ValueError as exc:
            self._pending_command = None
            self._queue(EntrProtocolError(str(exc)))
            return
        if result is not None:
            self._queue((self._pending_command, result))
            self._pending_command = None

    def _queue(self, item: tuple[int | None, bytes] | Exception) -> None:
        self._responses.put_nowait(item)

    async def _checked_command(self, command: int, fields: bytes) -> None:
        """Sends a settings/maintenance command and confirms its success echo."""
        outer_command, payload = await self._exchange_encrypted(command, fields)
        if not self._is_explicit_success(outer_command, payload, command):
            raise EntrProtocolError(f"lock did not confirm command {command}")

    async def _get_data_response(self, fields: bytes) -> bytes:
        """Sends GET_DATA and returns its encrypted response (echo 82 included)."""
        payload = await self._send_encrypted(const.CMD_GET_DATA, fields)
        if not payload or payload[0] != const.CMD_GET_DATA_RESPONSE:
            raise EntrProtocolError("no audit trail response from lock")
        if len(payload) < 3 or len(payload) < 3 + payload[2]:
            raise EntrProtocolError("truncated audit trail response")
        return payload

    async def _send_encrypted(self, command: int, fields: bytes) -> bytes | None:
        outer_command, payload = await self._exchange_encrypted(command, fields)
        return payload if outer_command == const.CMD_GENERAL_ENCRYPTED else None

    async def _exchange_encrypted(
        self, command: int, fields: bytes
    ) -> tuple[int, bytes | None]:
        """Returns the outer command and its plaintext or decrypted payload."""
        return await self._exchange_encrypted_on(command, fields, fota=False)

    async def _exchange_encrypted_on(
        self, command: int, fields: bytes, fota: bool
    ) -> tuple[int, bytes | None]:
        if self.session is None:
            raise EntrProtocolError(
                "establish a session before sending encrypted commands"
            )
        plaintext = bytes([command]) + fields
        wire = self.session.encrypt(plaintext)
        response_command, response = await self._send_raw(
            const.CMD_GENERAL_ENCRYPTED, wire, fota=fota
        )
        if response_command != const.CMD_GENERAL_ENCRYPTED:
            return response_command, response
        return response_command, self._decrypt_response(response)

    async def _send_raw(
        self, outer_command: int, payload: bytes, fota: bool = False
    ) -> tuple[int, bytes]:
        control_char = (
            const.FOTA_REQUEST_CHAR_CONTROL if fota else const.REQUEST_CHAR_CONTROL
        )
        payload_char = (
            const.FOTA_REQUEST_CHAR_PAYLOAD if fota else const.REQUEST_CHAR_PAYLOAD
        )
        if not self._connected:
            raise EntrConnectionLostError(f"not connected to {self.address}")
        # Drop anything left over from a previous exchange.
        while not self._responses.empty():
            self._responses.get_nowait()
        self._pending_command = None

        frame = build_control_frame(outer_command, payload)
        await self.client.write_gatt_char(control_char, frame, response=True)
        if len(payload) > MAX_INLINE_PAYLOAD:
            for chunk in build_payload_chunks(payload):
                await self.client.write_gatt_char(payload_char, chunk, response=True)
        return await self._receive()

    async def _receive_encrypted(self) -> bytes | None:
        command, response = await self._receive()
        if command != const.CMD_GENERAL_ENCRYPTED:
            return None
        return self._decrypt_response(response)

    def _decrypt_response(self, response):
        if self.session is None:
            raise EntrProtocolError("encrypted response arrived without a session")
        try:
            return self.session.decrypt(response)
        except ValueError as exc:
            raise EntrProtocolError("invalid encrypted response") from exc

    async def _receive(self) -> tuple[int, bytes]:
        item = await asyncio.wait_for(self._responses.get(), RESPONSE_TIMEOUT)
        if isinstance(item, Exception):
            raise item
        command, response = item
        if command is None:
            raise EntrProtocolError("payload response arrived without a control frame")
        if command == const.CMD_OP_ERROR:
            if len(response) < 4:
                raise EntrProtocolError("truncated lock error response")
            raise EntrLockError(
                category=response[1], detail=response[2] | (response[3] << 8)
            )
        return command, response

    @staticmethod
    def _is_explicit_success(
        outer_command: int, payload: bytes | None, command: int
    ) -> bool:
        """OP_SUCCESS_EXP (101): check that the success response confirms the requested
        command.
        """
        if outer_command == const.CMD_GENERAL_ENCRYPTED:
            return (
                payload is not None
                and len(payload) >= 2
                and payload[0] == const.CMD_OP_SUCCESS_EXP
                and payload[1] == command
            )
        if outer_command == const.CMD_OP_SUCCESS_EXP:
            return payload is not None and len(payload) >= 2 and payload[1] == command
        return False

    async def _send_ack(self, command: int) -> None:
        """Send an encrypted acknowledgment after the lock's processing delay; no reply
        is expected.
        """
        assert self.session is not None
        await asyncio.sleep(ACK_DELAY)
        wire = self.session.encrypt(bytes([command]))
        frame = build_control_frame(const.CMD_GENERAL_ENCRYPTED, wire)
        await self.client.write_gatt_char(
            const.REQUEST_CHAR_CONTROL, frame, response=True
        )
        if len(wire) > MAX_INLINE_PAYLOAD:
            for chunk in build_payload_chunks(wire):
                await self.client.write_gatt_char(
                    const.REQUEST_CHAR_PAYLOAD, chunk, response=True
                )


def validate_response(
    payload: bytes | None, command: int, minimum_length: int
) -> bytes:
    """Check a response's leading command byte and minimum byte length.

    Return the validated payload; raise EntrProtocolError for a missing payload,
    unexpected command or truncated response.
    """
    if not payload or payload[0] != command:
        raise EntrProtocolError(f"unexpected response to command {command}")
    if len(payload) < minimum_length:
        raise EntrProtocolError(f"truncated response to command {command}")
    return payload
