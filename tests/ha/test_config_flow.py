"""Config flow: Bluetooth discovery, manual pick, connection test, aborts."""

from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_device import FakeSaber  # noqa: E402

from custom_components.xeno_saber_ble import device as dev  # noqa: E402
from custom_components.xeno_saber_ble.const import DOMAIN  # noqa: E402

ADDR = "AA:BB:CC:DD:EE:FF"
OTHER = "11:22:33:44:55:66"
DATA_UUID = "00003ab0-0000-1000-8000-00805f9b34fb"


def _info(address=ADDR, name="Xeno3", uuids=(DATA_UUID,), manufacturer=None):
    device = MagicMock(address=address)
    device.name = name
    info = MagicMock(
        address=address,
        service_uuids=list(uuids),
        manufacturer_data=manufacturer or {},
        device=device,
    )
    info.name = name
    return info


@pytest.fixture
def saber_link():
    """The saber answers unless ``fail`` is set; records connection attempts."""
    state = {"attempts": 0, "fail": False}

    async def fake_establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        state["attempts"] += 1
        if state["fail"]:
            raise dev.BleakError("Device not found")
        fw = FakeSaber()
        if disconnected_callback:
            fw.on_disconnect = disconnected_callback
        state["fw"] = fw
        return fw

    with (
        patch.object(dev, "establish_connection", fake_establish),
        patch.object(dev, "STATE_SETTLE", 0),
        patch("custom_components.xeno_saber_ble.async_setup_entry", return_value=True),
    ):
        yield state


@pytest.fixture(autouse=True)
def _bluetooth_loaded(hass: HomeAssistant):
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})


async def test_bluetooth_discovery_tests_connection_then_creates_entry(
    hass: HomeAssistant, saber_link
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert saber_link["attempts"] == 0  # no connection before the user confirms

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Xeno3"
    assert result["result"].unique_id == ADDR
    assert saber_link["attempts"] == 1
    assert not saber_link["fw"].is_connected  # released after the test


async def test_bluetooth_discovery_cannot_connect_then_retry(
    hass: HomeAssistant, saber_link
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info()
    )
    saber_link["fail"] = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    saber_link["fail"] = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_bluetooth_discovery_aborts_when_already_configured(
    hass: HomeAssistant, saber_link
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ADDR).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_title_falls_back_to_address(
    hass: HomeAssistant, saber_link
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info(name=None)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["title"] == "Xeno saber EE:FF"


def _discovered(*infos):
    return patch(
        "custom_components.xeno_saber_ble.config_flow.async_discovered_service_info",
        return_value=list(infos),
    )


async def test_user_flow_lists_only_new_sabers_and_creates_entry(
    hass: HomeAssistant, saber_link
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="99:99:99:99:99:99").add_to_hass(hass)
    infos = (
        _info(),
        # Matched by the "Damien" manufacturer data alone (no service UUID in the ad).
        _info(OTHER, "Saber", uuids=(), manufacturer={0x6144: b"mien"}),
        _info("22:22:22:22:22:22", "Headphones", uuids=("0000110b-0000-1000-8000-00805f9b34fb",)),
        _info("99:99:99:99:99:99", "Already added"),
    )
    with _discovered(*infos):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    choices = result["data_schema"].schema["address"].container
    assert set(choices) == {ADDR, OTHER}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": OTHER}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Every board advertises "Saber": the end of the address tells them apart.
    assert result["title"] == "Saber 55:66"
    assert result["result"].unique_id == OTHER


async def test_user_flow_cannot_connect_shows_error(
    hass: HomeAssistant, saber_link
) -> None:
    with _discovered(_info()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        saber_link["fail"] = True
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": ADDR}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}

        saber_link["fail"] = False  # saber woken up: second try succeeds
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": ADDR}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_aborts_when_nothing_found(
    hass: HomeAssistant, saber_link
) -> None:
    with _discovered(_info("22:22:22:22:22:22", "Phone", uuids=())):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_aborts_if_configured_meanwhile(
    hass: HomeAssistant, saber_link
) -> None:
    with _discovered(_info()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    MockConfigEntry(domain=DOMAIN, unique_id=ADDR).add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": ADDR}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
