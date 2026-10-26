"""Experimental writes to the saber's settings, and copy_sound_font
(experimental/settings_writes).

They are not part of the integration folder: these tests load the module the
way ``script/build --experimental settings_writes`` adds it.
"""

from collections.abc import Iterator
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_services import DATA, _backup_zip, _call, _device_id, _expect_invalid, _setup

import experimental

from custom_components.xeno_saber_ble import device as dev
from custom_components.xeno_saber_ble.const import DOMAIN


@pytest.fixture
def settings_module() -> Iterator[object]:
    with experimental.enabled("settings_writes") as modules:
        yield modules["settings_writes"]


@pytest.fixture
async def saber(hass: HomeAssistant, tmp_path: Path, settings_module: object):
    entry, holder = await _setup(hass, tmp_path)
    holder["entry"] = entry
    holder["tmp"] = tmp_path
    yield holder
    holder["stop"]()


async def test_registered_only_in_experimental_builds(
    hass: HomeAssistant, saber, hass_read_only_user
) -> None:
    assert "copy_sound_font" in hass.services.async_services()[DOMAIN]
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "copy_sound_font",
            {"device_id": _device_id(hass, saber), "source_font": 1, "destination_font": 2},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
            return_response=True,
        )


async def test_settings_files_can_be_written_and_deleted(hass: HomeAssistant, saber) -> None:
    tmp, sim = saber["tmp"], saber["sim"]
    new = tmp / "config.ini"
    new.write_bytes(b"volume=10\nend=1\n")
    result = await _call(hass, saber, "upload_file", source=str(new), destination="config.ini")
    assert sim.files["config.ini"] == b"volume=10\nend=1\n" and result["bytes"] == 16
    await _call(hass, saber, "copy_file", source="/1/hum (1).wav", destination="/setting/ready.wav")
    assert "/setting/ready.wav" in sim.files
    # Deleting a settings file still needs an explicit confirmation.
    sim.files["/setting/customfunction.diy"] = b"XENO"
    await _expect_invalid(_call(hass, saber, "delete_file", path="config.ini"), "confirm_required")
    await _expect_invalid(
        _call(hass, saber, "delete_file", path="/setting/customfunction.diy"), "confirm_required"
    )
    assert "config.ini" in sim.files
    await _call(hass, saber, "delete_file", path="config.ini", confirm=True)
    assert "config.ini" not in sim.files


async def test_copy_sound_font(hass: HomeAssistant, saber) -> None:
    sim = saber["sim"]
    sim.files["/1/lock (1).wav"] = b"lock"  # only in the destination: removed by replace
    for i in (3, 4):
        del sim.files[f"/2/clash ({i}).wav"]
    result = await _call(hass, saber, "copy_sound_font", source_font=2, destination_font=1)
    assert result == {
        "source_font": 2,
        "destination_font": 1,
        "files": 5,
        "deleted": ["clash (3).wav", "clash (4).wav", "lock (1).wav"],
        "name": "Luke",
    }
    assert sorted(f for f in sim.files if f.startswith("/1/")) == sorted(
        f.replace("/2/", "/1/") for f in sim.files if f.startswith("/2/")
    )
    assert sim.names[1] == "Luke"

    result = await _call(
        hass, saber, "copy_sound_font", source_font=3, destination_font=1, name="Ben", replace=False
    )
    assert result["deleted"] == [] and sim.names[1] == "Ben"

    await _expect_invalid(
        _call(hass, saber, "copy_sound_font", source_font=2, destination_font=2), "same_font"
    )
    sim.names[4] = None
    await _expect_invalid(
        _call(hass, saber, "copy_sound_font", source_font=4, destination_font=1), "font_empty"
    )
    orig = sim.handle
    sim.handle = lambda packet: [b'[3,{"cp":false}]'] if b'"cp"' in packet else orig(packet)
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, saber, "copy_sound_font", source_font=3, destination_font=2)
    assert err.value.translation_key == "saber_refused"


async def test_backup_then_restore(hass: HomeAssistant, saber) -> None:
    sim, tmp = saber["sim"], saber["tmp"]
    sim.files["/setting/customfunction.diy"] = b"XENO" + bytes(60)
    saber["fw"]._push(DATA, [3, {"FileID": [1, 305419896]}])
    await hass.async_block_till_done()
    with patch.object(dev.t, "REPLY_TIMEOUT", 0.01):
        await _call(
            hass,
            saber,
            "backup_saber",
            destination=str(tmp / "full.zip"),
            include_system_sounds=False,
        )
    original = dict(sim.files)
    names = dict(sim.names)

    # Wreck the saber: a font loses files and gets other ones, settings change.
    for f in [f for f in sim.files if f.startswith("/2/")]:
        del sim.files[f]
    sim.files["/2/swing (1).wav"] = b"other"
    sim.names[2] = "Wrong"
    sim.files["config.ini"] = b"volume=0\n"
    sim.files["/setting/customfunction.diy"] = b"XENO" + bytes(4)

    # A dry run checks the backup and writes nothing.
    sim.commands.clear()
    plan = await _call(hass, saber, "restore_backup", source=str(tmp / "full.zip"), dry_run=True)
    assert plan["fonts"]["2"] == {"files": 7, "bytes": 7 * 44, "created": False, "name": "Luke"}
    assert plan["settings"] == ["config.ini", "customfunction.diy"]
    assert plan["system_sounds"] == 0 and "restored" not in plan
    assert not any("FileTransfer" in c or "rm" in c for c in sim.commands)

    await _expect_invalid(
        _call(hass, saber, "restore_backup", source=str(tmp / "full.zip")), "confirm_required"
    )
    result = await _call(hass, saber, "restore_backup", source=str(tmp / "full.zip"), confirm=True)
    assert result["restored"] is True
    assert sim.files == original and sim.names == names
    # Settings are written last, under the names the saber uses for them.
    requested = [c["FileTransfer"][1] for c in sim.commands if "FileTransfer" in c]
    assert requested[-2:] == ["config.ini", "/setting/customfunction.diy"]
    assert saber["fw"].sim.mode is None


