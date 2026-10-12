import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from entr_ble import EntrLockClient, EntrLockError, EntrProtocolError, const
from entr_ble.client.access import OperationStatus
from entr_ble.client.fields import decode_status
from entr_ble.client.transport import EntrConnectionLostError
from entr_ble.crypto import derive_session_key, generate_keypair, public_key_bytes
from entr_ble.framing import build_control_frame, build_payload_chunks
from entr_ble.session_crypto import SessionCrypto


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bleak = AsyncMock()
        with patch("entr_ble.client.transport.BleakClient", return_value=self.bleak):
            self.client = EntrLockClient("AA:BB")
        self.client.session = SessionCrypto(bytes(16))
        self.client._connected = True

    async def test_disconnect_fails_pending_command_immediately(self):
        async def drop_link(*_args, **_kwargs):
            self.client.handle_disconnect(self.bleak)

        self.bleak.write_gatt_char.side_effect = drop_link
        with self.assertRaises(EntrConnectionLostError):
            await asyncio.wait_for(self.client._send_raw(120, b"\x01"), 1)
        # Later commands fail before touching the link.
        self.bleak.write_gatt_char.reset_mock()
        with self.assertRaises(EntrConnectionLostError):
            await self.client._send_raw(120, b"\x01")
        self.bleak.write_gatt_char.assert_not_awaited()

    async def test_intentional_disconnect_does_not_queue_error(self):
        await self.client.disconnect()
        self.client.handle_disconnect(self.bleak)
        self.assertTrue(self.client._responses.empty())

    async def test_connect_reuses_injected_connected_client(self):
        injected = AsyncMock()
        injected.is_connected = True
        await self.client.connect(injected)
        self.assertIs(self.client.client, injected)
        injected.connect.assert_not_awaited()
        self.assertEqual(injected.start_notify.await_count, 4)
        self.assertTrue(self.client.fota_available)

    async def test_chunked_notifications_reassemble_once(self):
        payload = bytes(range(144))
        self.client._on_control(None, build_control_frame(10, payload))
        for chunk in build_payload_chunks(payload):
            self.client._on_primary(None, chunk)
        self.assertEqual(await self.client._receive(), (10, payload))
        self.client._on_primary(None, build_payload_chunks(payload)[-1])
        with self.assertRaises(EntrProtocolError):
            await self.client._receive()

    async def test_malformed_notifications_become_protocol_errors(self):
        for notify, data in (
            (self.client._on_control, b""),
            (self.client._on_control, build_control_frame(const.CMD_OP_ERROR, b"")),
            (self.client._on_primary, b""),
        ):
            with self.subTest(data=data):
                notify(None, data)
                with self.assertRaises(EntrProtocolError):
                    await self.client._receive()
        self.client._on_control(
            None, build_control_frame(const.CMD_OP_ERROR, bytes([102, 5, 14, 0]))
        )
        with self.assertRaises(EntrLockError) as error:
            await self.client._receive()
        self.assertEqual((error.exception.category, error.exception.detail), (5, 14))

    async def test_settings_accept_plaintext_success_and_check_command(self):
        self.client._send_raw = AsyncMock(
            return_value=(const.CMD_OP_SUCCESS_EXP, bytes([101, 47]))
        )
        await self.client._checked_command(47, b"")
        with self.assertRaises(EntrProtocolError):
            await self.client._checked_command(51, b"")

    async def test_send_raw_writes_control_and_chunks(self):
        self.client._receive = AsyncMock(return_value=(25, b""))
        payload = bytes(range(36))
        self.assertEqual(await self.client._send_raw(120, payload), (25, b""))
        writes = self.bleak.write_gatt_char.await_args_list
        self.assertEqual(
            writes[0].args,
            (const.REQUEST_CHAR_CONTROL, build_control_frame(120, payload)),
        )
        self.assertEqual(
            [call.args[1] for call in writes[1:]], build_payload_chunks(payload)
        )

    async def test_device_config_euro_and_niz_request_layouts(self):
        self.client._send_raw = AsyncMock(return_value=(101, bytes([101, 47])))
        app_id = bytes(range(16))
        lock_name = b"FFront          "
        prefix = b"\x2f" + app_id + b"123456654321\x03" + b"\xff" * 4 + lock_name
        for statuses, ending in ((None, b"\x07"), (b"\x11\x22", b"\x11\x22")):
            with self.subTest(statuses=statuses):
                await self.client.set_device_config(
                    app_id,
                    "123456",
                    "654321",
                    3,
                    lock_name,
                    wall_reader_request_status=7,
                    niz_statuses=statuses,
                )
                assert self.client._send_raw.await_args is not None
                assert self.client.session is not None
                command, wire = self.client._send_raw.await_args.args
                self.assertEqual(command, const.CMD_GENERAL_ENCRYPTED)
                self.assertEqual(self.client.session.decrypt(wire), prefix + ending)

    async def test_device_config_rejects_invalid_niz_status_lengths_before_writes(self):
        for statuses in (b"", b"\x11", b"\x11\x22\x33"):
            with self.subTest(statuses=statuses), self.assertRaises(ValueError):
                await self.client.set_device_config(
                    bytes(16), "123456", "123456", 0, bytes(16), niz_statuses=statuses
                )
        self.bleak.write_gatt_char.assert_not_awaited()

    async def test_key_activation_validates_before_acknowledging(self):
        self.client._send_encrypted = AsyncMock(return_value=b"\x13short")
        self.client._send_ack = AsyncMock()
        with self.assertRaises(EntrProtocolError):
            await self.client.get_new_key("123456", bytes(16))
        self.client._send_ack.assert_not_awaited()
        response = (
            bytes([19])
            + bytes(range(32))
            + bytes([1, 4])
            + b"Alice           "
            + bytes([7])
        )
        self.client._send_encrypted.return_value = response
        credentials = await self.client.get_new_key("123456", bytes(16))
        self.assertEqual(credentials["kdf_id"], 7)
        self.assertEqual(credentials["user_id"], b"Alice           ")
        self.client._send_ack.assert_awaited_once_with(41)

    async def test_legacy_config_without_optional_battery_fields(self):
        self.client._send_encrypted = AsyncMock(return_value=bytes([31, 0x40]))
        self.client._send_ack = AsyncMock()
        status = await self.client.get_device_config()
        self.assertIsNone(status["battery_percentage"])
        self.assertEqual(status["battery_state"], "low")
        self.assertTrue(status["locked"])

    async def test_kdf_does_not_reuse_previous_status(self):
        self.client.status = decode_status(0, None, None)
        self.client.status_raw = 0
        self.client._send_raw = AsyncMock(return_value=(20, bytes([20]) + bytes(88)))
        self.assertIsNone(await self.client.kdf_resync(1, 0, bytes(16)))
        self.assertIsNone(self.client.status_raw)

    async def test_invalid_credentials_are_rejected_before_writes(self):
        with self.assertRaises(ValueError):
            await self.client.unlock(bytes(15), bytes(16), bytes(32))
        with self.assertRaises(ValueError):
            await self.client.recover_owner("123", bytes(16))
        self.bleak.write_gatt_char.assert_not_awaited()

    async def test_user_stream_consumes_all_batches(self):
        first = bytes([27, 2, 1]) + b"Alice           " + bytes([1, 1])
        second = bytes([27, 1, 1]) + b"Bob             " + bytes([0, 0])
        self.client._send_encrypted = AsyncMock(return_value=first)
        self.client._receive_encrypted = AsyncMock(return_value=second)
        users = await self.client.list_users("123456", bytes(16))
        self.assertEqual([user["name"] for user in users], ["Alice", "Bob"])

    async def test_numerical_firmware_version_comparison(self):
        self.client.comm_version = "1.29r10"
        self.client.fota_available = True
        payload = bytes([46, 1, 42, 1, 65, 1, 66]) + bytes(4)
        self.client._exchange_encrypted_on = AsyncMock(return_value=(120, payload))
        self.assertEqual((await self.client.get_device_info())["device_id"], "2a")

    async def test_error_log_requires_an_encrypted_complete_response(self):
        self.client.fota_available = True
        self.client._exchange_encrypted_on = AsyncMock()
        for response in ((44, b"\x08\x00"), (120, b"\x08\x02a")):
            self.client._exchange_encrypted_on.return_value = response
            with self.subTest(response=response), self.assertRaises(EntrProtocolError):
                await self.client.get_errors(bytes(8))
        self.client._exchange_encrypted_on.return_value = (120, b"\x08\x00")
        self.assertTrue((await self.client.get_errors(bytes(8)))["empty"])

    async def test_receive_cancellation_leaves_queue_usable(self):
        pending = asyncio.create_task(self.client._receive())
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.client._on_control(None, build_control_frame(25, b""))
        self.assertEqual(await self.client._receive(), (25, b""))

    async def test_operations_require_a_success_response(self):
        self.client._exchange_encrypted = AsyncMock(return_value=(25, b""))
        await self.client.lock(bytes(16), bytes(16), bytes(32))
        self.client._exchange_encrypted.return_value = (44, b"unrelated")
        with self.assertRaises(EntrProtocolError):
            await self.client.unlock(bytes(16), bytes(16), bytes(32))

    async def test_operations_return_parsed_op_status(self):
        self.client._exchange_encrypted = AsyncMock(
            return_value=(25, bytes([25, 7, 2, 0xAB, 0xCD, 0xFF]))
        )
        self.assertEqual(
            await self.client.unlock(bytes(16), bytes(16), bytes(32)),
            OperationStatus(type_id=7, value=b"\xab\xcd"),
        )
        for payload in (b"", bytes([25, 7]), bytes([25, 7, 0]), bytes([25, 7, 3, 1])):
            with self.subTest(payload=payload):
                self.client._exchange_encrypted.return_value = (25, payload)
                self.assertIsNone(
                    await self.client.lock(bytes(16), bytes(16), bytes(32))
                )
        self.client._exchange_encrypted.return_value = (120, bytes([101, 18]))
        self.assertIsNone(await self.client.lock(bytes(16), bytes(16), bytes(32)))

    async def test_idle_status_read_retries_only_while_busy(self):
        config = {"locked": True}
        self.client.get_device_config = AsyncMock(
            side_effect=[EntrLockError(4, 23), EntrLockError(4, 23), config]
        )
        with patch("entr_ble.client.status.asyncio.sleep", AsyncMock()) as sleep:
            self.assertEqual(await self.client.get_device_config_when_idle(), config)
        self.assertEqual(sleep.await_count, 3)
        self.client.get_device_config = AsyncMock(side_effect=EntrLockError(4, 27))
        with (
            patch("entr_ble.client.status.asyncio.sleep", AsyncMock()),
            self.assertRaises(EntrLockError),
        ):
            await self.client.get_device_config_when_idle()
        self.assertEqual(self.client.get_device_config.await_count, 1)
        self.client.get_device_config = AsyncMock(side_effect=EntrLockError(4, 23))
        with (
            patch("entr_ble.client.status.asyncio.sleep", AsyncMock()),
            self.assertRaises(EntrLockError),
        ):
            await self.client.get_device_config_when_idle(attempts=2)
        self.assertEqual(self.client.get_device_config.await_count, 2)

    async def test_truncated_success_is_not_confirmation(self):
        for command, payload in ((101, b""), (101, b"e"), (120, b"e")):
            with self.subTest(command=command):
                self.assertFalse(self.client._is_explicit_success(command, payload, 47))

    async def test_invalid_ciphertext_is_a_protocol_failure(self):
        self.client._send_raw = AsyncMock(return_value=(120, b"short"))
        with self.assertRaises(EntrProtocolError):
            await self.client._exchange_encrypted(30, b"")
        self.client._receive = AsyncMock(return_value=(120, b"short"))
        with self.assertRaises(EntrProtocolError):
            await self.client._receive_encrypted()

    async def test_pairing_and_handshake_use_negotiated_key_and_iv(self):
        remote_key = generate_keypair()
        response = public_key_bytes(remote_key) + bytes(64) + bytes(range(16))
        self.client._send_raw = AsyncMock(return_value=(10, response))
        await self.client.pair()
        peer = SessionCrypto(
            derive_session_key(remote_key, public_key_bytes(self.client.private_key))
        )
        peer.set_iv(bytes(range(16)))
        self.client._send_raw.return_value = (100, b"")
        await self.client.handshake(bytes(range(16)))
        assert self.client._send_raw.await_args is not None
        command, encrypted = self.client._send_raw.await_args.args
        self.assertEqual(command, 120)
        self.assertEqual(peer.decrypt(encrypted), bytes([11]) + bytes(range(16)))

    async def test_pairing_rejects_malformed_keys_and_kdf(self):
        for response in (bytes(143), bytes(144)):
            self.client._send_raw = AsyncMock(return_value=(10, response))
            with (
                self.subTest(length=len(response)),
                self.assertRaises(EntrProtocolError),
            ):
                await self.client.pair()
        self.client._send_raw = AsyncMock(return_value=(20, bytes([20]) + bytes(87)))
        with self.assertRaises(EntrProtocolError):
            await self.client.kdf_resync(1, 0, bytes(16))

    async def test_kdf_supplies_session_iv_and_status(self):
        iv = bytes(range(16))
        self.client._send_raw = AsyncMock(
            return_value=(20, bytes([20]) + iv + bytes(72) + bytes([8, 50, 0]))
        )
        status = await self.client.kdf_resync(1, 0, bytes(16))
        assert status is not None
        assert self.client.session is not None
        self.assertFalse(status["locked"])
        self.assertEqual(status["battery_percentage"], 50)
        self.assertTrue(status.get("passcode_required"))
        self.assertEqual(self.client.session.iv_tail, iv[1:])
