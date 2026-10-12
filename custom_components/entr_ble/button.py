from typing import override

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory

from .entity import EntrEntity


class EntrCommandButton(EntrEntity, ButtonEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    def __init__(self, entry, command):
        super().__init__(entry, f"{command}_button")
        self._command = command
        self._attr_name = command.capitalize()
        if command == "sync":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @override
    async def async_press(self):
        if self._command == "lock":
            await self._device.async_lock()
        elif self._command == "unlock":
            await self._device.async_unlock()
        else:
            await self._device.async_sync()


async def async_setup_entry(_hass, entry, async_add_entities):
    async_add_entities(
        [
            EntrCommandButton(entry, "lock"),
            EntrCommandButton(entry, "unlock"),
            EntrCommandButton(entry, "sync"),
        ]
    )
