"""script/build: the release package, and the experimental one."""

import json
from pathlib import Path
import subprocess
import sys
import zipfile

import yaml

ROOT = Path(__file__).resolve().parent.parent
BOARD_ACTIONS = ("update_firmware", "set_blade_firmware", "set_hardware_type")
FEATURES = ("board_actions", "settings_writes")


def _build(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "script" / "build"), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


def _version() -> str:
    manifest = ROOT / "custom_components" / "xeno_saber_ble" / "manifest.json"
    return json.loads(manifest.read_text())["version"]


def test_release_package_has_no_experimental_feature(tmp_path: Path) -> None:
    result = _build(tmp_path, "--out", str(tmp_path))
    assert result.returncode == 0, result.stderr
    archive = tmp_path / f"xeno_saber_ble-{_version()}.zip"
    names = zipfile.ZipFile(archive).namelist()
    assert "xeno_saber_ble/manifest.json" in names
    assert "xeno_saber_ble/CHANGELOG.md" in names
    assert "xeno_saber_ble/board.py" not in names
    assert not any("__pycache__" in n for n in names)
    services = yaml.safe_load(zipfile.ZipFile(archive).read("xeno_saber_ble/services.yaml"))
    assert not set(BOARD_ACTIONS) & set(services)
    # Reproducible: the same sources give the same bytes.
    first = archive.read_bytes()
    archive.unlink()
    assert _build(tmp_path, "--out", str(tmp_path)).returncode == 0
    assert archive.read_bytes() == first


def test_experimental_package_adds_the_board_actions(tmp_path: Path) -> None:
    result = _build(tmp_path, "--experimental", "board_actions", "--dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    package = tmp_path / "xeno_saber_ble"
    assert (package / "board.py").is_file()
    services = yaml.safe_load((package / "services.yaml").read_text())
    assert set(BOARD_ACTIONS) <= set(services)
    for name in (
        "strings.json",
        *(f"translations/{p.name}" for p in (package / "translations").iterdir()),
    ):
        data = json.loads((package / name).read_text(encoding="utf-8"))
        assert set(BOARD_ACTIONS) <= set(data["services"]), name
        assert {"firmware_size", "battery_too_low"} <= set(data["exceptions"]), name
        assert "blade_type" in data["selector"], name
    icons = json.loads((package / "icons.json").read_text())
    assert set(BOARD_ACTIONS) <= set(icons["services"])

    zipped = _build(tmp_path, "--experimental", "board_actions", "--out", str(tmp_path / "dist"))
    assert zipped.returncode == 0, zipped.stderr
    assert (tmp_path / "dist" / f"xeno_saber_ble-{_version()}-experimental.zip").is_file()


def test_unknown_feature_and_existing_folder_are_refused(tmp_path: Path) -> None:
    result = _build(tmp_path, "--experimental", "nope", "--dir", str(tmp_path))
    assert result.returncode != 0 and "unknown experimental feature 'nope'" in result.stderr
    (tmp_path / "again" / "xeno_saber_ble").mkdir(parents=True)
    result = _build(tmp_path, "--dir", str(tmp_path / "again"))
    assert result.returncode != 0 and "already exists" in result.stderr


def test_experimental_package_adds_the_settings_writes(tmp_path: Path) -> None:
    result = _build(tmp_path, "--experimental", "settings_writes", "--dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    package = tmp_path / "xeno_saber_ble"
    assert (package / "settings_writes.py").is_file()
    services = yaml.safe_load((package / "services.yaml").read_text())
    assert "copy_sound_font" in services
    assert {"include_settings", "include_system_sounds"} <= set(
        services["restore_backup"]["fields"]
    )
    assert "confirm" in services["delete_file"]["fields"]
    for name in (
        "strings.json",
        *(f"translations/{p.name}" for p in (package / "translations").iterdir()),
    ):
        data = json.loads((package / name).read_text(encoding="utf-8"))
        assert "copy_sound_font" in data["services"], name
        assert "include_settings" in data["services"]["restore_backup"]["fields"], name
        assert "confirm" in data["services"]["delete_file"]["fields"], name
        assert {"same_font", "backup_other_saber"} <= set(data["exceptions"]), name


def test_release_package_has_no_settings_writes(tmp_path: Path) -> None:
    assert _build(tmp_path, "--dir", str(tmp_path)).returncode == 0
    package = tmp_path / "xeno_saber_ble"
    assert not (package / "settings_writes.py").exists()
    services = yaml.safe_load((package / "services.yaml").read_text())
    assert "copy_sound_font" not in services
    assert "include_settings" not in services["restore_backup"]["fields"]
    assert "confirm" not in services["delete_file"]["fields"]


def test_experimental_package_imports(tmp_path: Path) -> None:
    """The built package loads, with every experimental module (no circular import)."""
    args = [a for f in FEATURES for a in ("--experimental", f)]
    result = _build(tmp_path, *args, "--dir", str(tmp_path / "custom_components"))
    assert result.returncode == 0, result.stderr
    check = (
        "import custom_components.xeno_saber_ble.services as s\n"
        f"assert s.__file__.startswith({str(tmp_path)!r}), s.__file__\n"
        "assert s._board is not None and s._settings_writes is not None\n"
        f"assert [m.FEATURE.split()[0] for m in s._EXPERIMENTAL] == {list(FEATURES)!r}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", check], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
