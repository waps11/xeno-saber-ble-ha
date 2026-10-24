"""Diagnostics: is the saber connected, and did it accept the authorization."""

from __future__ import annotations

from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XenoConfigEntry
from .device import XenoSaber
from .entity import XenoEntity

# State is pushed by the saber; commands are serialized by the device's own lock.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: XenoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    saber = entry.runtime_data.saber
    async_add_entities([XenoConnected(saber), XenoAuthorized(saber)])


class XenoConnected(XenoEntity, BinarySensorEntity):
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "connected")

    @property
    @override
    def available(self) -> bool:
        return True  # "off" is the information, not "unavailable"

    @property
    @override
    def is_on(self) -> bool:
        return self._saber.is_connected


class XenoAuthorized(XenoEntity, BinarySensorEntity):
    _attr_translation_key = "authorized"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False  # useful mostly when debugging

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "authorized")

    @property
    @override
    def is_on(self) -> bool | None:
        if not self._saber.is_connected:
            return None
        return bool(self._saber.authorized)
