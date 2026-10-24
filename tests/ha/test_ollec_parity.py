"""Features ported from Ollec's ha-xenopixel: keepalive, lockup/drag, diagnostics, colour sync."""

from datetime import timedelta
from pathlib import Path
import socket
import sys
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_device import FakeSaber  # noqa: E402

from custom_components.xeno_saber_ble import (
    device as dev,  # noqa: E402
    sync as sync_mod,  # noqa: E402
)
from custom_components.xeno_saber_ble.const import (  # noqa: E402
    CONF_IDLE_DISCONNECT,
    CONF_KEEPALIVE,
    CONF_SYNC_LIGHT,
    CONF_WLED_SOURCE,
    CONF_WLED_UDP,
    DOMAIN,
    ISSUE_KEEPALIVE,
)
from custom_components.xeno_saber_ble.wled_udp import WledUdpHub  # noqa: E402

ADDR = "AA:BB:CC:DD:EE:FF"
DATA = "00003ab1-0000-1000-8000-00805f9b34fb"


async def _setup(hass: HomeAssistant, options: dict | None = None):
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})
    holder: dict = {"all": []}

    async def fake_establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        fw = FakeSaber()
        if disconnected_callback:
            fw.on_disconnect = disconnected_callback
        holder["fw"] = fw
        holder["all"].append(fw)
        return fw

    ble_device = MagicMock(address=ADDR)
    ble_device.name = "Xeno3"
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDR, title="Xeno3", options=options or {}
    )
    entry.add_to_hass(hass)
    patches = (
        patch.object(dev, "establish_connection", fake_establish),
        patch.object(dev, "STATE_SETTLE", 0),
        patch.object(sync_mod, "MIN_INTERVAL", 0),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
    )
    for p in patches:
        p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    holder["stop"] = lambda: [p.stop() for p in patches]
    return entry, holder


def _device(hass: HomeAssistant, entry):
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    return devices[0]


def _sent(fw, since=0):
    return [w[1] for w in fw.writes[since:]]


# --------------------------------------------------------------- entities
async def test_diagnostics_lockup_drag_and_versions(hass: HomeAssistant) -> None:
    entry, holder = await _setup(hass)
    fw = holder["fw"]

    assert hass.states.get("binary_sensor.xeno3_connected").state == STATE_ON
    # Authorized is a debugging aid: registered but disabled by default.
    from homeassistant.helpers import entity_registry as er

    reg_entry = er.async_get(hass).async_get("binary_sensor.xeno3_authorized")
    assert reg_entry is not None and reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get("binary_sensor.xeno3_authorized") is None
    assert hass.states.get("switch.xeno3_lockup").state == STATE_OFF
    assert hass.states.get("switch.xeno3_drag").state == STATE_OFF
    # No colour sync source configured: no sync switch.
    assert hass.states.get("switch.xeno3_color_sync") is None

    device = _device(hass, entry)
    assert device.model == "XENO3" and device.sw_version == "1.0"
    # A new firmware version reported later reaches the device.
    fw._push(DATA, [3, {"SoftwareVersion": "1.4.0"}])
    await hass.async_block_till_done()
    device = _device(hass, entry)
    assert device.sw_version == "1.4.0"

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.xeno3_lockup"}, blocking=True
    )
    assert fw.writes[-1][1] == b'[2,{"Lockup":true}]'
    assert hass.states.get("switch.xeno3_lockup").state == STATE_ON
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.xeno3_drag"}, blocking=True
    )
    assert fw.writes[-1][1] == b'[2,{"Drag":true}]'

    # Reconnect: a fresh session, entities stay available.
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.xeno3_reconnect"}, blocking=True
    )
    await hass.async_block_till_done()
    assert len(holder["all"]) == 2 and not fw.is_connected
    assert hass.states.get("binary_sensor.xeno3_connected").state == STATE_ON

    # Link lost: "connected" turns off rather than going unavailable.
    holder["fw"].is_connected = False
    holder["fw"].on_disconnect(holder["fw"])
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.xeno3_connected").state == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    holder["stop"]()


# -------------------------------------------------------------- keepalive
async def test_keepalive_off_by_default_has_no_warning(hass: HomeAssistant) -> None:
    entry, holder = await _setup(hass)
    fw = holder["fw"]
    n = len(fw.writes)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()
    assert len(fw.writes) == n
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_KEEPALIVE}_{entry.entry_id}") is None
    holder["stop"]()


