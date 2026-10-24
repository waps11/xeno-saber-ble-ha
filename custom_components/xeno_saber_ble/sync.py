"""Colour sync: the blade follows a Home Assistant light and/or WLED UDP broadcasts.

Ported from the WLED sync of Ollec's ha-xenopixel ESPHome proxy, with an extra
source: any light entity in Home Assistant (a WLED, Hue, another saber...).

Updates are coalesced: only the latest target is kept, and writes are spaced
by ``MIN_INTERVAL`` because each BLE write takes roughly 100 ms. Values the
saber already reports are not sent again.

A sleeping saber is never woken up for sync: with a permanent connection,
updates are held until the saber reconnects (then the latest one is applied);
in on-demand mode, an unreachable saber is left alone for
``UNREACHABLE_BACKOFF`` seconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.hass_dict import HassKey

from . import protocol as p
from .const import DOMAIN
from .device import XenoSaber, XenoSaberError, XenoSaberUnreachable
from .wled_udp import WledState, WledUdpHub

_LOGGER = logging.getLogger(__name__)

MIN_INTERVAL = 0.1  # seconds between two rounds of writes
# On-demand mode: after the saber could not be reached, stop trying for a while
# instead of opening a connection attempt for every WLED packet.
UNREACHABLE_BACKOFF = 30.0
WLED_HUB_KEY: HassKey[WledUdpHub] = HassKey(f"{DOMAIN}_wled_udp_hub")


@dataclass(frozen=True)
class SyncTarget:
    on: bool
    brightness: int | None = None  # saber scale, 1-100
    rgb: tuple[int, int, int] | None = None


def _percent(value_255: int | float) -> int:
    return max(1, min(100, round(value_255 * 100 / 255)))


def target_from_light(state: State | None) -> SyncTarget | None:
    """Map a light's HA state to what the blade should do; None = ignore."""
    if state is None or state.state not in (STATE_ON, STATE_OFF):
        return None  # unavailable / unknown: leave the blade alone
    if state.state == STATE_OFF:
        return SyncTarget(on=False)
    rgb = state.attributes.get("rgb_color")
    bri = state.attributes.get("brightness")
    return SyncTarget(
        on=True,
        brightness=_percent(bri) if isinstance(bri, (int, float)) else None,
        rgb=(int(rgb[0]), int(rgb[1]), int(rgb[2])) if rgb else None,
    )


def target_from_wled(state: WledState) -> SyncTarget:
    if not state.on:
        return SyncTarget(on=False)
    return SyncTarget(on=True, brightness=_percent(state.brightness), rgb=state.rgb)


def wled_hub(hass: HomeAssistant) -> WledUdpHub:
    """The UDP socket shared by every saber."""
    if (hub := hass.data.get(WLED_HUB_KEY)) is None:
        hub = hass.data[WLED_HUB_KEY] = WledUdpHub()
    return hub


class SyncSourceError(Exception):
    """No sync source could be started."""


class ColorSync:
    """Drives one saber's blade from the configured sources while enabled."""

    def __init__(
        self,
        hass: HomeAssistant,
        saber: XenoSaber,
        *,
        follow_entity: str | None,
        wled_udp: bool,
        wled_source: str | None,
    ) -> None:
        self._hass = hass
        self._saber = saber
        self.follow_entity = follow_entity
        self.wled_udp = wled_udp
        self.wled_source = wled_source
        self.enabled = False
        self._unsubs: list[Callable[[], None]] = []
        self._pending: SyncTarget | None = None
        self._last: SyncTarget | None = None
        self._task: asyncio.Task[None] | None = None
        self._was_connected = saber.is_connected
        self._retry_at = 0.0

    @property
    def has_source(self) -> bool:
        return bool(self.follow_entity or self.wled_udp)

    async def async_enable(self) -> None:
        """Start following the sources. Raises SyncSourceError if none could start."""
        if self.enabled:
            return
        started = False
        if self.follow_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass, [self.follow_entity], self._on_light_change
                )
            )
            started = True
        if self.wled_udp:
            try:
                self._unsubs.append(
                    await wled_hub(self._hass).add_listener(
                        self._on_wled, self.wled_source
                    )
                )
                started = True
            except OSError as err:
                _LOGGER.warning(
                    "%s: cannot listen for WLED sync on UDP port %s: %s",
                    self._saber.address, wled_hub(self._hass).port, err,
                )
                if not started:
                    raise SyncSourceError(str(err)) from err
        if not started:
            raise SyncSourceError("no sync source configured")
        self._was_connected = self._saber.is_connected
        self._unsubs.append(self._saber.register_callback(self._on_saber_update))
        self.enabled = True
        # Match the followed light right away rather than on its next change.
        if self.follow_entity:
            self._submit(target_from_light(self._hass.states.get(self.follow_entity)))

    async def async_disable(self) -> None:
        self.enabled = False
        while self._unsubs:
            self._unsubs.pop()()
        self._pending = None
        self._last = None

    async def async_close(self) -> None:
        await self.async_disable()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------- sources
    @callback
    def _on_light_change(self, event: Event[EventStateChangedData]) -> None:
        self._submit(target_from_light(event.data["new_state"]))

    @callback
    def _on_wled(self, state: WledState) -> None:
        self._submit(target_from_wled(state))

    @callback
    def _on_saber_update(self) -> None:
        """Catch up with the source as soon as the saber (re)connects."""
        connected = self._saber.is_connected
        if connected and not self._was_connected and self._last is not None:
            self._retry_at = 0.0
            self._submit(self._last)
        self._was_connected = connected

    # --------------------------------------------------------------- writer
    @callback
    def _submit(self, target: SyncTarget | None) -> None:
        if target is None or not self.enabled:
            return
        self._last = target
        if not self._saber.is_connected:
            if not self._saber.idle_disconnect:
                return  # permanent mode: applied when the saber reconnects
            if self._hass.loop.time() < self._retry_at:
                return  # on-demand mode, saber recently unreachable
        self._pending = target
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_background_task(
                self._drain(), f"{DOMAIN} colour sync {self._saber.address}"
            )

    async def _drain(self) -> None:
        while self._pending is not None:
            target, self._pending = self._pending, None
            try:
                await self.apply(target)
            except XenoSaberUnreachable as err:
                self._retry_at = self._hass.loop.time() + UNREACHABLE_BACKOFF
                self._pending = None
                _LOGGER.debug("%s: colour sync paused, saber unreachable: %s", self._saber.address, err)
                return
            except XenoSaberError as err:
                # Saber asleep or out of range: drop this update, keep listening.
                _LOGGER.debug("%s: colour sync write failed: %s", self._saber.address, err)
            await asyncio.sleep(MIN_INTERVAL)

    async def apply(self, target: SyncTarget) -> None:
        """Send only what differs from what the saber last reported."""
        saber = self._saber
        if not target.on:
            if saber.blade_on is not False:
                await saber.set_blade(False)
            return
        if target.rgb is not None and saber.color != target.rgb:
            await saber.set_color(*target.rgb)
        if (
            target.brightness is not None
            and saber.state.get(p.KEY_BRIGHTNESS) != target.brightness
        ):
            await saber.send(p.KEY_BRIGHTNESS, target.brightness)
        if saber.blade_on is not True:
            await saber.set_blade(True)
