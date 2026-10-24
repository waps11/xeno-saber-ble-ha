"""Mapping font packs to the saber's file names, and WAV format checks."""

import importlib
from pathlib import Path
import sys
import types
import wave

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "xeno_saber_ble"
_pkg = types.ModuleType("_xsb_standalone")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules.setdefault("_xsb_standalone", _pkg)
sf = importlib.import_module("_xsb_standalone.soundfont")


def wav(path: Path, rate: int = 44100, width: int = 2, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setframerate(rate)
        w.setsampwidth(width)
        w.setnchannels(channels)
        w.writeframes(bytes(width * channels * 10))
    return path


def targets(plan) -> list[tuple[str, str]]:
    return [(item.source.name, item.target_name) for item in plan]


def test_xeno_pack_keeps_its_names(tmp_path: Path) -> None:
    for name in ("hum (1).wav", "clash (2).wav", "clash (1).wav", "swingh (1).wav", "swing (1).wav"):
        wav(tmp_path / name)
    plan, skipped = sf.plan_font(tmp_path)
    assert skipped == []
    assert targets(plan) == [
        ("clash (1).wav", "clash (1).wav"),
        ("clash (2).wav", "clash (2).wav"),
        ("hum (1).wav", "hum (1).wav"),
        ("swing (1).wav", "swing (1).wav"),
        ("swingh (1).wav", "swingh (1).wav"),
    ]


def test_numbered_and_aliased_names_are_renumbered_without_gaps(tmp_path: Path) -> None:
    for name in ("Clash03.WAV", "clash1.wav", "clash_10.wav", "poweron.wav", "pwroff2.wav",
                 "blst01.wav", "bgnlock.wav", "font.wav", "readme.wav", "notes.txt", ".hidden.wav"):
        (wav(tmp_path / name) if name.lower().endswith(".wav") else (tmp_path / name).write_text("x"))
    plan, skipped = sf.plan_font(tmp_path)
    assert skipped == ["readme.wav"]
    assert targets(plan) == [
        ("bgnlock.wav", "beginlock (1).wav"),
        ("blst01.wav", "blaster (1).wav"),
        ("clash1.wav", "clash (1).wav"),
        ("Clash03.WAV", "clash (2).wav"),
        ("clash_10.wav", "clash (3).wav"),
        ("font.wav", "font (1).wav"),
        ("pwroff2.wav", "in (1).wav"),
        ("poweron.wav", "out (1).wav"),
    ]


def test_proffie_layout_reads_type_folders(tmp_path: Path) -> None:
    wav(tmp_path / "hum.wav")
    wav(tmp_path / "clsh" / "clsh02.wav")
    wav(tmp_path / "clsh" / "clsh01.wav")
    wav(tmp_path / "swingl" / "a.wav")
    wav(tmp_path / "swingl" / "b.wav")
    wav(tmp_path / "extras" / "whatever.wav")
    plan, skipped = sf.plan_font(tmp_path)
    assert skipped == ["extras/whatever.wav"]
    assert [i.target_name for i in plan] == [
        "clash (1).wav", "clash (2).wav", "hum (1).wav", "swingl (1).wav", "swingl (2).wav",
    ]
    assert plan[0].source.name == "clsh01.wav"


def test_wav_problems(tmp_path: Path) -> None:
    assert sf.wav_problem(wav(tmp_path / "ok.wav")) is None
    assert "22050 Hz" in sf.wav_problem(wav(tmp_path / "a.wav", rate=22050))
    assert "8-bit" in sf.wav_problem(wav(tmp_path / "b.wav", width=1))
    assert "2 channels" in sf.wav_problem(wav(tmp_path / "c.wav", channels=2))
    junk = tmp_path / "d.wav"
    junk.write_bytes(b"ID3 not a wav")
    assert "not a PCM WAV" in sf.wav_problem(junk)


def test_leftovers_lists_old_files_not_overwritten(tmp_path: Path) -> None:
    for name in ("clash1.wav", "clash2.wav", "hum.wav"):
        wav(tmp_path / name)
    plan, _ = sf.plan_font(tmp_path)
    old = {"clash": (1, 4), "hum": (1, 1), "lock": (1, 2)}
    assert sf.leftovers(old, plan) == [
        "clash (3).wav", "clash (4).wav", "lock (1).wav", "lock (2).wav",
    ]
    assert sf.leftovers({}, plan) == []


def test_sound_file_name_validation() -> None:
    p = importlib.import_module("_xsb_standalone.protocol")
    assert p.sound_file_name("hum", 1) == "hum (1).wav"
    with pytest.raises(ValueError):
        p.sound_file_name("bogus", 1)
    with pytest.raises(ValueError):
        p.sound_file_name("hum", 0)


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_extract_wavs_keeps_only_safe_wav_files(tmp_path: Path) -> None:
    good = wav(tmp_path / "src" / "hum.wav").read_bytes()
    archive = _zip(tmp_path / "font.zip", {
        "My Font/hum.wav": good,
        "My Font/clsh/clsh01.wav": good,
        "My Font/config.txt": b"x",
        "__MACOSX/My Font/._hum.wav": b"junk",
    })
    out = tmp_path / "out"
    sf.extract_wavs(archive, out)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.wav")) == [
        "My Font/clsh/clsh01.wav", "My Font/hum.wav",
    ]
    root = sf.font_root(out)
    assert root == out / "My Font"
    plan, _ = sf.plan_font(root)
    assert [i.target_name for i in plan] == ["clash (1).wav", "hum (1).wav"]


