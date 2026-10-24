"""Actions: sound fonts, files, firmware and board configuration, end to end."""

from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import wave

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_device_files import FileSaber

from custom_components.xeno_saber_ble import device as dev
from custom_components.xeno_saber_ble.const import DOMAIN

ADDR = "AA:BB:CC:DD:EE:FF"
DATA = "00003ab1-0000-1000-8000-00805f9b34fb"


async def _setup(hass: HomeAssistant, tmp_path: Path):
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    holder: dict = {}

    async def fake_establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        fw = FileSaber()
        if "sim" in holder:  # keep the card contents across reconnects
            fw.sim = holder["sim"]
        holder["sim"] = fw.sim
        if disconnected_callback:
            fw.on_disconnect = disconnected_callback
        holder["fw"] = fw
        return fw

    ble_device = MagicMock(address=ADDR)
    ble_device.name = "Xeno3"
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDR, title="Xeno3")
    entry.add_to_hass(hass)
    patches = [
        patch.object(dev, "establish_connection", fake_establish),
        patch.object(dev, "STATE_SETTLE", 0),
        patch.object(dev, "FONT_NAMES_WINDOW", 0.05),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.xeno_saber_ble.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
    ]
    for p in patches:
        p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)  # initial connection
    holder["stop"] = lambda: [p.stop() for p in patches]
    return entry, holder


@pytest.fixture
async def saber(hass: HomeAssistant, tmp_path: Path):
    entry, holder = await _setup(hass, tmp_path)
    holder["entry"] = entry
    holder["tmp"] = tmp_path
    yield holder
    holder["stop"]()


def _device_id(hass: HomeAssistant, holder) -> str:
    from homeassistant.helpers import device_registry as dr

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), holder["entry"].entry_id)
    return device.id


async def _call(hass: HomeAssistant, holder, service: str, response: bool = True, **data):
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"device_id": _device_id(hass, holder), **data},
        blocking=True,
        return_response=response,
    )


def _wav(path: Path, rate: int = 44100, frames: int = 300) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setframerate(rate)
        w.setsampwidth(2)
        w.setnchannels(1)
        w.writeframes(bytes(2 * frames))
    return path


def _font(folder: Path) -> Path:
    for name in ("hum.wav", "clash1.wav", "clash2.wav", "swingl/a.wav"):
        _wav(folder / name)
    (folder / "readme.wav").write_bytes(b"not audio")
    return folder


async def _expect_invalid(coro, key: str) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await coro
    assert err.value.translation_key == key


async def test_all_actions_registered(hass: HomeAssistant, saber) -> None:
    assert set(hass.services.async_services()[DOMAIN]) == {
        "upload_sound_font",
        "export_sound_font",
        "copy_sound_font",
        "backup_saber",
        "restore_backup",
        "list_sound_fonts",
        "list_sound_files",
        "rename_sound_font",
        "play_sound",
        "upload_file",
        "download_file",
        "copy_file",
        "delete_file",
        "update_firmware",
        "set_blade_firmware",
        "set_hardware_type",
    }


async def test_upload_sound_font(hass: HomeAssistant, saber) -> None:
    folder = _font(saber["tmp"] / "New Font")
    sim = saber["sim"]

    # Files whose sound type is not recognised are skipped and reported.
    (folder / "readme.wav").unlink()
    (folder / "notes.wav").write_bytes(b"x")
    dry = await _call(hass, saber, "upload_sound_font", folder=str(folder), font=3, dry_run=True)
    assert dry["files"] == [
        {"source": "clash1.wav", "target": "clash (1).wav"},
        {"source": "clash2.wav", "target": "clash (2).wav"},
        {"source": "hum.wav", "target": "hum (1).wav"},
        {"source": "swingl/a.wav", "target": "swingl (1).wav"},
    ]
    assert dry["skipped"] == ["notes.wav"] and "deleted" not in dry
    assert not any("FileTransfer" in c for c in sim.commands)

    result = await _call(
        hass, saber, "upload_sound_font", folder=str(folder), font=3, name="New Font"
    )
    assert sim.files["/3/clash (1).wav"] == (folder / "clash1.wav").read_bytes()
    assert sim.files["/3/swingl (1).wav"] == (folder / "swingl" / "a.wav").read_bytes()
    assert result["deleted"] == ["clash (3).wav", "clash (4).wav", "in (1).wav", "out (1).wav"]
    assert "/3/clash (3).wav" not in sim.files and "/3/in (1).wav" not in sim.files
    assert sim.names[3] == "New Font" and result["name"] == "New Font"
    assert hass.states.get("sensor.xeno3_file_transfer").state == "0"

    # Without replace, the old files stay.
    result = await _call(
        hass, saber, "upload_sound_font", folder=str(folder), font=2, replace=False
    )
    assert result["deleted"] == [] and "/2/in (1).wav" in sim.files


