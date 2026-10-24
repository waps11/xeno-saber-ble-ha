"""Full Home Assistant setup against a simulated saber."""

from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_device import FakeSaber  # noqa: E402

from custom_components.xeno_saber_ble import device as dev  # noqa: E402
from custom_components.xeno_saber_ble.const import DOMAIN  # noqa: E402

ADDR = "AA:BB:CC:DD:EE:FF"


async def _setup(hass: HomeAssistant):
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})
    holder = {}

    async def fake_establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        holder["fw"] = FakeSaber()
        return holder["fw"]

    ble_device = MagicMock(address=ADDR)
    ble_device.name = "Xeno3"
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDR, title="Xeno3")
    entry.add_to_hass(hass)
    with (
        patch.object(dev, "establish_connection", fake_establish),
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
    return entry, holder


async def test_entities_and_commands(hass: HomeAssistant) -> None:
    entry, holder = await _setup(hass)
    fw = holder["fw"]

    blade = hass.states.get("light.xeno3_blade")
    assert blade is not None and blade.state == STATE_OFF
    assert hass.states.get("sensor.xeno3_battery").state == "82"
    assert hass.states.get("sensor.xeno3_font_name").state == "Vader"
    font = hass.states.get("select.xeno3_sound_font")
    assert font.state == "3" and len(font.attributes["options"]) == 34
    assert hass.states.get("number.xeno3_volume").state in ("40", "40.0")

    await hass.services.async_call(
        "light", "turn_on",
        {ATTR_ENTITY_ID: "light.xeno3_blade", "rgb_color": [255, 0, 0], "brightness": 128},
        blocking=True,
    )
    await hass.async_block_till_done()
    sent = [w[1] for w in fw.writes[2:]]
    assert sent == [
        b'[2,{"BackgroundColor":[255,0,0]}]',
        b'[2,{"Brightness":50}]',
        b'[2,{"PowerOn":true}]',
    ]
    blade = hass.states.get("light.xeno3_blade")
    assert blade.state == STATE_ON and tuple(blade.attributes["rgb_color"]) == (255, 0, 0)

    await hass.services.async_call(
        "select", "select_option",
        {ATTR_ENTITY_ID: "select.xeno3_sound_font", "option": "10"}, blocking=True,
    )
    assert fw.writes[-1][1] == b'[2,{"CurrentSoundPackageNo":9}]'

    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.xeno3_clash"}, blocking=True
    )
    assert fw.writes[-1][1] == b'[2,{"Clash":true}]'

    await hass.services.async_call(
        "light", "turn_off", {ATTR_ENTITY_ID: "light.xeno3_blade"}, blocking=True
    )
    await hass.async_block_till_done()
    assert fw.writes[-1][1] == b'[2,{"PowerOn":false}]'
    assert hass.states.get("light.xeno3_blade").state == STATE_OFF

    # Saber pushes a change on its own (button pressed on the hilt).
    fw._push("00003ab1-0000-1000-8000-00805f9b34fb", [3, {"PowerOn": True, "Power": 61}])
    await hass.async_block_till_done()
    assert hass.states.get("light.xeno3_blade").state == STATE_ON
    assert hass.states.get("sensor.xeno3_battery").state == "61"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not fw.is_connected


async def test_entity_names_follow_ha_language(hass: HomeAssistant) -> None:
    """With HA set to German, entity names (and new entity IDs) are German."""
    await hass.config.async_update(language="de")
    await _setup(hass)
    blade = hass.states.get("light.xeno3_klinge")
    assert blade is not None, sorted(hass.states.async_entity_ids("light"))
    assert blade.attributes["friendly_name"] == "Xeno3 Klinge"
    assert hass.states.get("select.xeno3_soundfont") is not None
    assert hass.states.get("sensor.xeno3_akku") is not None


async def test_command_failure_raises_translated_error(hass: HomeAssistant) -> None:
    from bleak.exc import BleakError
    import pytest

    from homeassistant.exceptions import HomeAssistantError

    _entry, holder = await _setup(hass)
    fw = holder["fw"]

    async def broken_write(*args, **kwargs):
        raise BleakError("GATT error 0x85")

    fw.write_gatt_char = broken_write
    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            "switch", "turn_on",
            {ATTR_ENTITY_ID: "switch.xeno3_background_music"}, blocking=True,
        )
    err = exc_info.value
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "command_failed"
    assert "GATT error 0x85" in err.translation_placeholders["error"]
    assert str(err).startswith("Command to the saber failed:")
