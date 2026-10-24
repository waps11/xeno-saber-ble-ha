"""Map a folder of WAV files to the saber's sound file names. No Home Assistant imports.

The saber keeps each font in a numbered folder whose files are named
``<type> (<n>).wav``, numbered from 1 without gaps (it only reports the first
and last number of each type). Font packs use other conventions: Xeno packs
already match, Proffie packs use ``clsh01.wav`` or ``clsh/clsh01.wav``, others
``clash1.wav``. This module recognises those and renumbers each type 1..n.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import wave
import zipfile

from . import protocol as p

# Limits for font archives: a full font is a few hundred files and tens of MB.
ZIP_MAX_FILES = 3000
ZIP_MAX_BYTES = 1024 * 1024 * 1024

SAMPLE_RATE = 44100
SAMPLE_WIDTH = 2  # bytes: 16-bit
CHANNELS = 1

# Names other font formats use for the saber's sound types.
ALIASES = {
    "bgndrag": "begindrag",
    "bgnlock": "beginlock",
    "bgnmelt": "beginmelt",
    "bgnlb": "beginlightningblock",
    "endlb": "endlightningblock",
    "lb": "lightningblock",
    "blst": "blaster",
    "clsh": "clash",
    "lockup": "lock",
    "swng": "swing",
    "pstoff": "postoff",
    "poweron": "out",
    "poweroff": "in",
    "pwroff": "in",
    "ignition": "out",
    "retraction": "in",
}
_NAMES = sorted({*p.SOUND_TYPES, *ALIASES}, key=len, reverse=True)
_NAME_RE = "|".join(re.escape(n) for n in _NAMES)
_XENO = re.compile(rf"^({_NAME_RE}) \((\d+)\)$")
_NUMBERED = re.compile(rf"^({_NAME_RE})[ _-]?0*(\d+)$")
_BARE = re.compile(rf"^({_NAME_RE})$")


@dataclass(frozen=True)
class PlannedFile:
    source: Path
    sound_type: str
    index: int

    @property
    def target_name(self) -> str:
        return p.sound_file_name(self.sound_type, self.index)


def _canonical(name: str) -> str:
    return ALIASES.get(name, name)


def _classify(path: Path, root: Path) -> tuple[str, int | None] | None:
    """(sound type, explicit number or None) for one file, or None if unknown."""
    stem = path.stem.strip().lower()
    for pattern in (_XENO, _NUMBERED):
        match = pattern.match(stem)
        if match:
            return _canonical(match.group(1)), int(match.group(2))
    match = _BARE.match(stem)
    if match:
        return _canonical(match.group(1)), None
    # Proffie layout: the folder names the type, files inside are just numbered.
    if path.parent != root:
        folder = _BARE.match(path.parent.name.strip().lower())
        if folder:
            number = re.search(r"(\d+)$", stem)
            return _canonical(folder.group(1)), int(number.group(1)) if number else None
    return None


def _natural_key(text: str) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text.lower())]


def plan_font(root: Path) -> tuple[list[PlannedFile], list[str]]:
    """Plan the upload of the WAVs in ``root`` (and one level of subfolders).

    Returns the planned files, in upload order, and the names of the files
    that were skipped because their sound type could not be recognised.
    """
    candidates = [
        f
        for f in sorted(root.glob("*")) + sorted(root.glob("*/*"))
        if f.suffix.lower() == ".wav" and f.is_file() and not f.name.startswith(".")
    ]
    by_type: dict[str, list[tuple[int | None, str, Path]]] = {}
    skipped: list[str] = []
    for path in candidates:
        found = _classify(path, root)
        if found is None:
            skipped.append(str(path.relative_to(root)))
            continue
        sound_type, number = found
        by_type.setdefault(sound_type, []).append((number, str(path.relative_to(root)), path))
    plan: list[PlannedFile] = []
    for sound_type in p.SOUND_TYPES:
        files = by_type.get(sound_type, [])
        # Explicitly numbered files first, in number order; then the rest by name.
        files.sort(key=lambda f: (f[0] is None, f[0] or 0, _natural_key(f[1])))
        plan += [PlannedFile(path, sound_type, i) for i, (_, _, path) in enumerate(files, 1)]
    return plan, skipped


def wav_problem(path: Path) -> str | None:
    """Why the saber cannot play this file, or None if it is 44.1 kHz 16-bit mono PCM."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate, width, channels = (wav.getframerate(), wav.getsampwidth(), wav.getnchannels())
    except (wave.Error, EOFError, OSError) as err:
        return f"not a PCM WAV file ({err})"
    problems = []
    if rate != SAMPLE_RATE:
        problems.append(f"{rate} Hz instead of {SAMPLE_RATE} Hz")
    if width != SAMPLE_WIDTH:
        problems.append(f"{width * 8}-bit instead of 16-bit")
    if channels != CHANNELS:
        problems.append(f"{channels} channels instead of mono")
    return ", ".join(problems) or None


def leftovers(old: dict[str, tuple[int, int]], plan: list[PlannedFile]) -> list[str]:
    """Files of the old font that the new one does not overwrite."""
    new_count: dict[str, int] = {}
    for item in plan:
        new_count[item.sound_type] = max(new_count.get(item.sound_type, 0), item.index)
    names = []
    for sound_type, (low, high) in old.items():
        start = max(low, new_count.get(sound_type, 0) + 1)
        names += [p.sound_file_name(sound_type, i) for i in range(start, high + 1)]
    return names


class FontArchiveError(ValueError):
    """The archive cannot be used as a font."""


