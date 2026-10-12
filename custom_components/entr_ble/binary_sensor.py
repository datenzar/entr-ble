from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import EntrEntity


class EntrStatusBinarySensor(  # pyright: ignore[reportIncompatibleVariableOverride]
    EntrEntity, RestoreEntity, BinarySensorEntity
):
    """A status flag reported by the lock, restored across restarts."""

    def __init__(self, entry, key, name, read, device_class=None, category=None):
        super().__init__(entry, key)
        self._read = read
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_entity_category = category
        self._attr_is_on = None

    @override
    async def _async_restore(self):
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in (STATE_ON, STATE_OFF):
            self._attr_is_on = restored.state == STATE_ON

    @override
    def _set_state(self):
        if self._device.status is not None:
            value = self._read(self._device.status)
            if value is not None:
                self._attr_is_on = value


def _door_open(status):
    closed = status.get("door_closed")
    return None if closed is None else not closed


async def async_setup_entry(_hass, entry, async_add_entities):
    async_add_entities(
        [
            EntrStatusBinarySensor(
                entry, "door", "Door", _door_open, BinarySensorDeviceClass.DOOR
            ),
            EntrStatusBinarySensor(
                entry,
                "charging",
                "Charging",
                lambda status: status.get("charging"),
                BinarySensorDeviceClass.BATTERY_CHARGING,
                EntityCategory.DIAGNOSTIC,
            ),
            EntrStatusBinarySensor(
                entry,
                "auto_lock",
                "Auto-lock",
                lambda status: status.get("auto_lock"),
                category=EntityCategory.DIAGNOSTIC,
            ),
        ]
    )
