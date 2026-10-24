"""One-shot effects (clash, blaster, force), save/restart, and a reconnect button."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XenoConfigEntry, protocol as p
from .const import DOMAIN
from .device import XenoSaber, XenoSaberError
from .entity import XenoEntity


@dataclass(frozen=True, kw_only=True)
class XenoButtonDescription(ButtonEntityDescription):
    protocol_key: str


BUTTONS = (
    XenoButtonDescription(key="clash", translation_key="clash", protocol_key="Clash"),
    XenoButtonDescription(key="blaster", translation_key="blaster", protocol_key="Blaster"),
    XenoButtonDescription(key="force", translation_key="force", protocol_key="Force"),
    # The app saves settings to flash in a separate step; without it, changes
    # may not survive a reboot.
    XenoButtonDescription(
        key="save_settings",
        translation_key="save_settings",
        protocol_key=p.KEY_SAVE,
        entity_category=EntityCategory.CONFIG,
    ),
    XenoButtonDescription(
        key="restart",
        translation_key="restart",
        protocol_key=p.KEY_RESTART,
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
    ),
)


# State is pushed by the saber; commands are serialized by the device's own lock.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: XenoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    saber = entry.runtime_data.saber
    async_add_entities([*(XenoButton(saber, d) for d in BUTTONS), XenoReconnectButton(saber)])


class XenoButton(XenoEntity, ButtonEntity):
    entity_description: XenoButtonDescription

    def __init__(self, saber: XenoSaber, description: XenoButtonDescription) -> None:
        super().__init__(saber, description.key)
        self.entity_description = description

    @override
    async def async_press(self) -> None:
        # The app sends [2,{"Clash":true}], [2,{"SaveCfg":true}], etc.
        await self._send(self.entity_description.protocol_key, True)


class XenoReconnectButton(XenoEntity, ButtonEntity):
    """Drop the Bluetooth link and open a fresh session."""

    _attr_translation_key = "reconnect"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "reconnect")

    @property
    @override
    def available(self) -> bool:
        return True  # most useful precisely when the link is down

    @override
    async def async_press(self) -> None:
        try:
            await self._saber.reconnect()
        except XenoSaberError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="connection_failed",
                translation_placeholders={"address": self._saber.address, "error": str(err)},
            ) from err
