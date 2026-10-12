from homeassistant.const import Platform

DOMAIN = "entr_ble"
PLATFORMS = [Platform.LOCK, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONF_ADDRESS = "address"
CONF_APP_ID = "app_id"
CONF_USER_ID = "user_id"
CONF_BLE_EKEY = "ble_ekey"
CONF_KDF_ID = "kdf_id"
CONF_AES_KEY = "aes_key"
CONF_COMM_VERSION = "comm_version"
CONF_ROLE = "role"
CONF_LOCK_NAME = "lock_name"

# Options
CONF_SCAN_INTERVAL = "scan_interval"
# Minutes between status reads; 0 disables polling. Every read is a BLE session that
# wakes the lock, so keep this modest.
DEFAULT_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 1440
