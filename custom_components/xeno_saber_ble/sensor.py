"""Battery, current font name, board configuration and file transfer progress."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XenoConfigEntry, protocol as p
from .device import XenoSaber
from .entity import XenoEntity

# State is pushed by the saber; commands are serialized by the device's own lock.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: XenoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    saber = entry.runtime_data.saber
    async_add_entities(
        [
            XenoBattery(saber),
            XenoFontName(saber),
            XenoBladeType(saber, "blade_firmware", p.KEY_SOFTWARE_TYPE),
            XenoBladeType(saber, "hardware_type", p.KEY_HARDWARE_TYPE),
            XenoButtonCount(saber),
            XenoFileTransfer(saber),
        ]
    )


class XenoBattery(XenoEntity, SensorEntity):
    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "battery")

    @property
    @override
    def native_value(self) -> int | None:
        value = self._saber.battery
        return None if value is None else max(0, min(100, value))


class XenoFontName(XenoEntity, SensorEntity):
    _attr_translation_key = "font_name"

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "font_name")

    @property
    @override
    def native_value(self) -> str | None:
        value = self._state(p.KEY_FONT_NAME)
        return str(value) if value else None


class XenoBladeType(XenoEntity, SensorEntity):
    """Pixel or RGB: blade firmware (SoftwareType) or board wiring (HardwareType)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: list[str] = [t.lower() for t in p.BLADE_TYPES]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, saber: XenoSaber, key: str, protocol_key: str) -> None:
        super().__init__(saber, key)
        self._attr_translation_key = key
        self._protocol_key = protocol_key

    @property
    @override
    def native_value(self) -> str | None:
        value = self._state(self._protocol_key)
        if isinstance(value, str) and value.lower() in self._attr_options:
            return value.lower()
        return None


class XenoButtonCount(XenoEntity, SensorEntity):
    _attr_translation_key = "button_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "button_count")

    @property
    @override
    def native_value(self) -> int | None:
        value = self._state(p.KEY_BUTTON_NUM)
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class XenoFileTransfer(XenoEntity, SensorEntity):
    """Progress of a sound font, file or firmware transfer (0 when idle)."""

    _attr_translation_key = "file_transfer"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, saber: XenoSaber) -> None:
        super().__init__(saber, "file_transfer")

    @property
    @override
    def available(self) -> bool:
        return True  # "idle" is meaningful even while the saber sleeps

    @property
    @override
    def native_value(self) -> int:
        return self._saber.progress or 0

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        status, batch = self._saber.transfer, self._saber.batch
        attrs: dict[str, Any] = {"active": status is not None or batch is not None}
        if batch is not None:
            attrs["files_done"] = batch.done
            attrs["files_total"] = batch.total
        if status is not None:
            attrs.update(
                direction="to_saber" if status.direction == "download" else "from_saber",
                path=status.path,
                bytes_done=status.done,
                bytes_total=status.total,
            )
        return attrs
