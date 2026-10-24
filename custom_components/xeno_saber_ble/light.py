"""Blade as a Home Assistant light (on/off, RGB colour, brightness)."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XenoConfigEntry, protocol as p
from .const import DOMAIN
from .device import XenoSaber, XenoSaberError
from .entity import XenoEntity

# State is pushed by the saber; commands are serialized by the device's own lock.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: XenoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([XenoBladeLight(entry.runtime_data.saber)])


class XenoBladeLight(XenoEntity, LightEntity):
    _attr_translation_key = "blade"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "blade")

    @property
    @override
    def is_on(self) -> bool | None:
        return self._saber.blade_on

    @property
    @override
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._saber.color

    @property
    @override
    def brightness(self) -> int | None:
        value = self._state(p.KEY_BRIGHTNESS)
        if not isinstance(value, (int, float)):
            return None
        return round(max(0, min(100, value)) * 255 / 100)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            if (rgb := kwargs.get(ATTR_RGB_COLOR)) is not None:
                await self._saber.set_color(*rgb)
            if (bri := kwargs.get(ATTR_BRIGHTNESS)) is not None:
                await self._saber.send(p.KEY_BRIGHTNESS, max(1, round(bri * 100 / 255)))
            if not self.is_on:
                await self._saber.set_blade(True)
        except XenoSaberError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(p.KEY_POWER_ON, False)