async def test_keepalive_sends_and_raises_battery_warning(hass: HomeAssistant) -> None:
    entry, holder = await _setup(hass, {CONF_KEEPALIVE: 30})
    fw = holder["fw"]

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_KEEPALIVE}_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == ISSUE_KEEPALIVE
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"name": "Xeno3", "interval": "30"}

    n = len(fw.writes)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert _sent(fw, n) == [b'[2,{"Brightness":100}]']

    # Turning keepalive off (options change reloads the entry) clears the warning.
    hass.config_entries.async_update_entry(entry, options={CONF_KEEPALIVE: 0})
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_KEEPALIVE}_{entry.entry_id}") is None
    holder["stop"]()


# ------------------------------------------------------------ options flow
async def test_options_validation(hass: HomeAssistant) -> None:
    entry, holder = await _setup(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {CONF_IDLE_DISCONNECT: 60, CONF_KEEPALIVE: 30, CONF_WLED_UDP: False},
    )
    assert result["errors"] == {CONF_KEEPALIVE: "keepalive_needs_persistent"}

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            CONF_IDLE_DISCONNECT: 0,
            CONF_KEEPALIVE: 0,
            CONF_SYNC_LIGHT: "light.xeno3_blade",
            CONF_WLED_UDP: True,
            CONF_WLED_SOURCE: "not-an-ip",
        },
    )
    assert result["errors"] == {
        CONF_SYNC_LIGHT: "sync_self",
        CONF_WLED_SOURCE: "invalid_ip",
    }

    with patch("custom_components.xeno_saber_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"],
            {
                CONF_IDLE_DISCONNECT: 0,
                CONF_KEEPALIVE: 0,
                CONF_SYNC_LIGHT: "light.living_room",
                CONF_WLED_UDP: True,
                CONF_WLED_SOURCE: " 192.168.1.50 ",
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_IDLE_DISCONNECT: 0,
        CONF_KEEPALIVE: 0,
        CONF_SYNC_LIGHT: "light.living_room",
        CONF_WLED_UDP: True,
        CONF_WLED_SOURCE: "192.168.1.50",
    }
    holder["stop"]()


# --------------------------------------------------------------- colour sync
async def test_sync_follows_a_light(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "light.living_room", STATE_ON, {"rgb_color": (0, 255, 0), "brightness": 128}
    )
    entry, holder = await _setup(hass, {CONF_SYNC_LIGHT: "light.living_room"})
    fw = holder["fw"]
    sync_switch = "switch.xeno3_color_sync"
    assert hass.states.get(sync_switch).state == STATE_OFF

    # Off: changes to the light are ignored.
    n = len(fw.writes)
    hass.states.async_set("light.living_room", STATE_ON, {"rgb_color": (1, 2, 3), "brightness": 255})
    await hass.async_block_till_done()
    assert len(fw.writes) == n

    # On: matches the light immediately (colour, brightness, then ignition).
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: sync_switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert _sent(fw, n) == [
        b'[2,{"BackgroundColor":[1,2,3]}]',
        b'[2,{"PowerOn":true}]',  # brightness 255 = 100, already the saber's value
    ]

    n = len(fw.writes)
    hass.states.async_set("light.living_room", STATE_ON, {"rgb_color": (255, 0, 0), "brightness": 128})
    await hass.async_block_till_done()
    assert _sent(fw, n) == [b'[2,{"BackgroundColor":[255,0,0]}]', b'[2,{"Brightness":50}]']

    n = len(fw.writes)
    hass.states.async_set("light.living_room", "unavailable")
    await hass.async_block_till_done()
    assert len(fw.writes) == n  # unavailable: blade left alone

    hass.states.async_set("light.living_room", STATE_OFF)
    await hass.async_block_till_done()
    assert _sent(fw, n) == [b'[2,{"PowerOn":false}]']

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: sync_switch}, blocking=True
    )
    n = len(fw.writes)
    hass.states.async_set("light.living_room", STATE_ON, {"rgb_color": (0, 0, 9)})
    await hass.async_block_till_done()
    assert len(fw.writes) == n
    holder["stop"]()


async def test_sync_from_wled_udp(hass: HomeAssistant, socket_enabled) -> None:
    hub = WledUdpHub(port=0, host="127.0.0.1")
    hass.data[sync_mod.WLED_HUB_KEY] = hub
    entry, holder = await _setup(
        hass, {CONF_WLED_UDP: True, CONF_WLED_SOURCE: "127.0.0.1"}
    )
    fw = holder["fw"]
    assert hub._transport is None  # socket only open while sync is on

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.xeno3_color_sync"}, blocking=True
    )
    assert hub._transport is not None

    def send(bri, r, g, b):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(bytes([0, 1, bri, r, g, b] + [0] * 18), ("127.0.0.1", hub.port))

    async def settle():
        for _ in range(20):
            await hass.async_block_till_done()
            import asyncio

            await asyncio.sleep(0.01)

    n = len(fw.writes)
    send(255, 0, 0, 255)
    await settle()
    assert _sent(fw, n) == [b'[2,{"PowerOn":true}]']  # colour and brightness already match

    n = len(fw.writes)
    send(51, 255, 128, 0)
    await settle()
    assert _sent(fw, n) == [b'[2,{"BackgroundColor":[255,128,0]}]', b'[2,{"Brightness":20}]']

    n = len(fw.writes)
    send(0, 255, 128, 0)  # brightness 0 = off, like Ollec's proxy
    await settle()
    assert _sent(fw, n) == [b'[2,{"PowerOn":false}]']

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.xeno3_color_sync"}, blocking=True
    )
    assert hub._transport is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    holder["stop"]()