async def test_upload_sound_font_validation(hass: HomeAssistant, saber, tmp_path: Path) -> None:
    folder = _font(tmp_path / "font")
    upload = lambda **kw: _call(hass, saber, "upload_sound_font", **kw)  # noqa: E731
    await _expect_invalid(upload(folder="/etc", font=1), "path_not_allowed")
    await _expect_invalid(upload(folder=str(tmp_path / "missing"), font=1), "folder_not_found")
    empty = tmp_path / "empty"
    empty.mkdir()
    await _expect_invalid(upload(folder=str(empty), font=1), "no_sound_files")
    await _expect_invalid(upload(folder=str(folder), font=7), "font_not_found")
    _wav(folder / "hum.wav", rate=22050)
    with pytest.raises(ServiceValidationError) as err:
        await upload(folder=str(folder), font=1)
    assert err.value.translation_key == "wav_format"
    assert "22050 Hz" in err.value.translation_placeholders["files"]


async def test_blade_must_be_off_for_transfers(hass: HomeAssistant, saber) -> None:
    folder = _font(saber["tmp"] / "font")
    saber["fw"]._push(DATA, [3, {"PowerOn": True}])
    await hass.async_block_till_done()
    await _expect_invalid(
        _call(hass, saber, "upload_sound_font", folder=str(folder), font=1), "blade_must_be_off"
    )


async def test_upload_and_download_files(hass: HomeAssistant, saber) -> None:
    tmp, sim = saber["tmp"], saber["sim"]
    backup = tmp / "config-backup.ini"
    result = await _call(hass, saber, "download_file", source="config.ini", destination=str(backup))
    assert (
        backup.read_bytes() == sim.files["config.ini"] and result["bytes"] == backup.stat().st_size
    )
    await _expect_invalid(
        _call(hass, saber, "download_file", source="config.ini", destination=str(backup)),
        "file_exists",
    )
    await _call(
        hass, saber, "download_file", source="config.ini", destination=str(backup), overwrite=True
    )
    await _expect_invalid(
        _call(
            hass,
            saber,
            "download_file",
            source="config.ini",
            destination=str(tmp / "nope" / "x.ini"),
        ),
        "folder_not_found",
    )

    new = tmp / "config.ini"
    new.write_bytes(b"volume=10\nend=1\n")
    result = await _call(hass, saber, "upload_file", source=str(new), destination="config.ini")
    assert sim.files["config.ini"] == b"volume=10\nend=1\n" and result["bytes"] == 16
    await _expect_invalid(
        _call(hass, saber, "upload_file", source=str(new), destination="/update.bin"),
        "use_update_firmware",
    )
    await _expect_invalid(
        _call(hass, saber, "upload_file", source=str(tmp / "missing"), destination="x"),
        "file_not_found",
    )
    # Paths are validated before anything reaches the saber.
    with pytest.raises(Exception):  # noqa: B017 - schema error type differs per HA version
        await _call(hass, saber, "upload_file", source=str(new), destination='bad"name')


async def test_file_system_actions(hass: HomeAssistant, saber) -> None:
    sim = saber["sim"]
    fonts = await _call(hass, saber, "list_sound_fonts")
    assert fonts == {
        "fonts": [
            {"number": 1, "name": "Vader"},
            {"number": 2, "name": "Luke"},
            {"number": 3, "name": "Obi-Wan"},
        ],
        "storage": {"used_mb": 1234, "total_mb": 30000, "free_mb": 28766},
    }
    files = await _call(hass, saber, "list_sound_files", font=1)
    assert files["sounds"]["clash"] == [f"clash ({i}).wav" for i in range(1, 5)]

    assert await _call(hass, saber, "rename_sound_font", font=2, name="Skywalker") == {
        "font": 2,
        "name": "Skywalker",
    }
    assert sim.names[2] == "Skywalker"

    await _call(hass, saber, "copy_file", source="/1/hum (1).wav", destination="/2/hum (2).wav")
    assert "/2/hum (2).wav" in sim.files
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, saber, "copy_file", source="/9/none.wav", destination="/2/x.wav")
    assert err.value.translation_key == "saber_refused"
    await _expect_invalid(
        _call(hass, saber, "copy_file", source="/1/hum (1).wav", destination="update.bin"),
        "use_update_firmware",
    )

    await _call(hass, saber, "delete_file", path="/2/hum (2).wav")
    assert "/2/hum (2).wav" not in sim.files
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, saber, "delete_file", path="/2/hum (2).wav")
    assert err.value.translation_key == "saber_refused"

    # The settings file needs an explicit confirmation.
    await _expect_invalid(_call(hass, saber, "delete_file", path="config.ini"), "confirm_required")
    await _expect_invalid(
        _call(hass, saber, "delete_file", path="/setting/customfunction.diy"), "confirm_required"
    )
    assert "config.ini" in sim.files
    await _call(hass, saber, "delete_file", path="config.ini", confirm=True)
    assert "config.ini" not in sim.files

    fw = saber["fw"]
    await _call(hass, saber, "play_sound", response=False, path="/1/hum (1).wav")
    assert fw.writes[-1][1] == b'[2,{"play":"/1/hum (1).wav"}]'
    await _call(hass, saber, "play_sound", response=False)
    assert fw.writes[-1][1] == b'[2,{"play":"null"}]'


