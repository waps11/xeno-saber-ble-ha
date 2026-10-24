"""Diagnostics for Xeno Saber BLE (Settings > Devices > the saber > Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import XenoConfigEntry
from .const import CONF_WLED_SOURCE, TESTED_FIRMWARE

# The Bluetooth address identifies the user's saber, the source the adapter
# (its MAC address) or proxy that heard it, and the WLED source is a LAN address.
TO_REDACT = {"address", "unique_id", "source", CONF_WLED_SOURCE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: XenoConfigEntry
) -> dict[str, Any]:
    saber = entry.runtime_data.saber
    sync = entry.runtime_data.sync
    service_info = bluetooth.async_last_service_info(hass, saber.address.upper(), connectable=True)
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "firmware": {
            "reported": saber.state.get("SoftwareVersion"),
            "tested": sorted(TESTED_FIRMWARE),
        },
        "connection": {
            "connected": saber.is_connected,
            "authorized": saber.authorized,
            "idle_disconnect": saber.idle_disconnect,
        },
        "bluetooth": None
        if service_info is None
        else async_redact_data(
            {
                "name": service_info.name,
                "rssi": service_info.rssi,
                "source": service_info.source,
                "connectable": service_info.connectable,
                "service_uuids": service_info.service_uuids,
            },
            TO_REDACT,
        ),
        "color_sync": None
        if sync is None
        else {
            "enabled": sync.enabled,
            "follows_light": bool(sync.follow_entity),
            "wled_udp": sync.wled_udp,
            "wled_source_filter": bool(sync.wled_source),
        },
        # Everything the saber reported: versions, settings, battery, current font...
        "saber_state": dict(saber.state),
    }