@pytest.mark.parametrize("name", ["../evil.wav", "/abs.wav", "C:/x.wav"])
def test_extract_wavs_refuses_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = _zip(tmp_path / "bad.zip", {name: b"x"})
    with pytest.raises(sf.FontArchiveError, match="unsafe path"):
        sf.extract_wavs(archive, tmp_path / "out")
    assert not (tmp_path / "evil.wav").exists()


def test_extract_wavs_limits_and_bad_files(tmp_path: Path, monkeypatch) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(sf.FontArchiveError, match="not a valid zip"):
        sf.extract_wavs(junk, tmp_path / "out")
    archive = _zip(tmp_path / "big.zip", {"a.wav": b"12345", "b.wav": b"12345"})
    monkeypatch.setattr(sf, "ZIP_MAX_FILES", 1)
    with pytest.raises(sf.FontArchiveError, match="more than 1 WAV"):
        sf.extract_wavs(archive, tmp_path / "out")
    monkeypatch.setattr(sf, "ZIP_MAX_FILES", 10)
    monkeypatch.setattr(sf, "ZIP_MAX_BYTES", 8)
    with pytest.raises(sf.FontArchiveError, match="1 GB"):
        sf.extract_wavs(archive, tmp_path / "out")


def test_font_root_stops_at_type_folders_and_several_folders(tmp_path: Path) -> None:
    wav(tmp_path / "a" / "clsh" / "clsh01.wav")
    assert sf.font_root(tmp_path / "a") == tmp_path / "a"  # clsh is a sound type
    wav(tmp_path / "b" / "one" / "x.wav")
    wav(tmp_path / "b" / "two" / "y.wav")
    assert sf.font_root(tmp_path / "b") == tmp_path / "b"
    deep = tmp_path / "c" / "d1" / "d2" / "d3" / "d4"
    wav(deep / "hum.wav")
    assert sf.font_root(tmp_path / "c") == tmp_path / "c" / "d1" / "d2" / "d3"


def test_exported_zip_round_trip(tmp_path: Path) -> None:
    _zip(tmp_path / "f.zip", {"hum (1).wav": b"abc"})  # what export_sound_font writes
    sf.extract_wavs(tmp_path / "f.zip", tmp_path / "out")
    assert (tmp_path / "out" / "hum (1).wav").read_bytes() == b"abc"


def test_read_backup(tmp_path: Path) -> None:
    import json
    import zipfile

    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in ("2/swing (1).wav", "2/clash (2).wav", "2/clash (1).wav", "2/hum (1).wav",
                     "setting/config.ini", "setting/customfunction.diy", "setting/ready.wav",
                     "2/notes.txt", "__MACOSX/2/._hum (1).wav", "setting/other.ini"):
            zf.writestr(name, b"x")
        zf.writestr(sf.BACKUP_MANIFEST, json.dumps({"fonts": {"2": "Luke"}}))
    index = sf.read_backup(archive)
    # Files are ordered like the saber reports them: by sound type, then number.
    assert index.fonts == {2: ["clash (1).wav", "clash (2).wav", "hum (1).wav", "swing (1).wav"]}
    assert index.settings == ["config.ini", "customfunction.diy"]
    assert index.system == ["ready.wav"]
    assert index.ignored == ["2/notes.txt", "setting/other.ini"]
    assert index.manifest == {"fonts": {"2": "Luke"}} and index.sizes["2/hum (1).wav"] == 1

    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("x/" + sf.BACKUP_MANIFEST, b"")  # only the top-level manifest counts
    assert sf.read_backup(archive).ignored[-1] == "x/xeno_backup.json"

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("1/hum (1).wav", b"x")
        zf.writestr(sf.BACKUP_MANIFEST, b"{not json")
    with pytest.raises(sf.FontArchiveError, match="unreadable"):
        sf.read_backup(bad)
