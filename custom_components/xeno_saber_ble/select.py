"""Sound font, blade effect and per-event effect selectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XenoConfigEntry, protocol as p
from .device import XenoSaber
from .entity import XenoEntity

# Used until the saber reports its totals.
_FALLBACK_TOTAL = 1


@dataclass(frozen=True, kw_only=True)
class XenoSelectDescription(SelectEntityDescription):
    current_key: str
    total_key: str


SELECTS = (
    XenoSelectDescription(
        key="sound_font",
        translation_key="sound_font",
        current_key=p.KEY_FONT,
        total_key=p.KEY_FONT_TOTAL,
    ),
    XenoSelectDescription(
        key="light_effect",
        translation_key="light_effect",
        current_key=p.KEY_EFFECT,
        total_key=p.KEY_EFFECT_TOTAL,
    ),
    # Light effect played on each event, chosen per font in the app.
    *(
        XenoSelectDescription(
            key=f"{event}_effect",
            translation_key=f"{event}_effect",
            current_key=f"Current{name}",
            total_key=f"Total{name}",
            entity_category=EntityCategory.CONFIG,
        )
        for event, name in (
            ("blaster", "Blaster"),
            ("force", "Force"),
            ("lockup", "Lockup"),
            ("clash", "Clash"),
            ("post_off", "PostOff"),
        )
    ),
    XenoSelectDescription(
        key="mode",
        translation_key="mode",
        current_key="CurrentMode",
        total_key="TotalMode",
        entity_category=EntityCategory.CONFIG,
    ),
)


# State is pushed by the saber; commands are serialized by the device's own lock.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: XenoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(XenoSelect(entry.runtime_data.saber, d) for d in SELECTS)


class XenoSelect(XenoEntity, SelectEntity):
    """Options are 1-based numbers; the protocol index is 0-based."""

    entity_description: XenoSelectDescription

    def __init__(self, saber: XenoSaber, description: XenoSelectDescription) -> None:
        super().__init__(saber, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        # A total of 0 means this font has no such effect to choose from.
        return self._state(self.entity_description.total_key) != 0 and super().available

    @property
    @override
    def options(self) -> list[str]:
        total = self._state(self.entity_description.total_key)
        if not isinstance(total, int) or total < 1:
            total = _FALLBACK_TOTAL
        current = self._state(self.entity_description.current_key)
        if isinstance(current, int) and current >= total:
            total = current + 1
        return [str(i) for i in range(1, total + 1)]

    @property
    @override
    def current_option(self) -> str | None:
        current = self._state(self.entity_description.current_key)
        return str(current + 1) if isinstance(current, int) else None

    @override
    async def async_select_option(self, option: str) -> None:
        await self._send(self.entity_description.current_key, int(option) - 1)