async def test_sync_udp_port_busy_raises_translated_error(hass: HomeAssistant, socket_enabled) -> None:
    from homeassistant.exceptions import HomeAssistantError

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
        taken.bind(("127.0.0.1", 0))
        hass.data[sync_mod.WLED_HUB_KEY] = WledUdpHub(
            port=taken.getsockname()[1], host="127.0.0.1"
        )
        _entry, holder = await _setup(hass, {CONF_WLED_UDP: True})
        with pytest.raises(HomeAssistantError) as exc_info:
            await hass.services.async_call(
                "switch", "turn_on", {ATTR_ENTITY_ID: "switch.xeno3_color_sync"}, blocking=True
            )
    assert exc_info.value.translation_key == "sync_unavailable"
    assert hass.states.get("switch.xeno3_color_sync").state == STATE_OFF
    holder["stop"]()


async def test_sync_switch_restores_after_restart(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    from homeassistant.core import State

    mock_restore_cache(hass, [State("switch.xeno3_color_sync", STATE_ON)])
    hass.states.async_set(
        "light.living_room", STATE_ON, {"rgb_color": (9, 9, 9), "brightness": 255}
    )
    _entry, holder = await _setup(hass, {CONF_SYNC_LIGHT: "light.living_room"})
    await hass.async_block_till_done()
    assert hass.states.get("switch.xeno3_color_sync").state == STATE_ON
    assert b'[2,{"BackgroundColor":[9,9,9]}]' in _sent(holder["fw"])
    holder["stop"]()


async def test_sync_holds_updates_while_saber_sleeps_then_catches_up(hass: HomeAssistant) -> None:
    """Permanent mode: no reconnect storm for a sleeping saber; catch up on wake."""
    hass.states.async_set("light.living_room", STATE_OFF)
    entry, holder = await _setup(hass, {CONF_SYNC_LIGHT: "light.living_room"})
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.xeno3_color_sync"}, blocking=True
    )
    await hass.async_block_till_done()

    fw = holder["fw"]
    fw.is_connected = False
    fw.on_disconnect(fw)  # saber falls asleep
    await hass.async_block_till_done()

    for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 9)):
        hass.states.async_set("light.living_room", STATE_ON, {"rgb_color": rgb, "brightness": 255})
        await hass.async_block_till_done()
    assert len(holder["all"]) == 1  # never tried to wake it

    # Saber wakes; HA reconnects (here through the button) and applies the latest state only.
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.xeno3_reconnect"}, blocking=True
    )
    await hass.async_block_till_done()
    assert len(holder["all"]) == 2
    sent = _sent(holder["fw"], 2)  # after handshake + authorize
    assert sent == [b'[2,{"BackgroundColor":[0,0,9]}]', b'[2,{"PowerOn":true}]']
    assert await hass.config_entries.async_unload(entry.entry_id)
    holder["stop"]()


async def test_sync_backs_off_from_unreachable_saber_in_on_demand_mode(hass: HomeAssistant) -> None:
    from bleak.exc import BleakError

    hass.states.async_set("light.living_room", STATE_OFF)
    entry, holder = await _setup(
        hass, {CONF_SYNC_LIGHT: "light.living_room", CONF_IDLE_DISCONNECT: 60}
    )
    attempts = []

    async def unreachable(cls, device, name, disconnected_callback=None, max_attempts=3):
        attempts.append(1)
        raise BleakError("Device with address AA:BB:CC:DD:EE:FF was not found")

    with patch.object(dev, "establish_connection", unreachable), patch(
        "custom_components.xeno_saber_ble.entity.bluetooth.async_address_present",
        return_value=True,
    ):
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: "switch.xeno3_color_sync"}, blocking=True
        )
        await hass.async_block_till_done()
        assert len(attempts) == 1  # matching the light (off) needed a connection
        for rgb in ((255, 0, 0), (0, 255, 0)):
            hass.states.async_set("light.living_room", STATE_ON, {"rgb_color": rgb})
            await hass.async_block_till_done()
        assert len(attempts) == 1  # backing off, no attempt per update
    assert await hass.config_entries.async_unload(entry.entry_id)
    holder["stop"]()
