"""Entry setup and lifecycle: not found, retries, reconnect on advertisement, stop, diagnostics."""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_device import FakeSaber
from test_ollec_parity import ADDR, _setup

import custom_components.xeno_saber_ble as integration
from custom_components.xeno_saber_ble import device as dev
from custom_components.xeno_saber_ble.const import (
    CONF_IDLE_DISCONNECT,
    CONF_SYNC_LIGHT,
    CONF_WLED_SOURCE,
    CONF_WLED_UDP,
    DOMAIN,
)
from custom_components.xeno_saber_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDR, title="Xeno3", options=options or {})
    entry.add_to_hass(hass)
    return entry


async def test_sleeping_saber_is_set_up_and_connects_when_it_wakes(hass: HomeAssistant) -> None:
    """Asleep is a saber's normal state: no setup error, entities unavailable."""
    captured = {}

    def register(hass, callback, matcher, mode):
        captured["cb"] = callback
        return lambda: None

    async def establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        fw = FakeSaber()
        fw.on_disconnect = disconnected_callback
        return fw

    entry = _entry(hass)
    with (
        patch.object(dev, "establish_connection", establish),
        patch.object(dev, "STATE_SETTLE", 0),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_ble_device_from_address",
            return_value=None,
        ),
        patch("custom_components.xeno_saber_ble.bluetooth.async_register_callback", register),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        saber = entry.runtime_data.saber
        assert saber.address == ADDR and not saber.is_connected
        assert hass.states.get("light.xeno3_blade").state == STATE_UNAVAILABLE
        with pytest.raises(dev.XenoSaberUnreachable, match="wake it up"):
            await saber.connect()

        ble_device = MagicMock(address=ADDR)
        ble_device.name = "Saber"
        captured["cb"](SimpleNamespace(device=ble_device), None)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saber.is_connected
        assert hass.states.get("light.xeno3_blade").state != STATE_UNAVAILABLE


async def test_connection_failure_at_setup_is_retried_quietly(hass: HomeAssistant) -> None:
    entry = _entry(hass)

    async def unreachable(*args, **kwargs):
        raise dev.BleakError("Device not found")

    ble_device = MagicMock(address=ADDR)
    ble_device.name = "Xeno3"
    with (
        patch.object(dev, "establish_connection", unreachable),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data.saber.is_connected


async def test_entry_and_device_share_one_name(hass: HomeAssistant) -> None:
    from homeassistant.helpers import device_registry as dr

    entry, holder = await _setup(hass)
    registry = dr.async_get(hass)
    (device,) = dr.async_entries_for_config_entry(registry, entry.entry_id)
    saber = entry.runtime_data.saber
    reloads = []
    with patch.object(hass.config_entries, "async_reload", side_effect=reloads.append):
        # Renaming the device renames the entry...
        registry.async_update_device(device.id, name_by_user="Black Saber")
        await hass.async_block_till_done()
        assert entry.title == "Black Saber" and saber.name == "Black Saber"
        # ...clearing the device's name leaves the entry alone...
        registry.async_update_device(device.id, name_by_user=None)
        await hass.async_block_till_done()
        assert entry.title == "Black Saber"
        # ...and renaming the entry renames the device.
        hass.config_entries.async_update_entry(entry, title="Red Saber")
        await hass.async_block_till_done()
        device = registry.async_get(device.id)
        assert device.name == "Red Saber" and device.name_by_user is None
        registry.async_update_device(device.id, name_by_user="Old")
        hass.config_entries.async_update_entry(entry, title="Blue Saber")
        await hass.async_block_till_done()
        assert registry.async_get(device.id).name_by_user == "Blue Saber"
        assert reloads == []  # names are not options: nothing reloads
        hass.config_entries.async_update_entry(entry, options={CONF_IDLE_DISCONNECT: 30})
        await hass.async_block_till_done()
        assert reloads == [entry.entry_id]
    holder["stop"]()


async def test_device_name_given_earlier_becomes_the_title(hass: HomeAssistant) -> None:
    from homeassistant.helpers import device_registry as dr

    entry = _entry(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, ADDR)}, name="Xeno3"
    )
    registry.async_update_device(device.id, name_by_user="Black Saber")
    with (
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_ble_device_from_address",
            return_value=None,
        ),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.title == "Black Saber"