async def test_restore_selected_fonts_and_new_folders(hass: HomeAssistant, saber) -> None:
    sim, tmp = saber["sim"], saber["tmp"]
    wav = b"RIFF" + bytes(10)
    source = _backup_zip(
        tmp / "sd.zip",
        {  # an SD card: no manifest, extra files
            "3/hum (1).wav": wav,
            "4/hum (1).wav": wav,
            "4/clash (1).wav": wav,
            "5/out (1).wav": wav,
            "setting/config.ini": b"volume=10\n",
            "setting/poweron.wav": b"on",
            "readme.txt": b"hi",
        },
    )
    plan = await _call(
        hass,
        saber,
        "restore_backup",
        source=str(source),
        dry_run=True,
        fonts=[4, 5],
        include_settings=False,
        include_system_sounds=True,
    )
    assert plan["fonts"]["4"]["created"] and plan["fonts"]["5"]["created"]
    assert plan["fonts"]["4"]["name"] is None
    assert plan["settings"] == [] and plan["system_sounds"] == 1
    assert plan["ignored"] == ["readme.txt"]

    before = sim.files["config.ini"]
    await _call(
        hass,
        saber,
        "restore_backup",
        source=str(source),
        fonts=[4, 5],
        include_settings=False,
        include_system_sounds=True,
        confirm=True,
    )
    assert sim.names[4] == "4" and sim.names[5] == "5"
    assert sim.files["/4/clash (1).wav"] == wav and sim.files["/5/out (1).wav"] == wav
    assert sim.files["/setting/poweron.wav"] == b"on"
    assert sim.files["config.ini"] == before  # settings left alone
    assert "/3/hum (1).wav" in sim.files and sim.files["/3/clash (1).wav"]  # font 3 untouched


async def test_restore_backup_guards(hass: HomeAssistant, saber) -> None:
    sim, tmp = saber["sim"], saber["tmp"]
    wav = b"RIFF" + bytes(10)
    source = _backup_zip(
        tmp / "b.zip",
        {"1/hum (1).wav": wav, "7/hum (1).wav": wav, "setting/config.ini": b"x"},
        {"saber": {"hardware_version": "OTHER"}, "fonts": {"1": "A"}},
    )
    await _expect_invalid(
        _call(hass, saber, "restore_backup", source=str(source), fonts=[2], dry_run=True),
        "font_not_in_backup",
    )
    # Folder 7 cannot be created: 4 is the next free one.
    await _expect_invalid(
        _call(
            hass,
            saber,
            "restore_backup",
            source=str(source),
            fonts=[7],
            dry_run=True,
            include_settings=False,
        ),
        "font_not_found",
    )
    # Settings of a board with another hardware version are refused...
    await _expect_invalid(
        _call(hass, saber, "restore_backup", source=str(source), fonts=[1], dry_run=True),
        "backup_other_saber",
    )
    # ...but its fonts can still be restored.
    plan = await _call(
        hass,
        saber,
        "restore_backup",
        source=str(source),
        fonts=[1],
        include_settings=False,
        dry_run=True,
    )
    assert plan["fonts"]["1"]["name"] == "A"

    (tmp / "bad.zip").write_bytes(b"not a zip")
    await _expect_invalid(
        _call(hass, saber, "restore_backup", source=str(tmp / "bad.zip"), dry_run=True),
        "bad_archive",
    )
    _backup_zip(tmp / "empty.zip", {"music/song.wav": wav})
    await _expect_invalid(
        _call(hass, saber, "restore_backup", source=str(tmp / "empty.zip"), dry_run=True),
        "bad_archive",
    )
    _backup_zip(tmp / "evil.zip", {"../1/hum (1).wav": wav})
    await _expect_invalid(
        _call(hass, saber, "restore_backup", source=str(tmp / "evil.zip"), dry_run=True),
        "bad_archive",
    )

    # Not enough room: refused before the font is written.
    orig = sim.handle
    sim.handle = lambda pk: [b'[3,{"df":[479,479]}]'] if b'"df"' in pk else orig(pk)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            saber,
            "restore_backup",
            source=str(source),
            fonts=[1],
            include_settings=False,
            confirm=True,
        )
    assert "/1/hum (1).wav" not in sim.files or sim.files["/1/hum (1).wav"] != wav