async def test_saber_errors_become_ha_errors(hass: HomeAssistant, saber) -> None:
    saber["sim"].silent = True
    with patch.object(dev, "REPLY_TIMEOUT", 0.01), pytest.raises(HomeAssistantError) as err:
        await _call(hass, saber, "list_sound_fonts")
    assert err.value.translation_key == "command_failed"


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


async def test_unloaded_saber(hass: HomeAssistant, saber) -> None:
    await hass.config_entries.async_unload(saber["entry"].entry_id)
    await _expect_invalid(_call(hass, saber, "list_sound_fonts"), "entry_not_loaded")


async def test_upload_sound_font_from_zip(hass: HomeAssistant, saber) -> None:
    import zipfile

    folder = _font(saber["tmp"] / "src")
    archive = saber["tmp"] / "Kyber.zip"
    with zipfile.ZipFile(archive, "w") as zf:  # wrapped in a folder, as packs usually are
        for f in folder.rglob("*.wav"):
            zf.write(f, f"Kyber Workshop/{f.relative_to(folder).as_posix()}")
    result = await _call(hass, saber, "upload_sound_font", folder=str(archive), font=1)
    assert [f["target"] for f in result["files"]] == [
        "clash (1).wav",
        "clash (2).wav",
        "hum (1).wav",
        "swingl (1).wav",
    ]
    assert result["skipped"] == ["readme.wav"]  # a .wav whose sound type is unknown
    assert saber["sim"].files["/1/hum (1).wav"] == (folder / "hum.wav").read_bytes()

    bad = saber["tmp"] / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, saber, "upload_sound_font", folder=str(bad), font=1)
    assert err.value.translation_key == "bad_archive"
    other = saber["tmp"] / "notes.txt"
    other.write_text("x")
    await _expect_invalid(
        _call(hass, saber, "upload_sound_font", folder=str(other), font=1), "folder_not_found"
    )


async def test_export_then_reimport_sound_font(hass: HomeAssistant, saber) -> None:
    import zipfile

    sim, tmp = saber["sim"], saber["tmp"]
    sim.files["/2/hum (1).wav"] = b"luke hum" * 100
    target = tmp / "luke.zip"
    result = await _call(hass, saber, "export_sound_font", font=2, destination=str(target))
    assert result == {
        "font": 2,
        "name": "Luke",
        "path": str(target),
        "files": 7,
        "bytes": sum(len(sim.files[f"/2/{n}"]) for n in zipfile.ZipFile(target).namelist()),
    }
    assert zipfile.ZipFile(target).read("hum (1).wav") == b"luke hum" * 100
    assert not (tmp / "luke.zip.part").exists()

    # A failure part-way leaves neither the zip nor its .part behind.
    sim.reject_transfer = True
    with pytest.raises(HomeAssistantError):
        await _call(hass, saber, "export_sound_font", font=2, destination=str(tmp / "y.zip"))
    assert not (tmp / "y.zip").exists() and not (tmp / "y.zip.part").exists()
    sim.reject_transfer = False
    await _expect_invalid(
        _call(hass, saber, "export_sound_font", font=2, destination=str(target)), "file_exists"
    )
    await _expect_invalid(
        _call(hass, saber, "export_sound_font", font=9, destination=str(tmp / "x.zip")),
        "font_not_found",
    )
    sim.names[4] = "Empty"
    await _expect_invalid(
        _call(hass, saber, "export_sound_font", font=4, destination=str(tmp / "x.zip")),
        "font_empty",
    )


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


