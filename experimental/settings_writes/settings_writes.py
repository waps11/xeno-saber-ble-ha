"""Writes to the saber's settings, and copy_sound_font: EXPERIMENTAL.

Not shipped: none of this has been confirmed on a real saber, and a bad write
to config.ini or the setting/ folder can leave the saber showing "error" at
boot. ``script/build --experimental settings_writes`` copies this file into the
integration as ``settings_writes.py`` and merges its services.yaml and
translations. services.py then lets these through:

- ``upload_file``, ``copy_file`` and ``delete_file`` on ``config.ini`` and
  ``setting/`` (deleting needs ``confirm: true``);
- ``restore_backup`` with ``include_settings`` (config.ini and the custom button
  mapping) and ``include_system_sounds`` (the voice prompts);
- the ``copy_sound_font`` action.
"""

from __future__ import annotations

from typing import Any
import zipfile

try:  # Home Assistant 2026.x replaced voluptuous with probatio
    import probatio as vol
except ImportError:  # older Home Assistant
    import voluptuous as vol  # type: ignore[no-redef]

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from . import protocol as p, soundfont
from .const import DOMAIN
from .device import XenoSaber
from .services import (
    _ENTRY,
    _FONT_NUMBER,
    ATTR_CONFIRM,
    ATTR_NAME,
    ATTR_REPLACE,
    ATTR_SYSTEM_SOUNDS,
    SCHEMAS as STANDARD_SCHEMAS,
    SERVICE_DELETE_FILE,
    SERVICE_RESTORE,
    Handler,
    _existing_font,
    _font_files,
    _font_name,
    _invalid,
    _on_saber,
    _require_blade_off,
    _require_space,
    _saber,
)

FEATURE = "settings_writes (settings files, voice prompts and copy_sound_font)"

ATTR_SETTINGS = "include_settings"
ATTR_SOURCE_FONT = "source_font"
ATTR_DESTINATION_FONT = "destination_font"
SERVICE_COPY_SOUND_FONT = "copy_sound_font"

# Where each settings file of a backup goes on the saber (as the app writes them).
_RESTORE_SETTINGS = {
    "config.ini": "config.ini",
    "customfunction.diy": "/setting/customfunction.diy",
}


def plan(
    call: ServiceCall, index: soundfont.BackupIndex, saber: XenoSaber
) -> tuple[list[str], list[str]]:
    """The settings files and system sounds ``restore_backup`` will write."""
    settings = index.settings if call.data[ATTR_SETTINGS] else []
    system = index.system if call.data[ATTR_SYSTEM_SOUNDS] else []
    backup_saber = index.manifest.get("saber")
    backup_hw = backup_saber.get("hardware_version") if isinstance(backup_saber, dict) else None
    current_hw = saber.state.get(p.KEY_HW_VERSION)
    if settings and backup_hw and current_hw and backup_hw != current_hw:
        # Settings of another board could leave this one misconfigured.
        raise _invalid("backup_other_saber", backup=backup_hw, saber=current_hw)
    return settings, system


async def write(
    hass: HomeAssistant,
    saber: XenoSaber,
    archive: zipfile.ZipFile,
    index: soundfont.BackupIndex,
    settings: list[str],
    system: list[str],
) -> None:
    """Write the system sounds, then the settings (the saber may apply them at once)."""
    if system:
        await _require_space(saber, sum(index.sizes[f"setting/{n}"] for n in system))
    for file_name in system:
        data = await hass.async_add_executor_job(archive.read, f"setting/{file_name}")
        await _on_saber(saber.send_file(f"/setting/{file_name}", data))
        saber.batch_step()
    for file_name in settings:
        data = await hass.async_add_executor_job(archive.read, f"setting/{file_name}")
        target = _RESTORE_SETTINGS[file_name]
        await _on_saber(saber.send_file(target, data, header_name=target))
        saber.batch_step()


async def _copy_sound_font(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    saber = _saber(hass, call)
    source: int = call.data[ATTR_SOURCE_FONT]
    destination: int = call.data[ATTR_DESTINATION_FONT]
    if source == destination:
        raise _invalid("same_font", font=source)
    await _require_blade_off(saber)
    fonts = await _existing_font(saber, source)
    await _existing_font(saber, destination)
    files = _font_files(await _on_saber(saber.list_sounds(source)))
    if not files:
        raise _invalid("font_empty", font=source)
    old = await _on_saber(saber.list_sounds(destination)) if call.data[ATTR_REPLACE] else {}
    with saber.batch_progress(len(files)):
        for item in files:
            src, dst = f"/{source}/{item.target_name}", f"/{destination}/{item.target_name}"
            if not await _on_saber(saber.file_command(p.FILE_CP, [src, dst])):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="saber_refused",
                    translation_placeholders={"operation": f"cp {src} {dst}"},
                )
            saber.batch_step()
    deleted: list[Any] = []
    for leftover in soundfont.leftovers(old, files):
        if await _on_saber(saber.file_command(p.FILE_RM, f"/{destination}/{leftover}")):
            deleted.append(leftover)
    name: str | None = call.data.get(ATTR_NAME) or fonts[source]
    if name:
        await _on_saber(saber.file_command(p.FILE_SETNAME, [f"/{destination}", name]))
    return {
        "source_font": source,
        "destination_font": destination,
        "files": len(files),
        "deleted": deleted,
        "name": name,
    }


SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_RESTORE: STANDARD_SCHEMAS[SERVICE_RESTORE].extend(
        {
            vol.Optional(ATTR_SETTINGS, default=True): cv.boolean,
            vol.Optional(ATTR_SYSTEM_SOUNDS, default=False): cv.boolean,
        }
    ),
    SERVICE_DELETE_FILE: STANDARD_SCHEMAS[SERVICE_DELETE_FILE].extend(
        {vol.Optional(ATTR_CONFIRM, default=False): cv.boolean}
    ),
    SERVICE_COPY_SOUND_FONT: vol.Schema(
        {
            **_ENTRY,
            vol.Required(ATTR_SOURCE_FONT): _FONT_NUMBER,
            vol.Required(ATTR_DESTINATION_FONT): _FONT_NUMBER,
            vol.Optional(ATTR_NAME): _font_name,
            vol.Optional(ATTR_REPLACE, default=True): cv.boolean,
        }
    ),
}

HANDLERS: dict[str, tuple[Handler, SupportsResponse]] = {
    SERVICE_COPY_SOUND_FONT: (_copy_sound_font, SupportsResponse.OPTIONAL),
}