async def test_on_demand_mode_does_not_connect_at_setup(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.xeno_saber_ble.entity.bluetooth.async_address_present",
        return_value=False,
    ):
        entry, holder = await _setup(hass, {CONF_IDLE_DISCONNECT: 60})
        assert entry.state is ConfigEntryState.LOADED
        assert holder["all"] == []  # nothing until a command is sent
        assert hass.states.get("light.xeno3_blade").state == STATE_UNAVAILABLE
    holder["stop"]()


async def test_advertisement_triggers_background_reconnect(hass: HomeAssistant) -> None:
    captured = {}

    def register(hass, callback, matcher, mode):
        captured["cb"] = callback
        return lambda: None

    entry = _entry(hass)
    sabers = []

    async def establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        fw = FakeSaber()
        fw.on_disconnect = disconnected_callback
        sabers.append(fw)
        return fw

    ble_device = MagicMock(address=ADDR)
    ble_device.name = "Xeno3"
    with (
        patch.object(dev, "establish_connection", establish),
        patch.object(dev, "STATE_SETTLE", 0),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("custom_components.xeno_saber_ble.bluetooth.async_register_callback", register),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        saber = entry.runtime_data.saber

        # Connected: an advertisement only refreshes the BLE path.
        new_path = MagicMock(address=ADDR)
        new_path.name = "Xeno3"
        captured["cb"](SimpleNamespace(device=new_path), None)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(sabers) == 1 and saber._ble_device is new_path

        # Saber falls asleep, then wakes up and advertises: HA reconnects by itself.
        sabers[0].is_connected = False
        sabers[0].on_disconnect(sabers[0])
        captured["cb"](SimpleNamespace(device=new_path), None)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(sabers) == 2 and saber.is_connected

        # Still asleep: a failed background attempt is quiet and retried next time.
        sabers[1].is_connected = False
        sabers[1].on_disconnect(sabers[1])

        async def unreachable(*a, **k):
            raise dev.BleakError("gone")

        with (
            patch.object(dev, "establish_connection", unreachable),
            patch.object(integration, "RECONNECT_BACKOFF_START", 0.05),
        ):
            captured["cb"](SimpleNamespace(device=new_path), None)
            await hass.async_block_till_done(wait_background_tasks=True)
        assert not saber.is_connected

        # Right after a failure, advertisements are ignored (backoff), so an
        # unreachable saber does not monopolise an adapter other sabers share.
        captured["cb"](SimpleNamespace(device=new_path), None)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert not saber.is_connected and len(sabers) == 2
        await asyncio.sleep(0.06)
        captured["cb"](SimpleNamespace(device=new_path), None)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saber.is_connected and len(sabers) == 3

        # Home Assistant stopping releases the saber.
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert not saber.is_connected


async def test_diagnostics_redact_identifiers(hass: HomeAssistant) -> None:
    entry, holder = await _setup(
        hass,
        {
            CONF_SYNC_LIGHT: "light.living_room",
            CONF_WLED_UDP: True,
            CONF_WLED_SOURCE: "192.168.1.50",
        },
    )
    info = SimpleNamespace(
        name="Saber",
        rssi=-78,
        source="esp32-proxy",
        connectable=True,
        service_uuids=["00003ab0-0000-1000-8000-00805f9b34fb"],
    )
    with patch(
        "custom_components.xeno_saber_ble.diagnostics.bluetooth.async_last_service_info",
        return_value=info,
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["unique_id"] == "**REDACTED**"
    assert diag["entry"]["options"][CONF_WLED_SOURCE] == "**REDACTED**"
    assert diag["entry"]["options"][CONF_SYNC_LIGHT] == "light.living_room"
    assert diag["connection"] == {"connected": True, "authorized": True, "idle_disconnect": 0}
    assert diag["bluetooth"]["rssi"] == -78 and diag["bluetooth"]["source"] == "**REDACTED**"
    assert "esp32-proxy" not in str(diag)
    assert diag["color_sync"] == {
        "enabled": False,
        "follows_light": True,
        "wled_udp": True,
        "wled_source_filter": True,
    }
    assert diag["saber_state"]["SoftwareVersion"] == "1.0"
    assert ADDR not in str(diag)
    holder["stop"]()


async def test_diagnostics_without_sync_or_advertisement(hass: HomeAssistant) -> None:
    entry, holder = await _setup(hass)
    with patch(
        "custom_components.xeno_saber_ble.diagnostics.bluetooth.async_last_service_info",
        return_value=None,
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["bluetooth"] is None and diag["color_sync"] is None
    holder["stop"]()


def test_device_lookup_falls_back_on_older_home_assistant() -> None:
    class OldRegistry:  # before async_get_device_by_identifier existed
        def async_get_device(self, identifiers):
            return ("found", identifiers)

    entry = SimpleNamespace(entry_id="abc")
    assert integration._find_device(OldRegistry(), entry, ADDR) == (
        "found",
        {(DOMAIN, ADDR)},
    )


async def test_device_info_updater_waits_for_versions_and_device(hass: HomeAssistant) -> None:
    saber = SimpleNamespace(state={}, address=ADDR)
    entry = SimpleNamespace(entry_id="missing", title="Xeno3")
    update = integration._device_info_updater(hass, entry, saber)
    update()  # no versions yet: nothing to do
    saber.state["SoftwareVersion"] = "2.0"
    update()  # versions, but no device registered for this entry: nothing to do


async def test_untested_firmware_is_warned_once_per_version(hass: HomeAssistant, caplog) -> None:
    import logging

    caplog.set_level(logging.WARNING)

    def warnings() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if "Xeno Saber BLE has not been tested with" in r.getMessage()
        ]

    entry, holder = await _setup(hass)  # the simulated saber reports firmware "1.0"
    assert len(warnings()) == 1 and "firmware 1.0" in warnings()[0]
    assert "DMN_XENO_B_SV1.4.0" in warnings()[0]  # names the tested version

    fw = holder["fw"]
    fw._push("00003ab1-0000-1000-8000-00805f9b34fb", [3, {"Power": 50}])
    await hass.async_block_till_done()
    assert len(warnings()) == 1  # same version: no repeat

    fw._push("00003ab1-0000-1000-8000-00805f9b34fb", [3, {"SoftwareVersion": "DMN_XENO_B_SV1.4.0"}])
    await hass.async_block_till_done()
    assert len(warnings()) == 1  # tested version: silent

    fw._push("00003ab1-0000-1000-8000-00805f9b34fb", [3, {"SoftwareVersion": "DMN_XENO_B_SV1.5.0"}])
    await hass.async_block_till_done()
    assert len(warnings()) == 2 and "DMN_XENO_B_SV1.5.0" in warnings()[1]

    with patch(
        "custom_components.xeno_saber_ble.diagnostics.bluetooth.async_last_service_info",
        return_value=None,
    ):
        diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["firmware"] == {"reported": "DMN_XENO_B_SV1.5.0", "tested": ["DMN_XENO_B_SV1.4.0"]}
    holder["stop"]()


async def test_setup_does_not_wait_for_the_connection(hass: HomeAssistant) -> None:
    """A busy saber can take several attempts: setup returns at once."""
    release = asyncio.Event()

    async def slow(cls, device, name, disconnected_callback=None, max_attempts=3):
        await release.wait()
        fw = FakeSaber()
        fw.on_disconnect = disconnected_callback
        return fw

    entry = _entry(hass)
    ble_device = MagicMock(address=ADDR)
    ble_device.name = "Saber"
    with (
        patch.object(dev, "establish_connection", slow),
        patch.object(dev, "STATE_SETTLE", 0),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert not entry.runtime_data.saber.is_connected
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data.saber.is_connected
