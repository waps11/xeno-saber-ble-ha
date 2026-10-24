"""Xeno Saber BLE: unofficial Home Assistant integration for Xenopixel V3 compatible sabers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import protocol as p
from .const import (
    CONF_IDLE_DISCONNECT,
    CONF_KEEPALIVE,
    CONF_SYNC_LIGHT,
    CONF_WLED_SOURCE,
    CONF_WLED_UDP,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_KEEPALIVE,
    DOMAIN,
    ISSUE_KEEPALIVE,
    PLATFORMS,
    TESTED_FIRMWARE,
)
from .device import XenoSaber, XenoSaberError
from .services import async_setup_services
from .sync import ColorSync

_LOGGER = logging.getLogger(__name__)

# Background reconnects after a failure wait 10 s, then 20, 40... up to 5 min,
# so a saber that is awake but unreachable (phone app connected, edge of range)
# does not monopolise the Bluetooth adapter that other sabers share.
RECONNECT_BACKOFF_START = 10.0
RECONNECT_BACKOFF_MAX = 300.0

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class XenoRuntimeData:
    saber: XenoSaber
    sync: ColorSync | None = None
    title: str = ""  # as last seen, to tell a rename from an options change
    options: dict[str, Any] = field(default_factory=dict)


type XenoConfigEntry = ConfigEntry[XenoRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions: files, sound fonts, firmware, board configuration."""
    async_setup_services(hass)
    return True


def _keepalive_issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_KEEPALIVE}_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: XenoConfigEntry) -> bool:
    address = entry.unique_id
    assert address is not None  # set by the config flow
    # A sleeping saber does not advertise. That is its normal state, not a
    # setup failure: set up now, unavailable, and connect when it wakes up.
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), connectable=True)
    saber = XenoSaber(
        ble_device,
        idle_disconnect=entry.options.get(CONF_IDLE_DISCONNECT, DEFAULT_IDLE_DISCONNECT),
        name=entry.title,
        address=address.upper(),
    )
    sync = ColorSync(
        hass,
        saber,
        follow_entity=entry.options.get(CONF_SYNC_LIGHT) or None,
        wled_udp=bool(entry.options.get(CONF_WLED_UDP, False)),
        wled_source=entry.options.get(CONF_WLED_SOURCE) or None,
    )
    entry.runtime_data = XenoRuntimeData(
        saber, sync if sync.has_source else None, entry.title, dict(entry.options)
    )
    connecting = False
    backoff = 0.0  # seconds to wait after the last failed background attempt
    next_attempt = 0.0

    async def _async_connect() -> None:
        nonlocal connecting, backoff, next_attempt
        connecting = True
        try:
            await saber.connect()
        except XenoSaberError as err:
            backoff = min(backoff * 2 or RECONNECT_BACKOFF_START, RECONNECT_BACKOFF_MAX)
            next_attempt = hass.loop.time() + backoff
            _LOGGER.debug(
                "%s: background connect failed (%s); next try in %.0f s", address, err, backoff
            )
        else:
            backoff = next_attempt = 0.0
        finally:
            connecting = False

    @callback
    def _async_on_advertisement(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        saber.set_ble_device(service_info.device)
        # Persistent mode: reconnect when the saber wakes up and advertises.
        if (
            not saber.idle_disconnect
            and not saber.is_connected
            and not connecting
            and saber.transfer is None
            and hass.loop.time() >= next_attempt
        ):
            entry.async_create_background_task(
                hass, _async_connect(), f"xeno_saber_ble connect {address}"
            )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    _async_setup_keepalive(hass, entry, saber)
    update_device_info = _device_info_updater(hass, entry, saber)
    entry.async_on_unload(saber.register_callback(update_device_info))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    update_device_info()  # versions from the initial state dump; warns on untested firmware

    if not saber.idle_disconnect and ble_device is not None:
        # In the background: a busy saber (phone app connected) can take several
        # attempts, and entities already handle "unavailable until connected".
        # On failure it is retried when the saber advertises.
        entry.async_create_background_task(
            hass, _async_connect(), f"xeno_saber_ble connect {address}"
        )

    async def _async_stop(_event: Event) -> None:
        await saber.disconnect()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))
    _async_sync_names(hass, entry, saber)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