def _hidden(part: str) -> bool:
    return part.startswith(".") or part == "__MACOSX"


def safe_parts(name: str) -> tuple[str, ...]:
    """Path parts of an archive entry; refuses absolute paths and ``..``."""
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts or name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
        raise FontArchiveError(f"unsafe path in archive: {name!r}")
    return parts


def extract_wavs(archive: Path, dest: Path) -> None:
    """Extract the WAV files of a font archive into ``dest``.

    Entries with absolute paths or ``..`` are refused, other files ignored.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            wavs = []
            for member in members:
                parts = safe_parts(member.filename)
                if parts[-1].lower().endswith(".wav") and not any(_hidden(x) for x in parts):
                    wavs.append((member, parts))
            if len(wavs) > ZIP_MAX_FILES:
                raise FontArchiveError(f"more than {ZIP_MAX_FILES} WAV files")
            if sum(m.file_size for m, _ in wavs) > ZIP_MAX_BYTES:
                raise FontArchiveError("more than 1 GB of audio once extracted")
            for member, parts in wavs:
                target = dest.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as err:
        raise FontArchiveError(f"not a valid zip file ({err})") from err


def font_root(folder: Path) -> Path:
    """Skip wrapper folders, as in a zip of ``My Font/`` holding the files."""
    root = folder
    for _ in range(3):
        entries = [e for e in root.iterdir() if not _hidden(e.name)]
        if any(e.is_file() and e.suffix.lower() == ".wav" for e in entries):
            return root
        dirs = [e for e in entries if e.is_dir()]
        if len(dirs) != 1 or _BARE.match(dirs[0].name.strip().lower()):
            return root  # several folders, or a sound-type folder (Proffie layout)
        root = dirs[0]
    return root


# System sounds of a Xeno3 card's "setting" folder (voice prompts and menus),
# as in the vendor's default card. The saber cannot list that folder, so a
# backup tries these names; firmwares may have fewer.
SYSTEM_SOUNDS: tuple[str, ...] = (
    *(
        f"{name}.wav"
        for name in (
            "appconnected",
            "batterylevel",
            "beginningupdate",
            "blade",
            "charging",
            "clashoff",
            "clashon",
            "colorchange",
            "colorselected",
            "countdown",
            "entermultiblockmode",
            "entermultilockmode",
            "exitmultiblockmode",
            "exitmultilockmode",
            "lighteffect",
            "lowbattery",
            "motioncontroloff",
            "motioncontrolon",
            "percent",
            "pixeltoRGB",
            "poweroff",
            "poweron",
            "ready",
            "RGBtopixel",
            "SDcarderror",
            "sensorerror",
            "updatesuccessful",
            "volumelevel",
        )
    ),
    *(f"saber ({i}).wav" for i in range(1, 8)),
    *(
        f"{name} ({i}).wav"
        for name in ("blade", "lighteffect", "power", "volume")
        for i in range(1, 101)
    ),
)


BACKUP_MANIFEST = "xeno_backup.json"
SETTINGS_FILES = ("config.ini", "customfunction.diy")


@dataclass
class BackupIndex:
    """What a backup zip (or a zip of a Xeno SD card) holds, without extracting it."""

    manifest: dict[str, object]
    fonts: dict[int, list[str]] = field(default_factory=dict)  # folder -> sound file names
    settings: list[str] = field(default_factory=list)  # config.ini, customfunction.diy
    system: list[str] = field(default_factory=list)  # other files of setting/
    sizes: dict[str, int] = field(default_factory=dict)  # archive entry -> bytes
    ignored: list[str] = field(default_factory=list)


def _sound_order(name: str) -> tuple[int, int]:
    parsed = p.parse_sound_file_name(name)
    assert parsed is not None  # only parsed names are indexed
    return p.SOUND_TYPES.index(parsed[0]), parsed[1]


def read_backup(archive: Path) -> BackupIndex:
    """Index a backup; refuses unsafe paths, ignores files the saber cannot use."""
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = [m for m in zf.infolist() if not m.is_dir()]
            manifest: dict[str, object] = {}
            if BACKUP_MANIFEST in zf.namelist():
                try:
                    loaded = json.loads(zf.read(BACKUP_MANIFEST))
                except ValueError as err:
                    raise FontArchiveError(f"unreadable {BACKUP_MANIFEST} ({err})") from err
                if isinstance(loaded, dict):
                    manifest = loaded
    except zipfile.BadZipFile as err:
        raise FontArchiveError(f"not a valid zip file ({err})") from err
    index = BackupIndex(manifest)
    for info in infos:
        name = info.filename
        parts = safe_parts(name)
        if name == BACKUP_MANIFEST or any(_hidden(x) for x in parts):
            continue
        index.sizes[name] = info.file_size
        if len(parts) == 2 and parts[0].isdigit() and p.parse_sound_file_name(parts[1]):
            index.fonts.setdefault(int(parts[0]), []).append(parts[1])
        elif len(parts) == 2 and parts[0].lower() == "setting" and parts[1] in SETTINGS_FILES:
            index.settings.append(parts[1])
        elif (
            len(parts) == 2 and parts[0].lower() == "setting" and parts[1].lower().endswith(".wav")
        ):
            index.system.append(parts[1])
        else:
            index.ignored.append(name)
    for files in index.fonts.values():
        files.sort(key=_sound_order)
    if not index.fonts and not index.settings and not index.system:
        raise FontArchiveError("no saber files found (expected 1/, 2/... and setting/)")
    return index
