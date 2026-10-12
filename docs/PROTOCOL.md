# ENTR BLE protocol

This document describes the BLE frames implemented by this library and behavior observed on an ENTR EURO with firmware mode 0 and communication version 1.29r5. Other firmware variants may differ.

## Transport and discovery

GATT UUIDs follow `c5e0xxxx-d396-11e3-bb18-0002a5d5c51b`. Service `0100` accepts requests through control characteristic `0101` and payload characteristic `0102`; service `0200` sends notifications through control characteristic `0201` and payload characteristic `0202`. The optional FOTA service uses `0500` and carries device information, error logs and firmware operations.

Every exchange begins with a six-byte control frame: `[command, payload location, length (2 bytes, little-endian), payload checksum, header checksum]`. Location 0 places up to 14 payload bytes after the header. Location 1 sends payload bytes separately in chunks of at most 18 bytes, each prefixed by `[counter, length]`; the final counter has `0xFE` set. Header and payload use independent folded eight-bit checksums with offset `0xA0`.

Advertisements include product id `0xE7`. The next byte contains the advertisement version in its high nibble and lock state in its low nibble. Version 1 also carries an encoding sign and a space-padded lock name. States are 0 uninitialized, 1 initialized, 2 keys pending, 3 fast keys pending and 4 initialized but uncalibrated.

## Security and sessions

ECDH uses secp256r1. The client sends a 64-byte raw public key (`x‖y`, command 10); the lock returns a public key and IV. The session key is `SHA-256(shared secret)[:16]`. Sensitive commands use AES-128-CBC with PKCS7 padding inside command 120 (`GENERAL_ENCRYPTED`). The first IV byte travels with each message, while the remaining 15 bytes stay fixed for the session.

The SEND_PUBLIC_KEY response is `aes_public_key(64) ‖ ecdsa_public_key(64) ‖ iv(16)`. Both keys are raw secp256r1 points; only the first takes part in ECDH.

Connections using saved credentials resynchronize through KDF command 14 with `[kdf_id, role]`. The lock returns a fresh IV, signature and status. A client must retain the AES key, KDF id, application id, user id and lock key needed for later connections.

The 72-byte `BLE_SIGNATURE` field (also returned after HANDSHAKE1) is sized for a DER-encoded ECDSA signature, presumably by the ECDSA key from SEND_PUBLIC_KEY. The signed message is unknown: the official app (ENTR 1.8.7.5, `com.multlock.multilocksdk`) stores the ECDSA key and parses the signature but never verifies it. Its only verification routine uses `NONEwithECDSA` with a placeholder message. Clients therefore cannot authenticate the lock, and the ciphertext carries no MAC. An attacker within radio range that impersonates the lock is not detected.

## Ownership and users

SET_OWNER (12) claims an uninitialized lock with `prev_admin_code(6)="000000" ‖ admin_code(6) ‖ app_id(16) ‖ user_id(16) ‖ mode(1)=0 ‖ lock_name(16) ‖ provider_id(1)`. The lock name begins with an encoding sign, followed by a space-padded name of at most 12 bytes. The response contains a lock key, KDF id and status, followed by an optional battery percentage. The client sends acknowledgment 40 before treating setup as complete.

RECOVER_OWNER (38) replaces the credential in the single owner slot of an initialized lock. Additional devices should use their own user or admin keys if the existing owner must retain access.

CREATE_NEW_KEY (15) creates a pending user with `admin_code(6) ‖ user_id(16) ‖ pin(6) ‖ expiration_hours(1) ‖ role(1) ‖ app_id(16)`. The user id is the name, space-padded or truncated to 16 bytes. GET_NEW_KEY (16) redeems the pin from another device and returns that device's lock key, assigned role, user id and KDF id; acknowledgment 41 completes activation. The request's role byte is a placeholder value of 6.

GET_KEYS (26) lists users in consecutive frames. Each frame carries the number of users remaining, its own batch size and 18 bytes per user. REVOKE_KEY (28), DISABLE_KEY (29) and ENABLE_KEY (32) use the target user's role. SET_ADMIN_CODE (36) sets an admin's personal code using that admin's user id.

