"""Settings entities added for parity with the official app."""

from pathlib import Path
import sys
from types import SimpleNamespace

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ollec_parity import DATA, _sent, _setup  # noqa: E402

from custom_components.xeno_saber_ble.device import TransferStatus  # noqa: E402
from custom_components.xeno_saber_ble.number import NUMBERS, XenoNumber  # noqa: E402
from custom_components.xeno_saber_ble.sensor import XenoFileTransfer  # noqa: E402

# What a real saber reports (firmware DMN_XENO_B_SV1.4.0), beyond FakeSaber's basics.
REPORTED = {
    "AwakenTime": 1000, "SleepTime": 10000, "IdleSleepTime": 5, "OnTime": 200,
    "OffTime": 1000, "ClashSensitivity": 13, "SwingSensitivity": 400,
    "TwistSensitivity": 220, "PushSensitivity": 18, "PullSensitivity": 13,
    "SwingSoundSensitivity": 4, "ClashOn": True, "Countdown": False, "TorchMode": False,
    "PullPushSwitchOn": True, "HardwareType": "RGB", "SoftwareType": "Pixel", "ButtonNum": 1,
    "CurrentBlaster": 0, "TotalBlaster": 1, "CurrentPostOff": 0, "TotalPostOff": 0,
    "CurrentMode": 0, "TotalMode": 1, "MainBlade": False, "SideBlade": False,
}


async def _reported(hass: HomeAssistant):
    entry, holder = await _setup(hass)
    holder["fw"]._push(DATA, [3, REPORTED])
    await hass.async_block_till_done()
    return entry, holder


async def _set(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


async def test_numbers_use_app_ranges_and_inversion(hass: HomeAssistant) -> None:
    _entry, holder = await _reported(hass)
    fw = holder["fw"]
    state = hass.states.get("number.xeno3_power_off_time")
    assert state.state == "10000"
    assert state.attributes["min"] == 6000 and state.attributes["max"] == 30000
    assert hass.states.get("number.xeno3_auto_deep_sleep").state == "5"
    assert hass.states.get("number.xeno3_ignition_time").attributes["max"] == 60000
    # Shown as sensitivity, like the app: higher = more sensitive.
    assert hass.states.get("number.xeno3_clash_sensitivity").state == "17"
    assert hass.states.get("number.xeno3_swing_sensitivity").state == "600"
    assert hass.states.get("number.xeno3_push_sensitivity").state == "32"
    assert hass.states.get("number.xeno3_accent_swing_sensitivity").state == "4"

    # Blade brightness: the app's 0-100 % slider, shared with the light.
    assert hass.states.get("number.xeno3_blade_brightness").state == "100"
    n = len(fw.writes)
    await _set(hass, "number", "set_value", "number.xeno3_blade_brightness", value=40)
    assert _sent(fw, n) == [b'[2,{"Brightness":40}]']
    fw._push(DATA, [3, {"PowerOn": True}])
    await hass.async_block_till_done()
    assert hass.states.get("light.xeno3_blade").attributes["brightness"] == 102  # 40 % of 255

    n = len(fw.writes)
    await _set(hass, "number", "set_value", "number.xeno3_clash_sensitivity", value=20)
    await _set(hass, "number", "set_value", "number.xeno3_retraction_time", value=750)
    assert _sent(fw, n) == [b'[2,{"ClashSensitivity":10}]', b'[2,{"OffTime":750}]']
    assert hass.states.get("number.xeno3_clash_sensitivity").state == "20"


def test_numbers_follow_the_app_availability_rules() -> None:
    by_key = {d.key: d for d in NUMBERS}
    saber = SimpleNamespace(
        address="AA", name="X", is_connected=True, idle_disconnect=0,
        state={"SideBlade": False, "MainBlade": True, "HardwareType": "RGB", "MotorSpeed": True},
    )
    assert not XenoNumber(saber, by_key["side_blade_length"]).available
    assert XenoNumber(saber, by_key["main_blade_length"]).available
    assert not XenoNumber(saber, by_key["motor_speed"]).available
    assert XenoNumber(saber, by_key["motor_speed"]).native_value is None  # bool is not a number
    saber.state = {}  # nothing reported yet: no reason to hide it
    assert XenoNumber(saber, by_key["side_blade_length"]).available


async def test_selects_switches_buttons_and_sensors(hass: HomeAssistant) -> None:
    _entry, holder = await _reported(hass)
    fw = holder["fw"]
    blaster = hass.states.get("select.xeno3_blaster_effect")
    assert blaster.state == "1" and blaster.attributes["options"] == ["1"]
    # The font has no post-off effect to choose from.
    assert hass.states.get("select.xeno3_post_off_effect").state == STATE_UNAVAILABLE
    assert hass.states.get("select.xeno3_mode").state == "1"

    assert hass.states.get("switch.xeno3_flash_on_clash").state == "on"
    assert hass.states.get("switch.xeno3_fast_color_cycling").state == STATE_OFF
    n = len(fw.writes)
    await _set(hass, "switch", "turn_off", "switch.xeno3_flash_on_clash")
    await _set(hass, "switch", "turn_on", "switch.xeno3_torch_mode")
    await _set(hass, "switch", "turn_on", "switch.xeno3_smooth_color_cycling")
    await _set(hass, "button", "press", "button.xeno3_save_settings")
    await _set(hass, "button", "press", "button.xeno3_restart")
    assert _sent(fw, n) == [
        b'[2,{"ClashOn":false}]',
        b'[2,{"TorchMode":true}]',
        b'[2,{"SmoothColorChanging":true}]',
        b'[2,{"SaveCfg":true}]',
        b'[2,{"Reset":true}]',
    ]
    assert hass.states.get("button.xeno3_restart").attributes["device_class"] == "restart"

    assert hass.states.get("sensor.xeno3_blade_firmware").state == "pixel"
    assert hass.states.get("sensor.xeno3_hardware_type").state == "rgb"
    assert hass.states.get("sensor.xeno3_buttons").state == "1"
    transfer = hass.states.get("sensor.xeno3_file_transfer")
    assert transfer.state == "0" and transfer.attributes["active"] is False

    fw._push(DATA, [3, {"SoftwareType": "Unknown", "ButtonNum": True}])
    await hass.async_block_till_done()
    assert hass.states.get("sensor.xeno3_blade_firmware").state == "unknown"
    assert hass.states.get("sensor.xeno3_buttons").state == "unknown"


def test_file_transfer_sensor_attributes() -> None:
    from custom_components.xeno_saber_ble.device import XenoSaber

    saber = XenoSaber(SimpleNamespace(address="AA", name="X"))
    sensor = XenoFileTransfer(saber)
    assert sensor.available and sensor.native_value == 0
    assert sensor.extra_state_attributes == {"active": False}
    saber.transfer = TransferStatus("download", "/3/hum (1).wav", 250, 1000)
    assert sensor.native_value == 25
    assert sensor.extra_state_attributes == {
        "active": True, "direction": "to_saber", "path": "/3/hum (1).wav",
        "bytes_done": 250, "bytes_total": 1000,
    }
    saber.transfer = TransferStatus("upload", "config.ini", 1, 2)
    assert sensor.extra_state_attributes["direction"] == "from_saber"

    # A font of 4 files: progress covers the whole font, not one file.
    with saber.batch_progress(4):
        saber.batch_step()  # 1 file done, second one half-way
        assert sensor.native_value == 37
        assert sensor.extra_state_attributes["files_done"] == 1
        assert sensor.extra_state_attributes["files_total"] == 4
        saber.transfer = None
        assert sensor.native_value == 25  # between two files
    assert saber.batch is None and saber.progress is None