@callback
def _async_sync_names(hass: HomeAssistant, entry: XenoConfigEntry, saber: XenoSaber) -> None:
    """Keep one name per saber: the entry title follows the device's name.

    Home Assistant shows both on the integration page; a name given to the
    device becomes the entry's title, and renaming the entry renames the device.
    """
    registry = dr.async_get(hass)
    device = _find_device(registry, entry, saber.address)
    if device is None:
        return
    device_id = device.id
    if device.name_by_user and device.name_by_user != entry.title:
        _rename_entry(hass, entry, device.name_by_user)

    @callback
    def _is_ours(data: dr.EventDeviceRegistryUpdatedData) -> bool:
        return (
            data["action"] == "update"
            and data["device_id"] == device_id
            and "name_by_user" in data["changes"]
        )

    @callback
    def _device_renamed(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        current = registry.async_get(device_id)
        if current is not None and current.name_by_user and current.name_by_user != entry.title:
            _rename_entry(hass, entry, current.name_by_user)

    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED, _device_renamed, event_filter=_is_ours
        )
    )


@callback
def _rename_entry(hass: HomeAssistant, entry: XenoConfigEntry, title: str) -> None:
    entry.runtime_data.saber.name = title
    entry.runtime_data.title = title  # not an options change: no reload
    hass.config_entries.async_update_entry(entry, title=title)


def _async_setup_keepalive(hass: HomeAssistant, entry: XenoConfigEntry, saber: XenoSaber) -> None:
    """Keep the saber awake if asked to, and say loudly that it drains the battery."""
    interval = int(entry.options.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE) or 0)
    issue_id = _keepalive_issue_id(entry)
    if interval <= 0:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_KEEPALIVE,
        translation_placeholders={"name": entry.title, "interval": str(interval)},
    )
    _LOGGER.warning(
        "%s: keepalive every %s s is on; the saber will not sleep and its battery "
        "will drain, even with the blade off",
        entry.title,
        interval,
    )

    @callback
    def _tick(_now: datetime) -> None:
        entry.async_create_background_task(
            hass, saber.keepalive(), f"{DOMAIN} keepalive {saber.address}"
        )

    entry.async_on_unload(async_track_time_interval(hass, _tick, timedelta(seconds=interval)))


def _find_device(
    registry: dr.DeviceRegistry, entry: ConfigEntry, address: str
) -> dr.DeviceEntry | None:
    identifier = (DOMAIN, address)
    if hasattr(registry, "async_get_device_by_identifier"):  # HA 2026.10+
        return registry.async_get_device_by_identifier(identifier, entry.entry_id)
    return registry.async_get_device(identifiers={identifier})


def _device_info_updater(
    hass: HomeAssistant, entry: ConfigEntry, saber: XenoSaber
) -> Callable[[], None]:
    """Push hardware/firmware versions to the device once the saber reports them.

    Also warns once per firmware version that has not been tested.
    """
    warned: set[str] = set()

    @callback
    def _update() -> None:
        model = saber.state.get(p.KEY_HW_VERSION)
        sw_version = saber.state.get(p.KEY_SW_VERSION)
        if not model and not sw_version:
            return
        if sw_version and sw_version not in TESTED_FIRMWARE and sw_version not in warned:
            warned.add(sw_version)
            _LOGGER.warning(
                "%s reports firmware %s, which Xeno Saber BLE has not been tested "
                "with (tested: %s). The saber's protocol is undocumented and can "
                "change with any firmware update; if something stops working, "
                "please report it with the diagnostics file",
                entry.title,
                sw_version,
                ", ".join(sorted(TESTED_FIRMWARE)),
            )
        registry = dr.async_get(hass)
        device = _find_device(registry, entry, saber.address)
        if device is None:
            return
        changes = {}
        if model and device.model != model:
            changes["model"] = model
        if sw_version and device.sw_version != sw_version:
            changes["sw_version"] = sw_version
        if changes:
            registry.async_update_device(device.id, **changes)

    return _update


async def _async_update_listener(hass: HomeAssistant, entry: XenoConfigEntry) -> None:
    data = entry.runtime_data
    if entry.title != data.title:
        # The entry was renamed: give the device the same name, without a reload.
        data.title = entry.title
        data.saber.name = entry.title
        registry = dr.async_get(hass)
        device = _find_device(registry, entry, data.saber.address)
        if device is not None:
            registry.async_update_device(
                device.id,
                name=entry.title,
                name_by_user=entry.title if device.name_by_user else None,
            )
    if dict(entry.options) != data.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: XenoConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        ir.async_delete_issue(hass, DOMAIN, _keepalive_issue_id(entry))
        if entry.runtime_data.sync is not None:
            await entry.runtime_data.sync.async_close()
        await entry.runtime_data.saber.disconnect()
    return unloaded