async def test_upload_creates_next_font_and_checks_space(hass: HomeAssistant, saber) -> None:
    sim = saber["sim"]
    folder = _font(saber["tmp"] / "font")
    # The next free folder (4) is created like the app does, by naming it.
    result = await _call(hass, saber, "upload_sound_font", folder=str(folder), font=4, name="New")
    assert result["created"] is True and sim.names[4] == "New"
    assert sim.commands[[i for i, c in enumerate(sim.commands) if "setname" in c][0]] == {
        "setname": ["/4", "4"]
    }
    assert "/4/hum (1).wav" in sim.files
    await _expect_invalid(
        _call(hass, saber, "upload_sound_font", folder=str(folder), font=9), "font_not_found"
    )

    # A full card: refused before anything is written.
    orig = sim.handle
    free = {"mb": 0}
    sim.handle = lambda pk: (
        [b'[3,{"df":[%d,479]}]' % (479 - free["mb"])] if b'"df"' in pk else orig(pk)
    )
    before = dict(sim.files)
    await _expect_invalid(
        _call(hass, saber, "upload_sound_font", folder=str(folder), font=1), "not_enough_space"
    )
    assert sim.files == before
    # delete_first frees the old font, then sends the new one.
    free["mb"] = 22
    result = await _call(
        hass, saber, "upload_sound_font", folder=str(folder), font=1, delete_first=True
    )
    assert result["deleted"] == [] and result["created"] is False
    assert sorted(f for f in sim.files if f.startswith("/1/")) == [
        "/1/clash (1).wav",
        "/1/clash (2).wav",
        "/1/hum (1).wav",
        "/1/swingl (1).wav",
    ]


async def test_font_names_are_limited_to_16_characters(hass: HomeAssistant, saber) -> None:
    with pytest.raises(Exception):  # noqa: B017 - schema error type differs per HA version
        await _call(hass, saber, "rename_sound_font", font=1, name="x" * 17)
    assert await _call(hass, saber, "rename_sound_font", font=1, name="x" * 16)


async def test_backup_saber(hass: HomeAssistant, saber) -> None:
    import json
    import zipfile

    sim, tmp, fw = saber["sim"], saber["tmp"], saber["fw"]
    sim.files["/setting/customfunction.diy"] = b"XENO" + bytes(60)
    sim.files["/setting/appconnected.wav"] = b"hello"
    sim.files["/setting/blade (1).wav"] = b"b1"
    sim.files["/setting/blade (2).wav"] = b"b2"
    fw._push(DATA, [3, {"FileID": [1, 305419896]}])  # a custom mapping is installed
    await hass.async_block_till_done()
    target = tmp / "backup.zip"
    with patch.object(dev.t, "REPLY_TIMEOUT", 0.01):
        result = await _call(hass, saber, "backup_saber", destination=str(target))
    names = set(zipfile.ZipFile(target).namelist())
    fonts = {f"{n}/{f.split('/')[2]}" for f in sim.files if f[1].isdigit() for n in [f[1]]}
    assert names == {
        "setting/config.ini",
        "setting/customfunction.diy",
        "setting/appconnected.wav",
        "setting/blade (1).wav",
        "setting/blade (2).wav",
        "xeno_backup.json",
        *fonts,
    }
    assert zipfile.ZipFile(target).read("1/hum (1).wav") == sim.files["/1/hum (1).wav"]
    manifest = json.loads(zipfile.ZipFile(target).read("xeno_backup.json"))
    assert manifest["fonts"] == {"1": "Vader", "2": "Luke", "3": "Obi-Wan"}
    assert manifest["saber"]["software_version"] == "1.0"
    assert manifest["settings"]["Volume"] == 40 and "Power" not in manifest["settings"]
    # A numbered series stops at its first gap: blade (3) is tried, blade (4) is not.
    assert "setting/blade (3).wav" in manifest["missing"]
    assert "setting/blade (4).wav" not in manifest["missing"]
    requested = [c["FileTransfer"][1] for c in sim.commands if "FileTransfer" in c]
    assert "/setting/blade (4).wav" not in requested
    assert result["fonts"] == [1, 2, 3] and result["files"] == len(names) - 1
    assert result["system_sounds_skipped"] is False
    assert not (tmp / "backup.zip.part").exists()