Roles are 0 user, 1 admin, 2 owner, 3 remote control, 4 wall reader, 5 integration unit and 6 mobile placeholder. Radio accessories use the fixed pin `p7G513` and no expiration; their radio pairing happens on the hardware.

## Lock operations and settings

| Command | Code | Payload or result |
|---|---|---|
| UNLOCK | 17 | `user_id ‖ app_id ‖ ble_ekey ‖ mode`; mode 0 when auto-lock is enabled, 1 otherwise. |
| LOCK | 18 | Same frame with mode 0. |
| GET_DEVICE_CONFIG | 30 | Lock, door, battery, volume, auto-lock and passcode status. |
| OP_DEVICE_CONFIG | 47 | Settings and owner admin-code changes. |
| OP_LOCK_CALIB | 51 | Mechanical calibration. |
| OP_MAGNET_CALIB | 52 | Door magnet calibration. |
| OP_FACTORY_RESET | 53 | Erases users and settings. |
| GET_LOCK_SN | 72 | Serial number and firmware mode. |
| UPDATE_TIME | 80 | UTC timestamp as six BCD bytes. |
| GET_DEVICE_INFO | 45 | Device id, model, version strings and four-byte update status. |
| GET_DATA | 81 | Audit trail count and records on NIZ firmware. |
| GET_ERRORS | 49 | Firmware error log on the FOTA service. |

OP_DEVICE_CONFIG sends `app_id(16) ‖ prev_admin_code(6) ‖ admin_code(6) ‖ device_status(1) ‖ owner_unlock_code(4) ‖ lock_name(16) ‖ wall_reader_request_status(1)`. The status byte carries volume and the auto-lock bit. Different previous and new admin codes change the owner admin code. For NIZ firmware, the request ends with `wall_reader_status(1) ‖ integration_unit_status(1)` in place of `wall_reader_request_status(1)`. Both status values come from GET_DEVICE_CONFIG.

Mechanical calibration adds `door_direction(1) ‖ lock_type(1)`: left 1, right 3, normal 0 and lift 2. Magnet calibration uses an extra byte, normally 0. Settings, calibration, reset and time commands confirm success with OP_SUCCESS_EXP (101), which echoes the command id.

UNLOCK and LOCK return cleartext OP_STATUS (25) with `command(1) ‖ status_type_id(1) ‖ value_length(1) ‖ value`; failures return OP_ERROR (102) with a category and detail code. The official app logs OP_STATUS without interpreting it, and the status type ids are undocumented. Clients that need the resulting lock state should read GET_DEVICE_CONFIG after the motor has finished; the lock may reply LockBusy (23) while it is still moving. On EURO firmware, UNLOCK actuates a spring latch and can be useful even when the status reports unlocked. The reported locked bit may remain stale after manual operation.

## Status and firmware variants

| Bit | Mask | Meaning |
|---|---|---|
| 0 and 2 | `0x05` | Volume field: 0 high, 1 medium, 4 low, 5 muted. |
| 1 | `0x02` | Auto-lock disabled. |
| 2 | `0x04` | Muted. |
| 3 | `0x08` | Unlocked. |
| 4 | `0x10` | Door open. |
| 5 | `0x20` | Charging. |
| 6 and 7 | `0xC0` | Battery state fallback. |

A valid battery percentage takes precedence over the battery state bits. The passcode field is inverted: 0 requires a code and 1 does not.

GET_LOCK_SN reports firmware mode 0 ENTR_EURO, 1 ENTR_DB, 2 ENTR_S/Yale, 3–4 bridge variants and 7 ENTR_HK. ENTR_S/Yale GET_DEVICE_CONFIG responses include wall-reader status, integration-unit status, door direction and lock type; ENTR_EURO responses omit these fields.

GET_DEVICE_INFO requires the FOTA GATT service and communication version 1.29r3 or newer in this client. Older locks require an additional FOTA IV exchange that is not implemented. GET_ERRORS takes an eight-byte query of unknown meaning; on the tested ENTR EURO, it produces the same empty dump for the tested query values. Protocol errors do not appear in that log.

GET_DATA type 3 reads the audit record count. Type 1 streams records as TLV entries with date, user, credential and event fields until a final `0xFF` marker. NIZ locks use factory audit code `Aa1111`.

Firmware update transfer and pending-key commands 33, 35 and 37 are not implemented by this client.
