"""Actions: sound fonts, files on the saber's card, firmware and board configuration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
import importlib
import json
import logging
from pathlib import Path
import re
import tempfile
import time
from types import ModuleType
from typing import TYPE_CHECKING, Any, TypeVar
import zipfile

try:  # Home Assistant 2026.x replaced voluptuous with probatio
    import probatio as vol
except ImportError:  # older Home Assistant
    import voluptuous as vol  # type: ignore[no-redef]

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from . import protocol as p, soundfont
from .const import DOMAIN
from .device import XenoSaber, XenoSaberError

if TYPE_CHECKING:
    from . import XenoConfigEntry

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

ATTR_DEVICE_ID = "device_id"
ATTR_SOURCE = "source"
ATTR_DESTINATION = "destination"
ATTR_OVERWRITE = "overwrite"
ATTR_FOLDER = "folder"
ATTR_FONT = "font"
ATTR_NAME = "name"
ATTR_REPLACE = "replace"
ATTR_DRY_RUN = "dry_run"
ATTR_PATH = "path"
ATTR_CONFIRM = "confirm"
ATTR_DELETE_FIRST = "delete_first"
ATTR_FONTS = "fonts"
ATTR_SYSTEM_SOUNDS = "include_system_sounds"

SERVICE_UPLOAD_FILE = "upload_file"
SERVICE_DOWNLOAD_FILE = "download_file"
SERVICE_UPLOAD_SOUND_FONT = "upload_sound_font"
SERVICE_EXPORT_SOUND_FONT = "export_sound_font"
SERVICE_BACKUP = "backup_saber"
SERVICE_RESTORE = "restore_backup"
SERVICE_LIST_SOUND_FONTS = "list_sound_fonts"
SERVICE_LIST_SOUND_FILES = "list_sound_files"
SERVICE_RENAME_SOUND_FONT = "rename_sound_font"
SERVICE_DELETE_FILE = "delete_file"
SERVICE_COPY_FILE = "copy_file"
SERVICE_PLAY_SOUND = "play_sound"

# Writing these makes the saber install firmware at its next start.
_PROTECTED = frozenset({"update.bin", "update_factory.bin", "update_factory.ini"})

# The saber's settings: config.ini and the setting/ folder (custom button
# mapping, voice prompts). A bad write or a deletion can leave it showing
# "error" at boot, so only the settings_writes experimental build changes them.
_SETTINGS = re.compile(r"^/?(setting/|config\.ini$)", re.IGNORECASE)
# Paths on the saber end up inside a JSON string and a 128-byte header.
_SABER_PATH = re.compile(r'^[^"\\\x00-\x1f]{1,100}$')
_FONT_NAME = re.compile(r'^[^"\\\x00-\x1f]{1,16}$')  # the app allows 16 characters


def _saber_path(value: Any) -> str:
    path = cv.string(value).strip()
    if not _SABER_PATH.match(path):
        raise vol.Invalid("invalid path on the saber")
    return path


def _font_name(value: Any) -> str:
    name = cv.string(value).strip()
    if not _FONT_NAME.match(name):
        raise vol.Invalid("font names are 1 to 16 characters, without quotes")
    return name


# The saber is picked as a device: the picker shows the names given in HA.
_ENTRY = {vol.Required(ATTR_DEVICE_ID): cv.string}
_FONT_NUMBER = vol.All(vol.Coerce(int), vol.Range(min=1, max=99))

SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_UPLOAD_FILE: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_SOURCE): cv.string,
            vol.Required(ATTR_DESTINATION): _saber_path,
        }
    ),
    SERVICE_DOWNLOAD_FILE: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_SOURCE): _saber_path,
            vol.Required(ATTR_DESTINATION): cv.string,
            vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        }
    ),
    SERVICE_UPLOAD_SOUND_FONT: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_FOLDER): cv.string,
            vol.Required(ATTR_FONT): vol.All(vol.Coerce(int), vol.Range(min=1, max=99)),
            vol.Optional(ATTR_NAME): _font_name,
            vol.Optional(ATTR_REPLACE, default=True): cv.boolean,
            vol.Optional(ATTR_DELETE_FIRST, default=False): cv.boolean,
            vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        }
    ),
    SERVICE_EXPORT_SOUND_FONT: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_FONT): _FONT_NUMBER,
            vol.Required(ATTR_DESTINATION): cv.string,
            vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        }
    ),
    SERVICE_BACKUP: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_DESTINATION): cv.string,
            vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
            vol.Optional(ATTR_FONTS): vol.All(cv.ensure_list, [_FONT_NUMBER]),
            vol.Optional(ATTR_SYSTEM_SOUNDS, default=True): cv.boolean,
        }
    ),
    SERVICE_RESTORE: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_SOURCE): cv.string,
            vol.Optional(ATTR_FONTS): vol.All(cv.ensure_list, [_FONT_NUMBER]),
            vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
            vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
        }
    ),
    SERVICE_LIST_SOUND_FONTS: vol.Schema(_ENTRY),
    SERVICE_LIST_SOUND_FILES: vol.Schema(
        {**_ENTRY, vol.Required(ATTR_FONT): vol.All(vol.Coerce(int), vol.Range(min=1, max=99))}
    ),
    SERVICE_RENAME_SOUND_FONT: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_FONT): vol.All(vol.Coerce(int), vol.Range(min=1, max=99)),
            vol.Required(ATTR_NAME): _font_name,
        }
    ),
    SERVICE_DELETE_FILE: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_PATH): _saber_path,
        }
    ),
    SERVICE_COPY_FILE: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_SOURCE): _saber_path,
            vol.Required(ATTR_DESTINATION): _saber_path,
        }
    ),
    SERVICE_PLAY_SOUND: vol.Schema({**_ENTRY, vol.Optional(ATTR_PATH): _saber_path}),
}


# --------------------------------------------------------------------- helpers
def _invalid(key: str, **placeholders: Any) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()},
    )


def _saber(hass: HomeAssistant, call: ServiceCall) -> XenoSaber:
    device_id = str(call.data[ATTR_DEVICE_ID])
    device = dr.async_get(hass).async_get(device_id)
    for entry_id in device.config_entries if device else ():
        entry: XenoConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        if entry.state is not ConfigEntryState.LOADED:
            raise _invalid("entry_not_loaded", name=entry.title)
        return entry.runtime_data.saber
    raise _invalid("device_not_saber", device_id=device_id)


async def _on_saber(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a saber operation, turning its errors into Home Assistant errors."""
    try:
        return await coro
    except XenoSaberError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": str(err)},
        ) from err


