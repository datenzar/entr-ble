from typing import override

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass
from homeassistant.const import PERCENTAGE

from .entity import EntrEntity


class EntrBattery(EntrEntity, RestoreSensor):  # pyright: ignore[reportIncompatibleVariableOverride]
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, entry):
        super().__init__(entry, "battery")
        self._attr_native_value = None

    @override
    async def _async_restore(self):
        restored = await self.async_get_last_sensor_data()
        if restored is not None and isinstance(restored.native_value, int):
            self._attr_native_value = restored.native_value

    @override
    def _set_state(self):
        if self._device.status is not None:
            battery = self._device.status.get("battery_percentage")
            if battery is not None:
                self._attr_native_value = battery


async def async_setup_entry(_hass, entry, async_add_entities):
    async_add_entities([EntrBattery(entry)])