async def test_backup_saber_options_and_failures(hass: HomeAssistant, saber) -> None:
    import zipfile

    sim, tmp = saber["sim"], saber["tmp"]
    # No system sounds readable: given up after a few misses.
    with patch.object(dev.t, "REPLY_TIMEOUT", 0.01):
        result = await _call(
            hass, saber, "backup_saber", destination=str(tmp / "a.zip"), fonts=["2"]
        )
    assert result["system_sounds_skipped"] is True and result["fonts"] == [2]
    requested = [c["FileTransfer"][1] for c in sim.commands if "FileTransfer" in c]
    assert (
        sum(r.startswith("/setting/") and r != "/setting/customfunction.diy" for r in requested)
        == 5
    )
    assert all(not n.startswith(("1/", "3/")) for n in zipfile.ZipFile(tmp / "a.zip").namelist())

    # Settings and fonts only, no custom mapping installed.
    result = await _call(
        hass, saber, "backup_saber", destination=str(tmp / "b.zip"), include_system_sounds=False
    )
    assert "setting/customfunction.diy" not in zipfile.ZipFile(tmp / "b.zip").namelist()

    await _expect_invalid(
        _call(hass, saber, "backup_saber", destination=str(tmp / "b.zip")), "file_exists"
    )
    await _expect_invalid(
        _call(hass, saber, "backup_saber", destination=str(tmp / "c.zip"), fonts=[7]),
        "font_not_found",
    )
    # A required file that cannot be read fails the backup and leaves nothing.
    del sim.files["config.ini"]
    with patch.object(dev.t, "REPLY_TIMEOUT", 0.01), pytest.raises(HomeAssistantError):
        await _call(
            hass, saber, "backup_saber", destination=str(tmp / "d.zip"), include_system_sounds=False
        )
    assert not (tmp / "d.zip").exists() and not (tmp / "d.zip.part").exists()


def _backup_zip(path: Path, files: dict[str, bytes], manifest: dict | None = None) -> Path:
    import json
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if manifest is not None:
            zf.writestr("xeno_backup.json", json.dumps(manifest))
    return path


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


async def test_saber_picked_by_device(hass: HomeAssistant, saber) -> None:
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    (device,) = dr.async_entries_for_config_entry(registry, saber["entry"].entry_id)
    registry.async_update_device(device.id, name_by_user="Sabre de Luke")

    async def call(**data):
        return await hass.services.async_call(
            DOMAIN, "list_sound_fonts", data, blocking=True, return_response=True
        )

    result = await call(device_id=device.id)
    assert len(result["fonts"]) == 3
    with pytest.raises(Exception):  # noqa: B017 - schema error type differs per HA version
        await call()
    await _expect_invalid(call(device_id="nope"), "device_not_saber")
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    lamp = registry.async_get_or_create(config_entry_id=other.entry_id, identifiers={("demo", "x")})
    await _expect_invalid(call(device_id=lamp.id), "device_not_saber")


async def test_sensitive_actions_need_an_admin(
    hass: HomeAssistant, saber, hass_read_only_user
) -> None:
    """Host files, firmware, board configuration and saber storage: admins only."""
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    from custom_components.xeno_saber_ble import services as svc

    user = Context(user_id=hass_read_only_user.id)
    device_id = _device_id(hass, saber)
    tmp = str(saber["tmp"])
    valid = {  # schema-valid calls, so only the permission check can stop them
        "upload_file": {"source": f"{tmp}/x", "destination": "config.ini"},
        "download_file": {"source": "config.ini", "destination": f"{tmp}/x"},
        "upload_sound_font": {"folder": tmp, "font": 1},
        "export_sound_font": {"font": 1, "destination": f"{tmp}/x.zip"},
        "copy_sound_font": {"source_font": 1, "destination_font": 2},
        "backup_saber": {"destination": f"{tmp}/b.zip"},
        "restore_backup": {"source": f"{tmp}/b.zip"},
        "rename_sound_font": {"font": 1, "name": "x"},
        "delete_file": {"path": "/1/hum (1).wav"},
        "copy_file": {"source": "/1/hum (1).wav", "destination": "/2/hum (9).wav"},
        "update_firmware": {"file": f"{tmp}/x", "confirm": True},
        "set_blade_firmware": {"type": "rgb", "confirm": True},
        "set_hardware_type": {"type": "rgb", "confirm": True},
    }
    assert set(valid) == set(svc.HANDLERS) - svc.PUBLIC_SERVICES
    before = dict(saber["sim"].files)
    for name, data in valid.items():
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                name,
                {"device_id": device_id, **data},
                blocking=True,
                context=user,
                return_response=svc.HANDLERS[name][1] is not svc.SupportsResponse.NONE,
            )
    assert saber["sim"].files == before
    # Reading the saber stays open to everyone.
    result = await hass.services.async_call(
        DOMAIN,
        "list_sound_fonts",
        {"device_id": device_id},
        blocking=True,
        context=user,
        return_response=True,
    )
    assert len(result["fonts"]) == 3
    assert {"list_sound_fonts", "list_sound_files", "play_sound"} == svc.PUBLIC_SERVICES
