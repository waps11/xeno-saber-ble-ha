"""Firmware update and board configuration actions: EXPERIMENTAL.

Not shipped: these write firmware and board settings to the saber and have
only been tested against a simulated saber. ``script/build --experimental
board_actions`` copies this file into the integration as ``board.py``, merges
its services.yaml and translations, and services.py registers the actions
(administrators only) when the module is present.
"""

from __future__ import annotations

import hashlib
import logging
import re

try:  # Home Assistant 2026.x replaced voluptuous with probatio
    import probatio as vol
except ImportError:  # older Home Assistant
    import voluptuous as vol  # type: ignore[no-redef]

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv

from . import protocol as p
from .services import (
    _ENTRY,
    ATTR_CONFIRM,
    Handler,
    _invalid,
    _local_path,
    _on_saber,
    _read,
    _require_blade_off,
    _require_confirm,
    _saber,
)

_LOGGER = logging.getLogger(__name__)

ATTR_FILE = "file"
ATTR_TYPE = "type"
ATTR_BUTTONS = "buttons"

SERVICE_UPDATE_FIRMWARE = "update_firmware"
SERVICE_SET_BLADE_FIRMWARE = "set_blade_firmware"
SERVICE_SET_HARDWARE_TYPE = "set_hardware_type"

FIRMWARE_PATH = "update.bin"  # what the app sends; the saber installs it and reboots
FIRMWARE_MIN_BYTES = 16 * 1024
FIRMWARE_MAX_BYTES = 8 * 1024 * 1024
FIRMWARE_MIN_BATTERY = 30
_FIRMWARE_VERSION = re.compile(rb"DMN_[A-Z0-9]+_[A-Z0-9]+_SV\d+\.\d+\.\d+")
_BLADE_TYPE = vol.In([t.lower() for t in p.BLADE_TYPES])

SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_UPDATE_FIRMWARE: vol.Schema(
        {**_ENTRY, vol.Required(ATTR_FILE): cv.string, vol.Required(ATTR_CONFIRM): cv.boolean}
    ),
    SERVICE_SET_BLADE_FIRMWARE: vol.Schema(
        {**_ENTRY, vol.Required(ATTR_TYPE): _BLADE_TYPE, vol.Required(ATTR_CONFIRM): cv.boolean}
    ),
    SERVICE_SET_HARDWARE_TYPE: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_TYPE): _BLADE_TYPE,
            vol.Optional(ATTR_BUTTONS): vol.All(vol.Coerce(int), vol.In([1, 2])),
            vol.Required(ATTR_CONFIRM): cv.boolean,
        }
    ),
}


async def _update_firmware(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    _require_confirm(call)
    saber = _saber(hass, call)
    source = await _local_path(hass, call.data[ATTR_FILE], kind="file")
    data = await _read(hass, source)
    if not FIRMWARE_MIN_BYTES <= len(data) <= FIRMWARE_MAX_BYTES:
        raise _invalid("firmware_size", size=len(data))
    found = _FIRMWARE_VERSION.search(data)
    detected = found.group().decode() if found else None
    await _require_blade_off(saber)
    battery = saber.battery
    if battery is not None and battery < FIRMWARE_MIN_BATTERY:
        raise _invalid("battery_too_low", level=battery, minimum=FIRMWARE_MIN_BATTERY)
    current = saber.state.get(p.KEY_SW_VERSION)
    # The vendor's firmware is not signed: nothing can check it, but the hash
    # records exactly which file was sent.
    sha256 = await hass.async_add_executor_job(lambda: hashlib.sha256(data).hexdigest())
    _LOGGER.warning(
        "Sending firmware %s (%s, %d bytes, SHA-256 %s) to %s (currently %s). Keep "
        "it close and powered until it restarts; later firmware may not work with "
        "this integration",
        source.name,
        detected or "version not found in file",
        len(data),
        sha256,
        saber.name,
        current,
    )
    await _on_saber(saber.send_file(FIRMWARE_PATH, data, header_name=FIRMWARE_PATH))
    return {
        "bytes": len(data),
        "sha256": sha256,
        "previous_version": current,
        "detected_version": detected,
    }


async def _set_blade_firmware(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    _require_confirm(call)
    saber = _saber(hass, call)
    wanted = _blade_type(call.data[ATTR_TYPE])
    await _require_blade_off(saber)
    if saber.state.get(p.KEY_SOFTWARE_TYPE) == wanted:
        return {"type": wanted, "changed": False}
    await _on_saber(saber.send(p.KEY_SOFTWARE_TYPE, wanted))
    await _on_saber(saber.send(p.KEY_RESTART, True))
    return {"type": wanted, "changed": True}


async def _set_hardware_type(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    _require_confirm(call)
    saber = _saber(hass, call)
    wanted = _blade_type(call.data[ATTR_TYPE])
    await _require_blade_off(saber)
    await _on_saber(saber.send(p.KEY_HARDWARE_TYPE, wanted))
    buttons: int | None = call.data.get(ATTR_BUTTONS)
    if buttons is not None:
        await _on_saber(saber.send(p.KEY_BUTTON_NUM, buttons))
    await _on_saber(saber.send(p.KEY_RESTART, True))
    return {"type": wanted, "buttons": buttons}


def _blade_type(value: str) -> str:
    return next(t for t in p.BLADE_TYPES if t.lower() == value)


HANDLERS: dict[str, tuple[Handler, SupportsResponse]] = {
    SERVICE_UPDATE_FIRMWARE: (_update_firmware, SupportsResponse.OPTIONAL),
    SERVICE_SET_BLADE_FIRMWARE: (_set_blade_firmware, SupportsResponse.OPTIONAL),
    SERVICE_SET_HARDWARE_TYPE: (_set_hardware_type, SupportsResponse.OPTIONAL),
}
