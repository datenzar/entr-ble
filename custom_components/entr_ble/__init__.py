from datetime import timedelta

from .api import EntrDevice
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, PLATFORMS


async def async_setup_entry(hass, entry):
    device = EntrDevice(hass, entry.data)
    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    device.async_start(timedelta(minutes=minutes) if minutes > 0 else None)
    entry.async_on_unload(device.async_stop)
    entry.async_create_background_task(
        hass, device.async_refresh(), "entr_ble_initial_status"
    )
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
