"""Constants for Xeno Saber BLE."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "xeno_saber_ble"
MANUFACTURER = "LGT / Damiensaber"
MODEL = "Xenopixel V3"

CONF_IDLE_DISCONNECT = "idle_disconnect"
DEFAULT_IDLE_DISCONNECT = 0  # 0 = stay connected

# Keepalive stops the saber from sleeping, which drains its battery. Off by default.
CONF_KEEPALIVE = "keepalive"
DEFAULT_KEEPALIVE = 0  # 0 = off

# Colour sync sources (both optional; the "Colour sync" switch turns syncing on).
CONF_SYNC_LIGHT = "sync_light"  # entity_id of a light to follow
CONF_WLED_UDP = "wled_udp"  # listen for WLED UDP notifier packets
CONF_WLED_SOURCE = "wled_udp_source"  # optional: only obey this IP address

ISSUE_KEEPALIVE = "keepalive_battery_drain"

# Saber firmware (SoftwareVersion) this integration has been tested with.
# The protocol is undocumented and the manufacturer can change it in any
# update, so other versions get a one-time warning in the log.
TESTED_FIRMWARE: frozenset[str] = frozenset({"DMN_XENO_B_SV1.4.0"})

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
