from __future__ import annotations

import asyncio
import io
import json
import sys
import unittest
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from types import ModuleType, SimpleNamespace
from typing import ClassVar, cast
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from bleak.exc import BleakError
from entr_ble_cli.commands import config
from entr_ble_cli.store import LockCredentials
from homeassistant import components
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.lock import LockEntityFeature
from homeassistant.components.lock.const import LockState
from homeassistant.components.sensor import RestoreSensor
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity

from entr_ble.client.fields import decode_status
from entr_ble.const import MANUFACTURER_PRODUCT_ID

bluetooth = ModuleType("homeassistant.components.bluetooth")
bluetooth.__dict__.update(
    {
        "async_ble_device_from_address": Mock(return_value=object()),
        "async_request_active_scan": AsyncMock(),
        "async_discovered_service_info": Mock(return_value=[]),
        "async_register_callback": Mock(return_value=Mock()),
        "async_track_unavailable": Mock(return_value=Mock()),
        "BluetoothCallbackMatcher": dict,
        "BluetoothScanningMode": SimpleNamespace(PASSIVE="passive"),
    }
)
components.__dict__["bluetooth"] = bluetooth
sys.modules[bluetooth.__name__] = bluetooth

from custom_components.entr_ble import api, config_flow, connection, provisioning
from custom_components.entr_ble.binary_sensor import EntrStatusBinarySensor, _door_open
from custom_components.entr_ble.button import EntrCommandButton
from custom_components.entr_ble.const import (
    CONF_ADDRESS,
    CONF_AES_KEY,
    CONF_ROLE,
    CONF_SCAN_INTERVAL,
)
from custom_components.entr_ble.lock import EntrLock
from custom_components.entr_ble.sensor import EntrBattery


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, device, timeout):
        self.device = device
        self.timeout = timeout
        self.comm_version = "1.29r3"
        self.session = SimpleNamespace(key=b"\x11" * 16)
        self.status = {"battery_percentage": 73, "locked": False}
        self.read_back: dict[str, object] | Exception | None = None
        self.calls = []
        self.instances.append(self)

    async def connect(self, client=None):
        self.calls.append("connect")

    def handle_disconnect(self, _client=None):
        self.calls.append("lost")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def fetch_comm_version(self):
        self.calls.append("version")

    async def pair(self):
        self.calls.append("pair")

    async def handshake(self, app_id):
        self.calls.append("handshake")

    async def recover_owner(self, code, app_id):
        self.calls.append(("take_over", code))
        return {"ble_ekey": b"\x22" * 32, "kdf_id": 3, "user_id": b"\x33" * 16}

    async def set_owner(self, code, app_id, user_id, lock_name, provider_id):
        self.calls.append(("initialize", code, provider_id))
        return {"ble_ekey": b"\x22" * 32, "kdf_id": 3}

    async def get_new_key(self, code, app_id):
        self.calls.append(("user_key", code))
        return {
            "ble_ekey": b"\x22" * 32,
            "kdf_id": 3,
            "user_id": b"\x33" * 16,
            "role": 0,
        }

    async def kdf_resync(self, *_args):
        self.calls.append("kdf")

    async def lock(self, *_args):
        self.calls.append("lock")
        self.status = {**self.status, "locked": True}

    async def unlock(self, *_args):
        self.calls.append("unlock")
        self.status = {**self.status, "locked": False}

    async def get_device_config_when_idle(self):
        self.calls.append("read")
        if isinstance(self.read_back, Exception):
            raise self.read_back
        return self.read_back or {**self.status, "battery_percentage": None}


CREDENTIALS = {
    CONF_ADDRESS: "AA:BB",
    "kdf_id": 3,
    "role": 0,
    CONF_AES_KEY: "11" * 16,
    "user_id": "33" * 16,
    "app_id": "aa" * 16,
    "ble_ekey": "22" * 32,
}


@contextmanager
def fake_connection(client_factory: Callable[..., FakeClient] = FakeClient):
    """Route connection.async_connect through FakeClient without real Bluetooth."""
    bleak_client = Mock(disconnect=AsyncMock())
    with (
        patch.object(
            connection, "establish_connection", AsyncMock(return_value=bleak_client)
        ) as establish,
        patch.object(connection, "EntrLockClient", client_factory),
    ):
        yield establish


