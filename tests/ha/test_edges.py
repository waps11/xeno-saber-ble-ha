"""Entity and colour-sync edge cases: unknown values, failures, restore without a source."""

from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import mock_restore_cache

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ollec_parity import DATA, _sent, _setup

from custom_components.xeno_saber_ble import (
    device as dev,
    sync as sync_mod,
)
from custom_components.xeno_saber_ble.binary_sensor import XenoAuthorized
from custom_components.xeno_saber_ble.const import CONF_WLED_UDP
from custom_components.xeno_saber_ble.wled_udp import WledUdpHub


async def test_entities_handle_unknown_and_out_of_range_values(hass: HomeAssistant) -> None:
    _entry, holder = await _setup(hass)
    fw = holder["fw"]
    saber = _entry.runtime_data.saber

    # Brightness not reported: the light shows no brightness rather than a guess.
    fw._push(DATA, [3, {"PowerOn": True}])
    await hass.async_block_till_done()
    assert hass.states.get("light.xeno3_blade").attributes["brightness"] == 255
    saber.state.pop("Brightness")
    fw._push(DATA, [3, {"Power": 70}])
    await hass.async_block_till_done()
    blade = hass.states.get("light.xeno3_blade")
    assert blade.state == STATE_ON and blade.attributes.get("brightness") is None

    # Saber reports a font index beyond its total: the option list grows to include it.
    fw._push(DATA, [3, {"CurrentSoundPackageNo": 40}])
    await hass.async_block_till_done()
    font = hass.states.get("select.xeno3_sound_font")
    assert font.state == "41" and "41" in font.attributes["options"]

    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: "number.xeno3_volume", "value": 55}, blocking=True
    )
    assert fw.writes[-1][1] == b'[2,{"Volume":55}]'
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.xeno3_background_music"}, blocking=True
    )
    assert fw.writes[-1][1] == b'[2,{"BGMOn":false}]'
    holder["stop"]()


async def test_light_and_reconnect_failures_raise_translated_errors(hass: HomeAssistant) -> None:
    _entry, holder = await _setup(hass)

    async def broken(*args, **kwargs):
        raise dev.BleakError("GATT error 0x85")

    holder["fw"].write_gatt_char = broken
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "light", "turn_on", {ATTR_ENTITY_ID: "light.xeno3_blade"}, blocking=True
        )
    assert err.value.translation_key == "command_failed"

    async def unreachable(*args, **kwargs):
        raise dev.BleakError("Device not found")

    with (
        patch.object(dev, "establish_connection", unreachable),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: "button.xeno3_reconnect"}, blocking=True
        )
    assert err.value.translation_key == "connection_failed"
    holder["stop"]()


def test_authorized_sensor_is_unknown_while_disconnected() -> None:
    saber = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF", name="Xeno3", state={}, is_connected=False, authorized=True
    )
    sensor = XenoAuthorized(saber)
    assert sensor.is_on is None
    saber.is_connected = True
    assert sensor.is_on is True


async def test_sync_restore_with_busy_port_comes_back_off(
    hass: HomeAssistant, socket_enabled
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
        taken.bind(("127.0.0.1", 0))
        hass.data[sync_mod.WLED_HUB_KEY] = WledUdpHub(port=taken.getsockname()[1], host="127.0.0.1")
        mock_restore_cache(hass, [State("switch.xeno3_color_sync", STATE_ON)])
        _entry, holder = await _setup(hass, {CONF_WLED_UDP: True})
        assert hass.states.get("switch.xeno3_color_sync").state == STATE_OFF
    holder["stop"]()


async def test_color_sync_unit_behaviour(hass: HomeAssistant) -> None:
    # One shared hub per Home Assistant instance, created on first use.
    hub = sync_mod.wled_hub(hass)
    assert sync_mod.wled_hub(hass) is hub

    _entry, holder = await _setup(hass)
    saber = _entry.runtime_data.saber

    none = sync_mod.ColorSync(hass, saber, follow_entity=None, wled_udp=False, wled_source=None)
    with pytest.raises(sync_mod.SyncSourceError):
        await none.async_enable()

    hass.states.async_set("light.lamp", STATE_OFF)
    sync = sync_mod.ColorSync(
        hass, saber, follow_entity="light.lamp", wled_udp=False, wled_source=None
    )
    await sync.async_enable()
    await sync.async_enable()  # already on: no second subscription
    assert sync.enabled

    # A write error other than "unreachable" drops that update and keeps listening.
    async def flaky(*args, **kwargs):
        raise dev.XenoSaberError("Write failed: GATT 0x85")

    with patch.object(saber, "set_color", flaky), patch.object(sync_mod, "MIN_INTERVAL", 0):
        hass.states.async_set("light.lamp", STATE_ON, {"rgb_color": (1, 2, 3)})
        await hass.async_block_till_done()
    assert sync.enabled

    # Closing while a write round is pending cancels it cleanly.
    with patch.object(sync_mod, "MIN_INTERVAL", 10):
        hass.states.async_set("light.lamp", STATE_ON, {"rgb_color": (4, 5, 6)})
        await hass.async_block_till_done()
        await sync.async_close()
    assert not sync.enabled
    n = len(holder["fw"].writes)
    hass.states.async_set("light.lamp", STATE_ON, {"rgb_color": (7, 8, 9)})
    await hass.async_block_till_done()
    assert _sent(holder["fw"], n) == []
    holder["stop"]()