async def _local_path(hass: HomeAssistant, raw: str, *, kind: str) -> Path:
    """Resolve a path on the Home Assistant host and check it is allowed.

    ``kind`` is "file" or "dir" for something that must exist, "font" for a
    folder or a .zip, "new" for a file about to be written. Relative paths are
    relative to the config folder.
    """
    path, _is_dir = await _resolve_local_path(hass, raw, kind)
    return path


async def _resolve_local_path(hass: HomeAssistant, raw: str, kind: str) -> tuple[Path, bool]:
    """``_local_path``, plus whether the path is a folder (one executor job)."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(hass.config.path(raw))

    def _check() -> tuple[str | None, bool]:
        if not hass.config.is_allowed_path(str(path)):
            return "path_not_allowed", False
        is_dir = path.is_dir()
        if kind == "file" and not path.is_file():
            return "file_not_found", is_dir
        if kind == "dir" and not is_dir:
            return "folder_not_found", is_dir
        if kind == "font" and not (is_dir or (path.suffix.lower() == ".zip" and path.is_file())):
            return "folder_not_found", is_dir
        if kind == "new" and not path.parent.is_dir():
            return "folder_not_found", is_dir
        return None, is_dir

    problem, is_dir = await hass.async_add_executor_job(_check)
    if problem is not None:
        shown = path.parent if problem == "folder_not_found" and kind == "new" else path
        raise _invalid(problem, path=shown)
    return path, is_dir


async def _require_blade_off(saber: XenoSaber) -> None:
    """The app refuses file and firmware operations while the blade is lit."""
    await _on_saber(saber.connect())
    if saber.blade_on:
        raise _invalid("blade_must_be_off")


def _require_settings_writes(path: str) -> None:
    if _SETTINGS.match(path) and _settings_writes is None:
        raise _invalid("settings_write_held_back", path=path)


def _require_confirm(call: ServiceCall) -> None:
    if call.data.get(ATTR_CONFIRM) is not True:
        raise _invalid("confirm_required")


async def _read(hass: HomeAssistant, path: Path) -> bytes:
    return await hass.async_add_executor_job(path.read_bytes)


# --------------------------------------------------------------------- files
async def _upload_file(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    destination: str = call.data[ATTR_DESTINATION]
    if destination.strip("/").lower() in _PROTECTED:
        raise _invalid("firmware_file_protected", path=destination)
    _require_settings_writes(destination)
    source = await _local_path(hass, call.data[ATTR_SOURCE], kind="file")
    await _require_blade_off(saber)
    data = await _read(hass, source)
    await _on_saber(saber.send_file(destination, data))
    return {"path": destination, "bytes": len(data)}


async def _download_file(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    destination = await _local_path(hass, call.data[ATTR_DESTINATION], kind="new")
    overwrite: bool = call.data[ATTR_OVERWRITE]
    if not overwrite and await hass.async_add_executor_job(destination.exists):
        raise _invalid("file_exists", path=destination)
    await _require_blade_off(saber)
    data = await _on_saber(saber.receive_file(call.data[ATTR_SOURCE]))
    await hass.async_add_executor_job(destination.write_bytes, data)
    return {"path": str(destination), "bytes": len(data)}


async def _delete_file(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    path: str = call.data[ATTR_PATH]
    if _SETTINGS.match(path):
        _require_settings_writes(path)
        _require_confirm(call)
    if not await _on_saber(saber.file_command(p.FILE_RM, path)):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="saber_refused",
            translation_placeholders={"operation": f"rm {path}"},
        )
    return {"path": path, "deleted": True}


async def _copy_file(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    source: str = call.data[ATTR_SOURCE]
    destination: str = call.data[ATTR_DESTINATION]
    if destination.strip("/").lower() in _PROTECTED:
        raise _invalid("firmware_file_protected", path=destination)
    _require_settings_writes(destination)
    if not await _on_saber(saber.file_command(p.FILE_CP, [source, destination])):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="saber_refused",
            translation_placeholders={"operation": f"cp {source} {destination}"},
        )
    return {"source": source, "destination": destination}


async def _play_sound(hass: HomeAssistant, call: ServiceCall) -> None:
    saber = _saber(hass, call)
    path: str | None = call.data.get(ATTR_PATH)
    # The app sends the string "null" to stop playback.
    await _on_saber(saber.send_raw(p.encode_command(p.FILE_PLAY, path or "null")))


# ----------------------------------------------------------------- sound fonts
async def _list_sound_fonts(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    fonts = await _on_saber(saber.list_fonts())
    used, total = await _on_saber(saber.storage())
    return {
        "fonts": [{"number": n, "name": name} for n, name in sorted(fonts.items())],
        "storage": {"used_mb": used, "total_mb": total, "free_mb": total - used},
    }


async def _list_sound_files(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    font: int = call.data[ATTR_FONT]
    ranges = await _on_saber(saber.list_sounds(font))
    return {
        "font": font,
        "sounds": {
            sound_type: [p.sound_file_name(sound_type, i) for i in range(low, high + 1)]
            for sound_type, (low, high) in ranges.items()
        },
    }


async def _rename_sound_font(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    font: int = call.data[ATTR_FONT]
    name: str = call.data[ATTR_NAME]
    await _on_saber(saber.file_command(p.FILE_SETNAME, [f"/{font}", name]))
    return {"font": font, "name": name}


def _next_font(fonts: dict[int, str | None]) -> int:
    """The number the app gives a new font: the first gap, else last + 1."""
    number = 1
    while number in fonts:
        number += 1
    return number


async def _existing_font(
    saber: XenoSaber, font: int, *, create: bool = False
) -> dict[int, str | None]:
    """Check that font folder ``font`` exists, or create it like the app does."""
    fonts = await _on_saber(saber.list_fonts())
    if font in fonts:
        return fonts
    if create and font == _next_font(fonts):
        # The app adds a font by naming the next free folder.
        await _on_saber(saber.file_command(p.FILE_SETNAME, [f"/{font}", str(font)]))
        return {**fonts, font: str(font)}
    raise _invalid(
        "font_not_found",
        font=font,
        available=", ".join(str(n) for n in sorted(fonts)) or "-",
        next=_next_font(fonts),
    )


MIB = 1024 * 1024


async def _require_space(saber: XenoSaber, size: int) -> None:
    """Refuse an upload that cannot fit, before anything is written."""
    used, total = await _on_saber(saber.storage())
    needed = -(-size // MIB) + 1  # rounded up, plus 1 MiB of margin
    if needed > total - used:
        raise _invalid("not_enough_space", needed=needed, free=total - used)


async def _upload_sound_font(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    source, is_dir = await _resolve_local_path(hass, call.data[ATTR_FOLDER], "font")
    if is_dir:
        return await _upload_font_folder(hass, call, source)
    # A .zip: extract its WAVs to a private temporary folder first.
    tmp = await hass.async_add_executor_job(tempfile.TemporaryDirectory, None, "xeno_font_")
    try:
        extracted = Path(tmp.name)
        try:
            await hass.async_add_executor_job(soundfont.extract_wavs, source, extracted)
        except soundfont.FontArchiveError as err:
            raise _invalid("bad_archive", path=source, error=err) from err
        return await _upload_font_folder(hass, call, extracted, shown=source)
    finally:
        await hass.async_add_executor_job(tmp.cleanup)


async def _upload_font_folder(
    hass: HomeAssistant, call: ServiceCall, given: Path, shown: Path | None = None
) -> ServiceResponse:
    saber = _saber(hass, call)
    font: int = call.data[ATTR_FONT]
    folder = await hass.async_add_executor_job(soundfont.font_root, given)

    def _prepare() -> tuple[list[soundfont.PlannedFile], list[str], list[str], int]:
        plan, skipped = soundfont.plan_font(folder)
        bad = []
        for item in plan:
            problem = soundfont.wav_problem(item.source)
            if problem is not None:
                bad.append(f"{item.source.relative_to(folder)}: {problem}")
        size = sum(item.source.stat().st_size for item in plan)
        return plan, skipped, bad, size

    plan, skipped, bad, size = await hass.async_add_executor_job(_prepare)
    if not plan:
        raise _invalid("no_sound_files", path=shown or given)
    if bad:
        raise _invalid("wav_format", count=len(bad), files="; ".join(bad[:5]))
    summary: dict[str, Any] = {
        "font": font,
        "files": [
            {"source": str(item.source.relative_to(folder)), "target": item.target_name}
            for item in plan
        ],
        "skipped": skipped,
        "bytes": size,
    }
    if call.data[ATTR_DRY_RUN]:
        return summary

    await _require_blade_off(saber)
    before = await _on_saber(saber.list_fonts())
    created = font not in before
    await _existing_font(saber, font, create=True)
    old = await _on_saber(saber.list_sounds(font)) if call.data[ATTR_REPLACE] else {}
    if call.data[ATTR_DELETE_FIRST]:
        # Frees the old font's space first; only its exported copy remains.
        for item in _font_files(await _on_saber(saber.list_sounds(font))):
            await _on_saber(saber.file_command(p.FILE_RM, f"/{font}/{item.target_name}"))
        old = {}
    await _require_space(saber, size)
    _LOGGER.info(
        "Uploading %d sound files (%d kB) to font %d of %s; this takes a while",
        len(plan),
        size // 1024,
        font,
        saber.name,
    )
    with saber.batch_progress(len(plan)):
        for item in plan:
            data = await _read(hass, item.source)
            await _on_saber(saber.send_file(f"/{font}/{item.target_name}", data))
            saber.batch_step()
    deleted = []
    for name in soundfont.leftovers(old, plan):
        path = f"/{font}/{name}"
        if await _on_saber(saber.file_command(p.FILE_RM, path)):
            deleted.append(name)
    if ATTR_NAME in call.data:
        await _on_saber(saber.file_command(p.FILE_SETNAME, [f"/{font}", call.data[ATTR_NAME]]))
        summary["name"] = call.data[ATTR_NAME]
    summary["deleted"] = deleted
    summary["created"] = created
    return summary


def _font_files(ranges: dict[str, tuple[int, int]]) -> list[soundfont.PlannedFile]:
    return [
        soundfont.PlannedFile(Path(p.sound_file_name(sound_type, i)), sound_type, i)
        for sound_type, (low, high) in ranges.items()
        for i in range(low, high + 1)
    ]


async def _export_sound_font(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    font: int = call.data[ATTR_FONT]
    destination = await _local_path(hass, call.data[ATTR_DESTINATION], kind="new")
    if not call.data[ATTR_OVERWRITE] and await hass.async_add_executor_job(destination.exists):
        raise _invalid("file_exists", path=destination)
    await _require_blade_off(saber)
    fonts = await _existing_font(saber, font)
    files = _font_files(await _on_saber(saber.list_sounds(font)))
    if not files:
        raise _invalid("font_empty", font=font)
    _LOGGER.info("Exporting %d sound files of font %d from %s", len(files), font, saber.name)
    # Written file by file to "<name>.part", renamed once complete.
    partial = destination.with_name(destination.name + ".part")
    archive = await hass.async_add_executor_job(
        lambda: zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED)
    )
    total = 0
    try:
        with saber.batch_progress(len(files)):
            for item in files:
                data = await _on_saber(saber.receive_file(f"/{font}/{item.target_name}"))
                await hass.async_add_executor_job(archive.writestr, item.target_name, data)
                total += len(data)
                saber.batch_step()
        await hass.async_add_executor_job(archive.close)
        await hass.async_add_executor_job(partial.replace, destination)
    except BaseException:
        await hass.async_add_executor_job(archive.close)
        await hass.async_add_executor_job(lambda: partial.unlink(missing_ok=True))
        raise
    return {
        "font": font,
        "name": fonts[font],
        "path": str(destination),
        "files": len(files),
        "bytes": total,
    }


# Saber paths of the settings files, and where a backup stores them (the layout
# of a Xeno SD card, so a backup can also be copied onto a card).
_SETTINGS_FILES = (("config.ini", "setting/config.ini"),)
_CUSTOM_FUNCTION = ("/setting/customfunction.diy", "setting/customfunction.diy")
BACKUP_MANIFEST = soundfont.BACKUP_MANIFEST
# Give up on the optional system sounds after this many missing in a row: the
# firmware may not have them, or may not let them be read.
SYSTEM_SOUNDS_MAX_MISSES = 5


def _custom_function_installed(saber: XenoSaber) -> bool:
    file_id = saber.state.get("FileID")
    return isinstance(file_id, list) and len(file_id) > 1 and bool(file_id[1])


async def _backup_saber(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    destination = await _local_path(hass, call.data[ATTR_DESTINATION], kind="new")
    if not call.data[ATTR_OVERWRITE] and await hass.async_add_executor_job(destination.exists):
        raise _invalid("file_exists", path=destination)
    await _require_blade_off(saber)
    fonts = await _on_saber(saber.list_fonts())
    wanted: list[int] = call.data.get(ATTR_FONTS) or sorted(fonts)
    for font in wanted:
        if font not in fonts:
            raise _invalid(
                "font_not_found",
                font=font,
                available=", ".join(str(n) for n in sorted(fonts)) or "-",
                next=_next_font(fonts),
            )
    used, total_mb = await _on_saber(saber.storage())

    # (saber path, path in the zip, required)
    plan: list[tuple[str, str, bool]] = [(src, dst, True) for src, dst in _SETTINGS_FILES]
    if _custom_function_installed(saber):
        plan.append((*_CUSTOM_FUNCTION, True))
    if call.data[ATTR_SYSTEM_SOUNDS]:
        plan += [(f"/setting/{n}", f"setting/{n}", False) for n in soundfont.SYSTEM_SOUNDS]
    for font in wanted:
        for item in _font_files(await _on_saber(saber.list_sounds(font))):
            plan.append((f"/{font}/{item.target_name}", f"{font}/{item.target_name}", True))

    _LOGGER.info(
        "Backing up %s: %d files (about %d MB on the saber); this can take hours",
        saber.name,
        len(plan),
        used,
    )
    started = time.monotonic()
    partial = destination.with_name(destination.name + ".part")
    archive = await hass.async_add_executor_job(
        lambda: zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED)
    )
    stored: list[dict[str, Any]] = []
    missing: list[str] = []
    system_found = system_misses = 0
    dead_series: set[str] = set()  # numbered system sounds stop at the first gap
    skipped_system = False
    try:
        with saber.batch_progress(len(plan)):
            for source, name, required in plan:
                series = _series(name)
                if not required and (skipped_system or series in dead_series):
                    saber.batch_step()
                    continue
                try:
                    data = await saber.receive_file(source, attempts=3 if required else 1)
                except XenoSaberError as err:
                    if required:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="command_failed",
                            translation_placeholders={"error": str(err)},
                        ) from err
                    missing.append(name)
                    system_misses += 1
                    if series:
                        dead_series.add(series)
                    if not system_found and system_misses >= SYSTEM_SOUNDS_MAX_MISSES:
                        skipped_system = True  # this firmware does not expose them
                    saber.batch_step()
                    continue
                if not required:
                    system_found += 1
                await hass.async_add_executor_job(archive.writestr, name, data)
                stored.append({"path": name, "bytes": len(data)})
                saber.batch_step()
            manifest = {
                "format": 1,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "saber": {
                    "name": saber.name,
                    "hardware_version": saber.state.get(p.KEY_HW_VERSION),
                    "software_version": saber.state.get(p.KEY_SW_VERSION),
                    "storage_mb": {"used": used, "total": total_mb},
                },
                "fonts": {str(n): fonts.get(n) for n in wanted},
                "settings": {
                    k: v
                    for k, v in saber.state.items()
                    if k not in (p.AUTHORIZE_KEY, p.KEY_BATTERY)
                },
                "files": stored,
                "missing": missing,
                "system_sounds_skipped": skipped_system,
            }
            await hass.async_add_executor_job(
                archive.writestr,
                BACKUP_MANIFEST,
                json.dumps(manifest, indent=2, ensure_ascii=False),
            )
        await hass.async_add_executor_job(archive.close)
        await hass.async_add_executor_job(partial.replace, destination)
    except BaseException:
        await hass.async_add_executor_job(archive.close)
        await hass.async_add_executor_job(lambda: partial.unlink(missing_ok=True))
        raise
    return {
        "path": str(destination),
        "files": len(stored),
        "bytes": sum(f["bytes"] for f in stored),
        "fonts": wanted,
        "missing": len(missing),
        "system_sounds_skipped": skipped_system,
        "minutes": round((time.monotonic() - started) / 60, 1),
    }


async def _restore_backup(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    source = await _local_path(hass, call.data[ATTR_SOURCE], kind="file")
    try:
        index = await hass.async_add_executor_job(soundfont.read_backup, source)
    except soundfont.FontArchiveError as err:
        raise _invalid("bad_archive", path=source, error=err) from err

    wanted: list[int] = sorted(call.data.get(ATTR_FONTS) or index.fonts)
    for font in wanted:
        if font not in index.fonts:
            raise _invalid(
                "font_not_in_backup",
                font=font,
                available=", ".join(str(n) for n in sorted(index.fonts)) or "-",
            )
    # Settings and system sounds are only written by the settings_writes
    # experimental build; the standard one restores the fonts.
    settings: list[str] = []
    system: list[str] = []
    if _settings_writes is not None:
        settings, system = _settings_writes.plan(call, index, saber)
    backup_saber = index.manifest.get("saber")

    # Fonts missing on the saber are created in order, like the app does.
    fonts = await _on_saber(saber.list_fonts())
    existing = set(fonts)
    created: list[int] = []
    for font in wanted:
        if font not in existing:
            if font != _next_font({n: None for n in existing}):
                raise _invalid(
                    "font_not_found",
                    font=font,
                    available=", ".join(str(n) for n in sorted(existing)) or "-",
                    next=_next_font({n: None for n in existing}),
                )
            existing.add(font)
            created.append(font)
    manifest_names = index.manifest.get("fonts")
    font_bytes = {
        font: sum(index.sizes[f"{font}/{n}"] for n in index.fonts[font]) for font in wanted
    }
    font_names: dict[int, str | None] = {}
    for font in wanted:
        named = manifest_names.get(str(font)) if isinstance(manifest_names, dict) else None
        font_names[font] = named[:16] if isinstance(named, str) and named else None
    plan: dict[str, Any] = {
        "fonts": {
            str(font): {
                "files": len(index.fonts[font]),
                "bytes": font_bytes[font],
                "created": font in created,
                "name": font_names[font],
            }
            for font in wanted
        },
        "settings": settings,
        "system_sounds": len(system),
        "not_restored": {
            "settings": [n for n in index.settings if n not in settings],
            "system_sounds": len(index.system) - len(system),
        },
        "ignored": index.ignored,
        "backup_software_version": (
            backup_saber.get("software_version") if isinstance(backup_saber, dict) else None
        ),
    }
    if call.data[ATTR_DRY_RUN]:
        return plan
    _require_confirm(call)
    await _require_blade_off(saber)

    archive = await hass.async_add_executor_job(zipfile.ZipFile, source)
    total = len(system) + len(settings) + sum(len(index.fonts[f]) for f in wanted)
    _LOGGER.warning(
        "Restoring %s onto %s: %d fonts, %d settings files, %d system sounds (%d files)",
        source.name,
        saber.name,
        len(wanted),
        len(settings),
        len(system),
        total,
    )
    try:
        with saber.batch_progress(total):
            for font in wanted:
                if font in created:
                    await _on_saber(saber.file_command(p.FILE_SETNAME, [f"/{font}", str(font)]))
                else:
                    # The backup replaces the font: free its space first.
                    for item in _font_files(await _on_saber(saber.list_sounds(font))):
                        await _on_saber(
                            saber.file_command(p.FILE_RM, f"/{font}/{item.target_name}")
                        )
                await _require_space(saber, font_bytes[font])
                for file_name in index.fonts[font]:
                    data = await hass.async_add_executor_job(archive.read, f"{font}/{file_name}")
                    await _on_saber(saber.send_file(f"/{font}/{file_name}", data))
                    saber.batch_step()
                font_name = font_names[font]
                if font_name:
                    await _on_saber(saber.file_command(p.FILE_SETNAME, [f"/{font}", font_name]))
            if _settings_writes is not None:
                await _settings_writes.write(hass, saber, archive, index, settings, system)
    finally:
        await hass.async_add_executor_job(archive.close)
    return {**plan, "restored": True}


def _series(name: str) -> str | None:
    """'setting/blade (12).wav' -> 'blade'; None for unnumbered files."""
    match = re.fullmatch(r"setting/(.+) \(\d+\)\.wav", name)
    return match.group(1) if match else None


# --------------------------------------------------------------- registration
Handler = Callable[[HomeAssistant, ServiceCall], Awaitable[ServiceResponse]]

HANDLERS: dict[str, tuple[Handler, SupportsResponse]] = {
    SERVICE_UPLOAD_FILE: (_upload_file, SupportsResponse.OPTIONAL),
    SERVICE_DOWNLOAD_FILE: (_download_file, SupportsResponse.OPTIONAL),
    SERVICE_UPLOAD_SOUND_FONT: (_upload_sound_font, SupportsResponse.OPTIONAL),
    SERVICE_EXPORT_SOUND_FONT: (_export_sound_font, SupportsResponse.OPTIONAL),
    SERVICE_BACKUP: (_backup_saber, SupportsResponse.OPTIONAL),
    SERVICE_RESTORE: (_restore_backup, SupportsResponse.OPTIONAL),
    SERVICE_LIST_SOUND_FONTS: (_list_sound_fonts, SupportsResponse.ONLY),
    SERVICE_LIST_SOUND_FILES: (_list_sound_files, SupportsResponse.ONLY),
    SERVICE_RENAME_SOUND_FONT: (_rename_sound_font, SupportsResponse.OPTIONAL),
    SERVICE_DELETE_FILE: (_delete_file, SupportsResponse.OPTIONAL),
    SERVICE_COPY_FILE: (_copy_file, SupportsResponse.OPTIONAL),
    SERVICE_PLAY_SOUND: (_play_sound, SupportsResponse.NONE),
}

# Open to every user: they only read the saber or play a sound. Everything
# else reads or writes files on the Home Assistant host, changes the saber's
# storage, firmware or board configuration, and needs an administrator.
PUBLIC_SERVICES = frozenset(
    {SERVICE_LIST_SOUND_FONTS, SERVICE_LIST_SOUND_FILES, SERVICE_PLAY_SOUND}
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions (once, not per saber)."""
    handlers = dict(HANDLERS)
    schemas = dict(SCHEMAS)
    for module in _EXPERIMENTAL:  # experimental build: untested on a real saber
        _LOGGER.warning("Experimental feature enabled: %s", module.FEATURE)
        handlers.update(module.HANDLERS)
        schemas.update(module.SCHEMAS)
    for name, (handler, supports_response) in handlers.items():

        async def _call(call: ServiceCall, handler: Handler = handler) -> ServiceResponse:
            return await handler(hass, call)

        if name in PUBLIC_SERVICES:
            hass.services.async_register(
                DOMAIN, name, _call, schema=schemas[name], supports_response=supports_response
            )
        else:
            async_register_admin_service(
                hass, DOMAIN, name, _call, schema=schemas[name], supports_response=supports_response
            )


def _experimental(name: str) -> ModuleType | None:
    """A module only packages built with ``script/build --experimental`` contain.

    Imported last, once everything it may use from this module is defined.
    """
    module = f"{__package__}.{name}"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as err:
        if err.name != module:
            raise
        return None


_board = _experimental("board")  # firmware and board configuration
_settings_writes = _experimental("settings_writes")  # settings files, copy_sound_font
_EXPERIMENTAL = [m for m in (_board, _settings_writes) if m is not None]