def fake_hass():
    tasks = []
    hass = SimpleNamespace(
        tasks=tasks,
        async_create_background_task=lambda task, _name: tasks.append(task),
    )
    return cast(HomeAssistant, hass)


class HacsTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_export_matches_home_assistant_import_format(self):
        credentials = LockCredentials(
            "AA",
            "01" * 16,
            "02" * 16,
            "03" * 32,
            4,
            "04" * 16,
            "1.29r3",
            lock_name="Front",
        )
        with (
            patch.object(config, "get_credentials", return_value=credentials),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            config.run(SimpleNamespace(address="AA"))
        imported = config_flow._credentials(
            {"address": "aa", "credentials_json": stdout.getvalue()}
        )
        self.assertEqual(imported, json.loads(stdout.getvalue()))

    async def test_import_credentials_validates_untrusted_json(self):
        credentials = {
            "app_id": "aa" * 16,
            "user_id": "bb" * 16,
            "ble_ekey": "cc" * 32,
            "aes_key": "dd" * 16,
            "kdf_id": 3,
            "role": 2,
        }
        flow = config_flow.EntrConfigFlow()
        for field in (
            "app_id",
            "user_id",
            "ble_ekey",
            "kdf_id",
            "aes_key",
            "role",
        ):
            with self.subTest(missing_field=field):
                result = await flow.async_step_import_credentials(
                    {
                        "credentials_json": json.dumps(
                            credentials | {CONF_ADDRESS: "AA:BB"} | {field: None}
                        )
                    }
                )
                self.assertEqual(result.get("errors"), {"base": "invalid_credentials"})

        for field, value in (
            ("app_id", 123),
            ("aes_key", [0] * 16),
            ("aes_key", "dd " * 10 + "  "),
            ("kdf_id", True),
            ("role", 256),
            ("lock_name", {"unexpected": "object"}),
        ):
            with self.subTest(field=field, value=value):
                result = await flow.async_step_import_credentials(
                    {
                        CONF_ADDRESS: "AA:BB",
                        "credentials_json": json.dumps(
                            credentials | {CONF_ADDRESS: "AA:BB", field: value}
                        ),
                    }
                )
                self.assertEqual(result.get("errors"), {"base": "invalid_credentials"})

        with (
            patch.object(flow, "async_set_unique_id", AsyncMock()) as unique_id,
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            result = await flow.async_step_import_credentials(
                {
                    CONF_ADDRESS: " aa:bb ",
                    "credentials_json": json.dumps(
                        {
                            "AA:BB": credentials
                            | {CONF_ADDRESS: "AA:BB", "lock_name": None}
                        }
                    ),
                }
            )
        self.assertEqual(result.get("data"), credentials | {CONF_ADDRESS: "AA:BB"})
        unique_id.assert_awaited_once_with("aa:bb")

    async def test_lock_can_be_commanded_before_first_bluetooth_contact(self):
        device = SimpleNamespace(async_lock=AsyncMock(), async_unlock=AsyncMock())
        entry = SimpleNamespace(data={CONF_ADDRESS: "AA:BB"}, runtime_data=device)
        lock = EntrLock(entry)
        self.assertTrue(lock.available)
        await lock.async_lock()
        device.async_lock.assert_awaited_once()

    async def test_lock_restores_last_command_and_force_buttons_ignore_displayed_state(
        self,
    ):
        listeners = []
        device = SimpleNamespace(
            locked=None,
            available=True,
            add_listener=lambda listener: listeners.append(listener) or (lambda: None),
            async_lock=AsyncMock(),
            async_unlock=AsyncMock(),
            async_sync=AsyncMock(),
        )
        entry = SimpleNamespace(data={CONF_ADDRESS: "AA:BB"}, runtime_data=device)
        lock = EntrLock(entry)
        with (
            patch.object(RestoreEntity, "async_added_to_hass", AsyncMock()),
            patch.object(
                lock,
                "async_get_last_state",
                AsyncMock(return_value=SimpleNamespace(state=LockState.LOCKED)),
            ),
            patch.object(lock, "async_on_remove"),
            patch.object(lock, "async_write_ha_state"),
        ):
            await lock.async_added_to_hass()
            self.assertTrue(lock.is_locked)
            self.assertTrue(lock.available)
            device.locked = False
            listeners[0]()
            self.assertFalse(lock.is_locked)
            device.available = False
            listeners[0]()
            self.assertFalse(lock.available)
            self.assertFalse(lock.is_locked)
            device.available = True
            self.assertTrue(lock.supported_features & LockEntityFeature.OPEN)
            await lock.async_open()
            device.async_unlock.assert_awaited_once()
            await EntrCommandButton(entry, "lock").async_press()
            device.async_lock.assert_awaited_once()
            await EntrCommandButton(entry, "unlock").async_press()
            self.assertEqual(device.async_unlock.await_count, 2)
            await EntrCommandButton(entry, "sync").async_press()
            device.async_sync.assert_awaited_once()
            self.assertEqual(EntrCommandButton(entry, "lock").name, "Lock")
            self.assertEqual(EntrCommandButton(entry, "unlock").name, "Unlock")
            self.assertEqual(
                EntrCommandButton(entry, "sync").entity_category,
                EntityCategory.DIAGNOSTIC,
            )

    async def test_discovery_scans_once_and_lists_only_entr_locks(self):
        flow = config_flow.EntrConfigFlow()
        flow.hass = cast(HomeAssistant, object())
        info = SimpleNamespace(
            address="AA:BB",
            manufacturer_data={1: bytes([MANUFACTURER_PRODUCT_ID, 0x10])},
            local_name="Front",
            rssi=-40,
        )
        with (
            patch.object(bluetooth, "async_request_active_scan", AsyncMock()) as scan,
            patch.object(
                bluetooth,
                "async_discovered_service_info",
                return_value=[info],
            ),
        ):
            result = await flow.async_step_discover()
        scan.assert_awaited_once()
        schema = result.get("data_schema")
        self.assertIsNotNone(schema)
        assert schema is not None
        self.assertEqual(schema({CONF_ADDRESS: "AA:BB"}), {CONF_ADDRESS: "AA:BB"})

    async def test_discovery_with_no_devices_offers_retry_or_manual_entry(self):
        flow = config_flow.EntrConfigFlow()
        flow.hass = cast(HomeAssistant, object())
        unrelated = SimpleNamespace(
            address="CC:DD", manufacturer_data={}, local_name="Speaker", rssi=-50
        )
        for discovered in ([], [unrelated]):
            with (
                self.subTest(discovered=len(discovered)),
                patch.object(bluetooth, "async_request_active_scan", AsyncMock()),
                patch.object(
                    bluetooth, "async_discovered_service_info", return_value=discovered
                ),
            ):
                result = await flow.async_step_discover()
                self.assertIs(result.get("type"), FlowResultType.MENU)
                self.assertEqual(result.get("step_id"), "no_devices")
                self.assertEqual(result.get("menu_options"), ["discover", "manual"])
                # FlowManager rejects any shown step or menu option without a
                # matching handler with UnknownStep, which surfaced as "Error".
                for step_id in ("no_devices", "discover", "manual"):
                    self.assertTrue(
                        callable(getattr(flow, f"async_step_{step_id}", None)),
                        step_id,
                    )
        menu = await flow.async_step_no_devices()
        self.assertEqual(menu.get("step_id"), "no_devices")
        self.assertEqual(menu.get("menu_options"), ["discover", "manual"])

    async def test_provisioning_modes_disconnect_and_store_separate_credentials(self):
        FakeClient.instances.clear()
        with (
            fake_connection(),
            patch.object(provisioning.os, "urandom", return_value=b"\xaa" * 16),
        ):
            owner = await provisioning.async_provision(
                object(), "AA:BB", "take_over", "123456"
            )
            user = await provisioning.async_provision(
                object(), "AA:BB", "user_key", "654321"
            )
            new_owner = await provisioning.async_provision(
                object(), "AA:BB", "initialize", "123456", "Front"
            )
            with self.assertRaises(ValueError):
                await provisioning.async_provision(
                    object(), "AA:BB", "take_over", "short"
                )
        self.assertEqual(owner[CONF_ROLE], 2)
        self.assertEqual(user[CONF_ROLE], 0)
        self.assertEqual(new_owner[CONF_ROLE], 2)
        self.assertEqual(owner[CONF_AES_KEY], "11" * 16)
        self.assertEqual(owner[CONF_ADDRESS], "AA:BB")
        self.assertIn(("take_over", "123456"), FakeClient.instances[0].calls)
        self.assertIn(("user_key", "654321"), FakeClient.instances[1].calls)
        self.assertIn(("initialize", "123456", 4), FakeClient.instances[2].calls)
        self.assertEqual(len(FakeClient.instances), 3)
        for client in FakeClient.instances:
            self.assertEqual(client.calls[0], "connect")
            self.assertEqual(client.calls[-1], "disconnect")

    async def test_config_flow_requires_owner_confirmation_and_accepts_user_key(self):
        flow = config_flow.EntrConfigFlow()
        flow.hass = cast(HomeAssistant, object())
        self.assertEqual(
            (await flow.async_step_user()).get("menu_options"),
            ["discover", "manual", "import_credentials"],
        )
        with (
            patch.object(flow, "async_set_unique_id", AsyncMock()),
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            result = await flow.async_step_manual({CONF_ADDRESS: "aa:bb"})
        self.assertEqual(result.get("menu_options"), ["owner", "user_key"])
        self.assertEqual(
            (await flow.async_step_owner()).get("menu_options"),
            ["initialize", "take_over"],
        )
        result = await flow.async_step_take_over(
            {"admin_code": "123456", "confirm_take_over": False}
        )
        self.assertEqual(
            (result.get("errors") or {}).get("base"), "confirmation_required"
        )
        owner_data = {CONF_ADDRESS: "AA:BB", CONF_ROLE: 2}
        with patch.object(
            config_flow, "async_provision", AsyncMock(return_value=owner_data)
        ) as provision:
            result = await flow.async_step_take_over(
                {"admin_code": "123456", "confirm_take_over": True}
            )
        self.assertEqual(result.get("data"), owner_data)
        provision.assert_awaited_once_with(
            flow.hass, "AA:BB", "take_over", "123456", None
        )
        data = {CONF_ADDRESS: "AA:BB", CONF_ROLE: 0}
        with patch.object(config_flow, "async_provision", AsyncMock(return_value=data)):
            result = await flow.async_step_user_key({"key_code": "654321"})
        self.assertEqual(result.get("data"), data)

    async def test_battery_restores_last_value_without_polling(self):
        device = SimpleNamespace(
            status=None, available=True, add_listener=lambda _listener: lambda: None
        )
        entry = SimpleNamespace(data={CONF_ADDRESS: "AA:BB"}, runtime_data=device)
        sensor = EntrBattery(entry)
        with (
            patch.object(RestoreSensor, "async_added_to_hass", AsyncMock()),
            patch.object(
                sensor,
                "async_get_last_sensor_data",
                AsyncMock(return_value=SimpleNamespace(native_value=64)),
            ),
            patch.object(sensor, "async_on_remove"),
            patch.object(sensor, "async_write_ha_state"),
        ):
            await sensor.async_added_to_hass()
            self.assertEqual(sensor.native_value, 64)
            device.status = {"battery_percentage": None}
            sensor._update_state()
            self.assertEqual(sensor.native_value, 64)
            device.status = {"battery_percentage": 73}
            sensor._update_state()
            self.assertEqual(sensor.native_value, 73)
        self.assertFalse(sensor.should_poll)

    async def test_lock_action_reads_back_state_and_keeps_session_battery(self):
        FakeClient.instances.clear()
        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        updates = []
        device.add_listener(lambda: updates.append(device.locked))
        with fake_connection() as establish:
            await device.async_lock()
        establish.assert_awaited_once()
        self.assertIs(
            establish.call_args.kwargs["disconnected_callback"].__self__,
            FakeClient.instances[0],
        )
        assert device.status is not None
        self.assertEqual(device.status["battery_percentage"], 73)
        self.assertTrue(device.locked)
        self.assertEqual(updates, [True, True])
        self.assertEqual(
            FakeClient.instances[0].calls,
            ["connect", "kdf", "lock", "read", "disconnect"],
        )

    async def test_read_back_reports_what_the_lock_actually_did(self):
        def jammed(device, timeout):
            client = FakeClient(device, timeout)
            client.read_back = {"locked": False, "battery_percentage": 70}
            return client

        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        with fake_connection(jammed):
            await device.async_lock()
        self.assertFalse(device.locked)
        assert device.status is not None
        self.assertEqual(device.status["battery_percentage"], 70)

    async def test_failed_read_back_keeps_assumed_state(self):
        def busy(device, timeout):
            client = FakeClient(device, timeout)
            client.read_back = api.EntrProtocolError("LockBusy")
            return client

        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        with fake_connection(busy):
            await device.async_lock()
        self.assertTrue(device.locked)
        self.assertTrue(device.last_update_success)

    async def test_startup_read_and_sync_use_reported_state(self):
        FakeClient.instances.clear()
        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        updates = []
        device.add_listener(lambda: updates.append(device.status))
        with fake_connection():
            await device.async_refresh()
            self.assertFalse(device.locked)
            assert device.status is not None
            self.assertEqual(device.status["battery_percentage"], 73)
            await device.async_sync()
            self.assertFalse(device.locked)
        self.assertEqual(len(updates), 2)
        self.assertEqual(
            [client.calls for client in FakeClient.instances],
            [["connect", "kdf", "disconnect"], ["connect", "kdf", "disconnect"]],
        )

    async def test_out_of_range_lock_becomes_unavailable_and_keeps_values(self):
        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        device.status = dict(decode_status(0, 64, None))
        with (
            patch.object(bluetooth, "async_ble_device_from_address", return_value=None),
            self.assertLogs(api._LOGGER, "WARNING") as logs,
        ):
            await device.async_refresh()
            await device.async_refresh()
            with self.assertRaisesRegex(HomeAssistantError, "not in Bluetooth range"):
                await device.async_lock()
        self.assertEqual(len(logs.records), 1)
        assert device.status is not None
        self.assertEqual(device.status["battery_percentage"], 64)
        self.assertIsNone(device.locked)
        self.assertFalse(device.available)
        self.assertFalse(device.last_update_success)

        with fake_connection(), self.assertLogs(api._LOGGER, "INFO") as logs:
            await device.async_sync()
        self.assertIn("responding again", logs.output[0])
        self.assertTrue(device.available)

    async def test_failed_session_disconnects_without_changing_lock_state(self):
        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        device.locked = False
        listener = Mock()
        device.add_listener(listener)
        client = FakeClient(object(), 15)
        with (
            fake_connection(Mock(return_value=client)),
            patch.object(client, "kdf_resync", side_effect=BleakError("Lost link")),
            self.assertLogs(api._LOGGER, "WARNING"),
            self.assertRaisesRegex(HomeAssistantError, "Lost link"),
        ):
            await device.async_lock()
        self.assertFalse(device.locked)
        self.assertTrue(device.available)
        self.assertEqual(client.calls, ["connect", "disconnect"])
        listener.assert_called_once()

    async def test_parallel_sessions_wait_and_cancelled_session_disconnects(self):
        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        connected = asyncio.Event()
        release = asyncio.Event()
        client = FakeClient(object(), 15)

        async def connect(_bleak_client=None):
            connected.set()
            await release.wait()

        factory = Mock(return_value=client)
        with (
            fake_connection(factory) as establish,
            patch.object(client, "connect", side_effect=connect),
        ):
            first = asyncio.create_task(device.async_sync())
            await asyncio.wait_for(connected.wait(), 1)
            second = asyncio.create_task(device.async_sync())
            await asyncio.sleep(0)
            self.assertEqual(factory.call_count, 1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            bleak_client = establish.return_value
            bleak_client.disconnect.assert_awaited_once()
            release.set()
            await asyncio.wait_for(second, 1)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(client.calls, ["kdf", "disconnect"])

    async def test_poll_is_skipped_while_a_session_is_running(self):
        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        with patch.object(device, "async_refresh", AsyncMock()) as refresh:
            async with device._lock:
                await device._async_poll()
            refresh.assert_not_awaited()
            await device._async_poll()
            refresh.assert_awaited_once()

    async def test_unlock_with_auto_lock_follows_up_until_locked(self):
        def auto_lock(device, timeout):
            client = FakeClient(device, timeout)
            client.status = {**client.status, "auto_lock": True}
            return client

        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        cancel = Mock()
        with (
            fake_connection(auto_lock),
            patch.object(api, "async_call_later", return_value=cancel) as later,
        ):
            await device.async_unlock()
            self.assertFalse(device.locked)
            self.assertEqual(later.call_args.args[1], api.AUTO_LOCK_FOLLOW_UPS[0])

            # Still unlocked at the first follow-up: schedule the next one.
            await later.call_args.args[2](None)
            self.assertEqual(later.call_count, 2)
            self.assertEqual(later.call_args.args[1], api.AUTO_LOCK_FOLLOW_UPS[1])

            # A manual lock cancels the pending follow-up and stops the chain.
            await device.async_lock()
            cancel.assert_called_once()
            await later.call_args.args[2](None)
            self.assertEqual(later.call_count, 2)

    async def test_unlock_without_auto_lock_does_not_follow_up(self):
        device = api.EntrDevice(fake_hass(), CREDENTIALS)
        with (
            fake_connection(),
            patch.object(api, "async_call_later") as later,
        ):
            await device.async_unlock()
        later.assert_not_called()

    async def test_presence_tracking_and_polling_subscriptions(self):
        hass = fake_hass()
        device = api.EntrDevice(hass, CREDENTIALS)
        unsubscribers = [Mock(), Mock(), Mock()]
        updates = Mock()
        device.add_listener(updates)
        with (
            patch.object(
                bluetooth, "async_register_callback", return_value=unsubscribers[0]
            ) as register,
            patch.object(
                bluetooth, "async_track_unavailable", return_value=unsubscribers[1]
            ),
            patch.object(
                api, "async_track_time_interval", return_value=unsubscribers[2]
            ) as interval,
        ):
            device.async_start(timedelta(minutes=10))
        self.assertEqual(register.call_args.args[2]["address"], "AA:BB")
        self.assertEqual(interval.call_args.args[2], timedelta(minutes=10))

        device._async_advertisement(None, None)
        updates.assert_not_called()
        device._async_unavailable(None)
        self.assertFalse(device.available)
        device._async_unavailable(None)
        self.assertEqual(updates.call_count, 1)
        with patch.object(device, "async_refresh", AsyncMock()) as refresh:
            device._async_advertisement(None, None)
            self.assertTrue(device.available)
            await cast(SimpleNamespace, hass).tasks[0]
            refresh.assert_awaited_once()

        device.async_stop()
        for unsubscribe in unsubscribers:
            unsubscribe.assert_called_once()

        with patch.object(api, "async_track_time_interval") as interval:
            device.async_start(None)
        interval.assert_not_called()

    async def test_status_binary_sensors_follow_device_status(self):
        device = SimpleNamespace(
            status=None, available=True, add_listener=lambda _listener: lambda: None
        )
        entry = SimpleNamespace(data={CONF_ADDRESS: "AA:BB"}, runtime_data=device)
        door = EntrStatusBinarySensor(
            entry, "door", "Door", _door_open, BinarySensorDeviceClass.DOOR
        )
        with (
            patch.object(RestoreEntity, "async_added_to_hass", AsyncMock()),
            patch.object(
                door,
                "async_get_last_state",
                AsyncMock(return_value=SimpleNamespace(state="on")),
            ),
            patch.object(door, "async_on_remove"),
            patch.object(door, "async_write_ha_state"),
        ):
            await door.async_added_to_hass()
            self.assertTrue(door.is_on)
            device.status = decode_status(0, 50, None)
            door._update_state()
            self.assertFalse(door.is_on)
            device.status = decode_status(0x10, 50, None)
            door._update_state()
            self.assertTrue(door.is_on)
        self.assertEqual(door.unique_id, "AA:BB_door")

    async def test_options_flow_defaults_and_stores_scan_interval(self):
        flow = config_flow.EntrConfigFlow.async_get_options_flow(
            cast(config_flow.config_entries.ConfigEntry, object())
        )
        entry = SimpleNamespace(options={})
        with patch.object(
            type(flow), "config_entry", new_callable=PropertyMock, return_value=entry
        ):
            result = await flow.async_step_init()
            schema = result.get("data_schema")
            assert schema is not None
            self.assertEqual(schema({CONF_SCAN_INTERVAL: 5}), {CONF_SCAN_INTERVAL: 5})
            result = await flow.async_step_init({CONF_SCAN_INTERVAL: 0})
        self.assertEqual(result.get("data"), {CONF_SCAN_INTERVAL: 0})


if __name__ == "__main__":
    unittest.main()
