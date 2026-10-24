"""Switches: saber settings, toggled combat effects, and colour sync."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import XenoConfigEntry, protocol as p
from .const import DOMAIN
from .device import XenoSaber
from .entity import XenoEntity
from .sync import ColorSync, SyncSourceError


@dataclass(frozen=True, kw_only=True)
class XenoSwitchDescription(SwitchEntityDescription):
    protocol_key: str
    # The saber only reports some keys after they change; until then they are off.
    off_until_reported: bool = False


SWITCHES = (
    XenoSwitchDescription(
        key="background_music", translation_key="background_music", protocol_key="BGMOn"
    ),
    XenoSwitchDescription(
        key="motion_control",
        translation_key="motion_control",
        protocol_key="MotionControl",
        entity_category=EntityCategory.CONFIG,
    ),
    XenoSwitchDescription(
        key="lockup",
        translation_key="lockup",
        protocol_key=p.KEY_LOCKUP,
        off_until_reported=True,
    ),
    XenoSwitchDescription(
        key="drag",
        translation_key="drag",
        protocol_key=p.KEY_DRAG,
        off_until_reported=True,
    ),
    # Colour cycling, as on the app's colour screen. Not in the state dump.
    XenoSwitchDescription(
        key="fast_color_changing",
        translation_key="fast_color_changing",
        protocol_key="FastColorChanging",
        off_until_reported=True,
    ),
    XenoSwitchDescription(
        key="smooth_color_changing",
        translation_key="smooth_color_changing",
        protocol_key="SmoothColorChanging",
        off_until_reported=True,
    ),
    # Settings screen: "other control switch" and blade modes.
    *(
        XenoSwitchDescription(
            key=key,
            translation_key=key,
            protocol_key=protocol_key,
            entity_category=EntityCategory.CONFIG,
            entity_registry_enabled_default=enabled,
        )
        for key, protocol_key, enabled in (
            ("flash_on_clash", "ClashOn", True),
            ("countdown", "Countdown", True),
            ("led_screen", "Matrix", False),
            ("velocity_mode", "VelocityMode", True),
            ("torch_mode", "TorchMode", True),
            ("ghost_mode", "GhostMode", True),
            ("lightning_block_mode", "LightningMode", True),
            ("blaster_mode", "BlasterMode", True),
            ("multiblock_mode", "BlockMode", True),
            ("multilock_mode", "BattleMode", True),
            # Gestures (motion control must be on for them to work).
            ("pull_push_ignition", "PullPushSwitchOn", True),
            ("push_pull_retraction", "PushPullSwitchOn", True),
            ("twist_ignition", "TwistOnSwitchOn", True),
            ("twist_retraction", "TwistOffSwitchOn", True),
            ("swing_ignition", "SwingSwitchOn", True),
        )
    ),
)


# State is pushed by the saber; commands are serialized by the device's own lock.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: XenoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = entry.runtime_data
    entities: list[SwitchEntity] = [XenoSwitch(data.saber, d) for d in SWITCHES]
    if data.sync is not None:
        entities.append(XenoColorSyncSwitch(data.saber, data.sync))
    async_add_entities(entities)


class XenoSwitch(XenoEntity, SwitchEntity):
    entity_description: XenoSwitchDescription

    def __init__(self, saber: XenoSaber, description: XenoSwitchDescription) -> None:
        super().__init__(saber, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        value = self._state(self.entity_description.protocol_key)
        if value is None:
            if self.entity_description.off_until_reported and self._saber.is_connected:
                return False
            return None
        return bool(value)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(self.entity_description.protocol_key, True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self.entity_description.protocol_key, False)


class XenoColorSyncSwitch(XenoEntity, SwitchEntity, RestoreEntity):
    """While on, the blade follows the light and/or WLED set in the options."""

    _attr_translation_key = "color_sync"

    def __init__(self, saber: XenoSaber, sync: ColorSync) -> None:
        super().__init__(saber, "color_sync")
        self._sync = sync

    @property
    @override
    def available(self) -> bool:
        return True  # an HA-side setting; works whenever the saber can be reached

    @property
    @override
    def is_on(self) -> bool:
        return self._sync.enabled

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "follow_light": self._sync.follow_entity,
            "wled_udp": self._sync.wled_udp,
        }

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            # A source that cannot start is logged by the sync; the switch stays off.
            with contextlib.suppress(SyncSourceError):
                await self._sync.async_enable()
            self.async_write_ha_state()

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._sync.async_enable()
        except SyncSourceError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="sync_unavailable",
                translation_placeholders={"error": str(err)},
            ) from err
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._sync.async_disable()
        self.async_write_ha_state()
