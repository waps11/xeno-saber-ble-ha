"""Config flow for Xeno Saber BLE."""

from __future__ import annotations

import ipaddress
from typing import Any, override

try:  # Home Assistant 2026.x replaced voluptuous with probatio
    import probatio as vol
except ImportError:  # older Home Assistant
    import voluptuous as vol  # type: ignore[no-redef]

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_IDLE_DISCONNECT,
    CONF_KEEPALIVE,
    CONF_SYNC_LIGHT,
    CONF_WLED_SOURCE,
    CONF_WLED_UDP,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_KEEPALIVE,
    DOMAIN,
)
from .device import XenoSaber, XenoSaberError
from .protocol import DATA_SERVICE_UUID, HANDSHAKE_SERVICE_UUID

_SERVICE_UUIDS = {DATA_SERVICE_UUID, HANDSHAKE_SERVICE_UUID}
# Manufacturer data seen on real boards: ID 0x6144 ("Da") + b"mien" = "Damien".
_MANUFACTURER_ID = 0x6144


def _is_saber(info: BluetoothServiceInfoBleak) -> bool:
    if _SERVICE_UUIDS & {u.lower() for u in info.service_uuids}:
        return True
    return _MANUFACTURER_ID in (getattr(info, "manufacturer_data", None) or {})


# Every board advertises the same name, so add the end of the address to it.
_GENERIC_NAMES = {"saber", "xeno saber"}  # "Saber" is what real boards advertise


def entry_title(name: str | None, address: str) -> str:
    """A title that tells sabers apart: boards all advertise "Saber"."""
    if not name:
        return f"Xeno saber {address[-5:]}"
    if name.strip().lower() in _GENERIC_NAMES:
        return f"{name.strip()} {address[-5:]}"
    return name


def _title(info: BluetoothServiceInfoBleak) -> str:
    return entry_title(info.name, info.address)


async def _async_test_connection(info: BluetoothServiceInfoBleak) -> str | None:
    """Connect once (handshake + authorize) to make sure the saber answers.

    Returns an error key for the form, or None on success.
    """
    saber = XenoSaber(info.device)
    try:
        await saber.connect()
    except XenoSaberError:
        return "cannot_connect"
    finally:
        await saber.disconnect()
    return None


class XenoSaberConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    @override
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if (error := await _async_test_connection(self._discovery)) is None:
                return self.async_create_entry(title=_title(self._discovery), data={})
            errors["base"] = error
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _title(self._discovery)},
            errors=errors,
        )

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            info = self._discovered[address]
            if (error := await _async_test_connection(info)) is None:
                return self.async_create_entry(title=_title(info), data={})
            errors["base"] = error

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address not in current and _is_saber(info):
                self._discovered[info.address] = info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            addr: f"{_title(info)} ({addr})"
                            for addr, info in self._discovered.items()
                        }
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return XenoSaberOptionsFlow()


class XenoSaberOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate(user_input)
            if not errors:
                data = {k: v for k, v in user_input.items() if v not in (None, "")}
                return self.async_create_entry(data=data)

        values = user_input if user_input is not None else dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(values),
            errors=errors,
        )

    def _validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if user_input.get(CONF_KEEPALIVE, 0) and user_input.get(CONF_IDLE_DISCONNECT, 0):
            # Keepalive only makes sense with a permanent connection.
            errors[CONF_KEEPALIVE] = "keepalive_needs_persistent"
        if light := user_input.get(CONF_SYNC_LIGHT):
            own_blade = er.async_get(self.hass).async_get_entity_id(
                "light", DOMAIN, f"{(self.config_entry.unique_id or '').upper()}_blade"
            )
            if light == own_blade:
                errors[CONF_SYNC_LIGHT] = "sync_self"
        if source := (user_input.get(CONF_WLED_SOURCE) or "").strip():
            try:
                ipaddress.IPv4Address(source)
            except ValueError:
                errors[CONF_WLED_SOURCE] = "invalid_ip"
            else:
                user_input[CONF_WLED_SOURCE] = source
        return errors

    @staticmethod
    def _schema(values: dict[str, Any]) -> vol.Schema:
        def suggested(key: str) -> dict[str, Any]:
            value = values.get(key)
            return {"suggested_value": value} if value not in (None, "") else {}

        return vol.Schema(
            {
                vol.Required(
                    CONF_IDLE_DISCONNECT,
                    default=values.get(CONF_IDLE_DISCONNECT, DEFAULT_IDLE_DISCONNECT),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Required(
                    CONF_KEEPALIVE, default=values.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Optional(
                    CONF_SYNC_LIGHT, description=suggested(CONF_SYNC_LIGHT)
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="light")),
                vol.Required(
                    CONF_WLED_UDP, default=bool(values.get(CONF_WLED_UDP, False))
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_WLED_SOURCE, description=suggested(CONF_WLED_SOURCE)
                ): selector.TextSelector(),
            }
        )
