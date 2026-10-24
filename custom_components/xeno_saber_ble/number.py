"""Numeric settings, with the ranges the official app allows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XenoConfigEntry, protocol as p
from .device import XenoSaber
from .entity import XenoEntity


@dataclass(frozen=True, kw_only=True)
class XenoNumberDescription(NumberEntityDescription):
    protocol_key: str
    # The saber stores "threshold" values (lower = more sensitive). Like the app,
    # show them as sensitivity: displayed = invert_from - stored.
    invert_from: int | None = None
    # Only meaningful when the saber reports this (key, value), e.g. a side blade
    # is fitted. Unavailable when it reports something else, as in the app.
    requires: tuple[str, object] | None = None


def _ms(
    key: str,
    protocol_key: str,
    low: int,
    high: int,
    *,
    step: int = 100,
    enabled: bool = True,
    requires: tuple[str, object] | None = None,
) -> XenoNumberDescription:
    return XenoNumberDescription(
        key=key,
        translation_key=key,
        protocol_key=protocol_key,
        native_min_value=low,
        native_max_value=high,
        native_step=step,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=NumberDeviceClass.DURATION,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=enabled,
        requires=requires,
    )


def _sensitivity(key: str, protocol_key: str, maximum: int) -> XenoNumberDescription:
    return XenoNumberDescription(
        key=key,
        translation_key=key,
        protocol_key=protocol_key,
        native_min_value=0,
        native_max_value=maximum,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        invert_from=maximum,
    )


NUMBERS = (
    XenoNumberDescription(
        key="volume",
        translation_key="volume",
        protocol_key=p.KEY_VOLUME,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
    ),
    # Same value as the light's brightness, as a 0-100 % setting like the app's slider.
    XenoNumberDescription(
        key="blade_brightness",
        translation_key="blade_brightness",
        protocol_key=p.KEY_BRIGHTNESS,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    ),
    XenoNumberDescription(
        key="main_blade_length",
        translation_key="main_blade_length",
        protocol_key="MainBladeLength",
        native_min_value=0,
        native_max_value=144,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        requires=("MainBlade", True),
    ),
    XenoNumberDescription(
        key="side_blade_length",
        translation_key="side_blade_length",
        protocol_key="SideBladeLength",
        native_min_value=0,
        native_max_value=144,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        requires=("SideBlade", True),
    ),
    _ms("side_blade_delay", "SideBladeDelay", 0, 5000, enabled=False, requires=("SideBlade", True)),
    # "Power on/off time" in the app: how long to hold the button to wake the
    # saber from deep sleep, and to send it into deep sleep.
    _ms("power_on_time", "AwakenTime", 1000, 10000),
    _ms("power_off_time", "SleepTime", 6000, 30000),
    XenoNumberDescription(
        key="auto_deep_sleep",
        translation_key="auto_deep_sleep",
        protocol_key="IdleSleepTime",
        native_min_value=1,
        native_max_value=60,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=NumberDeviceClass.DURATION,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    _ms("ignition_time", "OnTime", 0, 60000, step=1),
    _ms("retraction_time", "OffTime", 0, 60000, step=1),
    _ms("preon_time", "PreonTime", 0, 5000, enabled=False),
    _sensitivity("clash_sensitivity", "ClashSensitivity", 30),
    _sensitivity("swing_sensitivity", "SwingSensitivity", 1000),
    _sensitivity("twist_sensitivity", "TwistSensitivity", 1000),
    _sensitivity("push_sensitivity", "PushSensitivity", 50),
    _sensitivity("pull_sensitivity", "PullSensitivity", 50),
    XenoNumberDescription(
        key="accent_swing_sensitivity",
        translation_key="accent_swing_sensitivity",
        protocol_key="SwingSoundSensitivity",
        native_min_value=1,
        native_max_value=20,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    ),
    XenoNumberDescription(
        key="motor_speed",
        translation_key="motor_speed",
        protocol_key="MotorSpeed",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        requires=(p.KEY_HARDWARE_TYPE, "Pixel"),  # rotating crystal chamber
    ),
)


# State is pushed by the saber; commands are serialized by the device's own lock.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: XenoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(XenoNumber(entry.runtime_data.saber, d) for d in NUMBERS)


class XenoNumber(XenoEntity, NumberEntity):
    entity_description: XenoNumberDescription

    def __init__(self, saber: XenoSaber, description: XenoNumberDescription) -> None:
        super().__init__(saber, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        requires = self.entity_description.requires
        if requires is not None:
            reported = self._state(requires[0])
            if reported is not None and reported != requires[1]:
                return False
        return super().available

    @property
    @override
    def native_value(self) -> float | None:
        value = self._state(self.entity_description.protocol_key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        invert = self.entity_description.invert_from
        return value if invert is None else invert - value

    @override
    async def async_set_native_value(self, value: float) -> None:
        invert = self.entity_description.invert_from
        stored = int(value) if invert is None else invert - int(value)
        await self._send(self.entity_description.protocol_key, stored)
