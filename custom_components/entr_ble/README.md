# ENTR BLE · Home Assistant

**Control your ENTR Bluetooth lock from Home Assistant.** Add a lock entity, battery and door sensors, and direct control buttons using this HACS custom integration.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/johannlejeune/entr-ble)
![Integration usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.entr_ble.total)

[Project overview](../../README.md) · [CLI](../../packages/entr-ble-cli/README.md) · [Python library](../../src/entr_ble/README.md) · [Protocol notes](../../docs/PROTOCOL.md)

This is an independent project, unaffiliated with ASSA ABLOY.

> [!NOTE]
> **datenzar fork.** This build adds status polling, door/charging sensors, availability tracking, state read-back after commands, auto-lock follow-ups, and connections through `bleak-retry-connector`. It installs the matching `entr-ble` library wheel from this fork's GitHub release instead of PyPI. To use it, add `https://github.com/datenzar/entr-ble` in HACS under **Custom repositories** (category **Integration**) and download the latest `-datenzar` release. Credentials and entities from the upstream integration carry over.

## Requirements

- **Home Assistant 2026.9.3+** and HACS.
- Home Assistant's Bluetooth integration with connectable Bluetooth coverage in range of the lock.
- A new or factory-reset lock, or an existing lock with an owner admin code, user activation code, or CLI credentials.

## Install with HACS

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=johannlejeune&repository=entr-ble&category=integration)

1. Open the repository using the button above. Alternatively, add `https://github.com/johannlejeune/entr-ble` in HACS under **Custom repositories**, with category **Integration**.
2. Download **ENTR BLE** through HACS.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → ENTR BLE**, or use the button below.

[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=entr_ble)

## Choose how to access the lock

The setup flow lets you **Find a nearby lock** or **Enter a Bluetooth address**, then choose an access method.

| Method | What you need | What happens |
| --- | --- | --- |
| **Make Home Assistant the owner → Initialize a new lock** | An uninitialized lock, a new admin code, and a lock name | Home Assistant becomes the first owner |
| **Make Home Assistant the owner → Replace the current owner** | The current owner's admin code | Home Assistant replaces the existing owner credential |
| **Use an additional user key** | A six-character activation code created by the owner | Home Assistant gets its own user key; the owner keeps access |
| **Import existing credentials** | JSON exported by the CLI | Home Assistant uses a copy of the existing key |

> [!WARNING]
> The lock has a single owner slot. Replacing the owner revokes the previous owner's credential, including the mobile app's owner access. Use an additional user key to keep the mobile app as owner.

For an additional key, first create a user for Home Assistant in the mobile app or with another owner client. Redeem its activation code before the code expires.

### Use credentials from the CLI

On the machine holding the saved key:

```sh
entr-ble-cli export-homeassistant ADDRESS
```

In the integration's initial setup menu, choose **Import existing credentials** and paste the complete output into **Credentials JSON**. The Bluetooth address is read from the exported JSON.

Export does not connect to the lock or alter credentials. The integration stores its own copy. Both the exported JSON and Home Assistant's stored credentials contain access keys; keep them private and out of version control.

## Entities and actions

| Entity | Purpose |
| --- | --- |
| Lock | Standard lock/unlock actions and displayed lock state |
| Battery sensor | Battery level from the latest successful BLE read |
| Door sensor | Door open/closed as reported by the lock's door sensor |
| Charging and Auto-lock sensors, under Diagnostics | Whether the battery is charging and whether auto-lock is enabled |
| Lock button | Send a lock command regardless of displayed state |
| Unlock button | Send an unlock command regardless of displayed state |
| Sync button, under Diagnostics | Read the lock's reported state and battery |

The lock also supports Home Assistant's `lock.open` action, which sends `UNLOCK` even when its displayed state is already unlocked. Replace the example entity id with the one assigned in your installation:

```yaml
action: lock.open
target:
  entity_id: lock.front_door
```

## How state updates

After a restart, the integration restores the last displayed values and performs one background BLE read. Every connection is closed after use.

- **Polling.** The lock is read in the background every 10 minutes by default. Change the interval under **Settings → Devices & services → ENTR BLE → Configure**; `0` disables polling. Each read is a short Bluetooth connection, so shorter intervals use more lock battery.
- **Commands.** After Lock or Unlock is accepted, the lock entity shows the commanded state immediately, then the lock's own report once the motor has stopped (for example, a bolt that could not extend shows as unlocked).
- **Auto-lock.** When auto-lock is enabled, the lock re-locks on its own after an unlock. The integration reads it again 30 s, 1, 2 and 4 minutes later and stops as soon as it reports locked.
- **Availability.** Entities become unavailable when no Bluetooth adapter or proxy has seen the lock advertise for a while, or when the lock cannot be found for a command. They come back, with a fresh read, when it advertises again. A failed read keeps the last values and logs one warning until the lock responds again.

Connections go through Home Assistant's Bluetooth stack, retrying and choosing whichever adapter or ESPHome Bluetooth proxy sees the lock best. The lock's reported state can be stale after manual operation; use the direct buttons or `lock.open` when you need to send a command regardless of the displayed state. See [protocol notes](../../docs/PROTOCOL.md) for the observed firmware limitations.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Lock missing from setup scan | Move it into Bluetooth range, scan again, or enter the address manually |
| Connection fails | Check Home Assistant's Bluetooth coverage and that the lock is reachable |
| Setup code rejected | Check the code and access method; new-lock initialization only applies to an uninitialized lock |
| Import rejected | Export again for the same address and paste the complete JSON |
| Displayed state is unexpected | Use Sync to request a read; use a direct button if the lock reports stale state |
| Values remain after a failed read | They are retained values from an earlier read or command; a warning in the log tells you the lock is not responding |
| Entities unavailable | No Bluetooth adapter or proxy currently sees the lock; add an ESPHome Bluetooth proxy near the door |

[Report an issue](https://github.com/johannlejeune/entr-ble/issues) with your Home Assistant and integration versions, lock model/firmware, action attempted, and error. Remove credentials and passwords from logs or screenshots.

## Development and license

See the [repository development guide](../../README.md#development) for the Home Assistant dependency group, simulated tests, and validation workflow.

Licensed under the [MIT License](../../LICENSE).
