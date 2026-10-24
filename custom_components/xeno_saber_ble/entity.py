"""Base entity for Xeno Saber BLE."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components import bluetooth
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from . import protocol as p
from .const import DOMAIN, MANUFACTURER, MODEL
from .device import XenoSaber, XenoSaberError


class XenoEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, saber: XenoSaber, key: str) -> None:
        self._saber = saber
        self._attr_unique_id = f"{saber.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, saber.address)},
            connections={(CONNECTION_BLUETOOTH, saber.address)},
            name=saber.name,
            manufacturer=MANUFACTURER,
            model=saber.state.get(p.KEY_HW_VERSION) or MODEL,
            sw_version=saber.state.get(p.KEY_SW_VERSION),
        )

    @property
    @override
    def available(self) -> bool:
        if self._saber.is_connected:
            return True
        # On-demand mode: usable as long as the saber is advertising.
        return bool(self._saber.idle_disconnect) and bluetooth.async_address_present(
            self.hass, self._saber.address, connectable=True
        )

    def _state(self, key: str) -> Any:
        return self._saber.state.get(key)

    async def _send(self, key: str, value: Any) -> None:
        try:
            await self._saber.send(key, value)
        except XenoSaberError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    @override
    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._saber.register_callback(self.async_write_ha_state))
