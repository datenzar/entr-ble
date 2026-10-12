from typing import override

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_ADDRESS, CONF_LOCK_NAME, DOMAIN


def device_info(entry):
    address = entry.data[CONF_ADDRESS]
    return DeviceInfo(
        identifiers={(DOMAIN, address)},
        name=entry.data.get(CONF_LOCK_NAME) or "ENTR lock",
        connections={("bluetooth", address)},
        manufacturer="ASSA ABLOY",
        model="ENTR",
    )


class EntrEntity(Entity):
    """Shared wiring: device info, availability and device listener."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key):
        self._entry = entry
        self._device = entry.runtime_data
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}_{key}"
        self._attr_device_info = device_info(entry)
        self._attr_available = getattr(self._device, "available", True)

    @override
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        await self._async_restore()
        self._refresh_attributes()
        self.async_on_remove(self._device.add_listener(self._update_state))

    async def _async_restore(self):
        """Restore the last known value before the first Bluetooth contact."""

    def _update_state(self):
        self._refresh_attributes()
        self.async_write_ha_state()

    def _refresh_attributes(self):
        self._attr_available = self._device.available
        self._set_state()

    def _set_state(self):
        """Copy device state onto the entity; keep restored values when unknown."""
