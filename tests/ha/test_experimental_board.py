"""Experimental firmware and board actions (experimental/board_actions).

They are not part of the integration folder: these tests load the module the
way ``script/build --experimental board_actions`` adds it.
"""

from collections.abc import Iterator
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_services import DATA, _call, _device_id, _expect_invalid, _setup

from custom_components.xeno_saber_ble import services
from custom_components.xeno_saber_ble.const import DOMAIN

BOARD = Path(__file__).resolve().parents[2] / "experimental" / "board_actions" / "board.py"
BOARD_ACTIONS = {"update_firmware", "set_blade_firmware", "set_hardware_type"}


@pytest.fixture
def board_module() -> Iterator[object]:
    name = "custom_components.xeno_saber_ble.board"
    spec = importlib.util.spec_from_file_location(name, BOARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    with patch.object(services, "_board", module):
        yield module
    sys.modules.pop(name, None)


@pytest.fixture
async def saber(hass: HomeAssistant, tmp_path: Path, board_module: object):
    entry, holder = await _setup(hass, tmp_path)
    holder["entry"] = entry
    holder["tmp"] = tmp_path
    yield holder
    holder["stop"]()


def test_not_in_the_integration() -> None:
    """The default build, which HACS installs, has no board module."""
    integration = Path(services.__file__).parent
    assert not (integration / "board.py").exists()
    assert services._experimental("board") is None


async def test_registered_only_in_experimental_builds(
    hass: HomeAssistant, saber, hass_read_only_user
) -> None:
    assert set(hass.services.async_services()[DOMAIN]) >= BOARD_ACTIONS
    user = Context(user_id=hass_read_only_user.id)
    for name, data in (
        ("update_firmware", {"file": str(saber["tmp"] / "x"), "confirm": True}),
        ("set_blade_firmware", {"type": "rgb", "confirm": True}),
        ("set_hardware_type", {"type": "rgb", "confirm": True}),
    ):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                name,
                {"device_id": _device_id(hass, saber), **data},
                blocking=True,
                context=user,
                return_response=True,
            )


async def test_update_firmware(hass: HomeAssistant, saber) -> None:
    tmp, sim, fw = saber["tmp"], saber["sim"], saber["fw"]
    image = tmp / "update.bin"
    image.write_bytes(bytes(20000) + b"DMN_XENO_B_SV1.5.0" + bytes(100))

    await _expect_invalid(
        _call(hass, saber, "update_firmware", file=str(image), confirm=False), "confirm_required"
    )
    small = tmp / "small.bin"
    small.write_bytes(b"tiny")
    await _expect_invalid(
        _call(hass, saber, "update_firmware", file=str(small), confirm=True), "firmware_size"
    )
    fw._push(DATA, [3, {"Power": 12}])
    await hass.async_block_till_done()
    await _expect_invalid(
        _call(hass, saber, "update_firmware", file=str(image), confirm=True), "battery_too_low"
    )
    fw._push(DATA, [3, {"Power": 80}])
    await hass.async_block_till_done()

    result = await _call(hass, saber, "update_firmware", file=str(image), confirm=True)
    assert sim.files["update.bin"] == image.read_bytes()
    assert sim.headers[-1] == ("update.bin", image.stat().st_size, "")
    import hashlib

    assert result == {
        "bytes": image.stat().st_size,
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "previous_version": "1.0",
        "detected_version": "DMN_XENO_B_SV1.5.0",
    }


async def test_blade_firmware_and_hardware_type(hass: HomeAssistant, saber) -> None:
    fw = saber["fw"]
    await _expect_invalid(
        _call(hass, saber, "set_blade_firmware", type="rgb", confirm=False), "confirm_required"
    )
    n = len(fw.writes)
    assert await _call(hass, saber, "set_blade_firmware", type="rgb", confirm=True) == {
        "type": "RGB",
        "changed": True,
    }
    assert [w[1] for w in fw.writes[n:]] == [
        b'[2,{"SoftwareType":"RGB"}]',
        b'[2,{"Reset":true}]',
    ]
    n = len(fw.writes)
    assert await _call(hass, saber, "set_blade_firmware", type="rgb", confirm=True) == {
        "type": "RGB",
        "changed": False,
    }
    assert len(fw.writes) == n

    await _call(hass, saber, "set_hardware_type", type="pixel", buttons=2, confirm=True)
    assert [w[1] for w in fw.writes[n:]] == [
        b'[2,{"HardwareType":"Pixel"}]',
        b'[2,{"ButtonNum":2}]',
        b'[2,{"Reset":true}]',
    ]
    n = len(fw.writes)
    await _call(hass, saber, "set_hardware_type", type="rgb", confirm=True)
    assert [w[1] for w in fw.writes[n:]] == [b'[2,{"HardwareType":"RGB"}]', b'[2,{"Reset":true}]']
