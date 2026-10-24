"""BLE protocol of Xenopixel V3 compatible boards: pure encoding/decoding, no Home Assistant imports.

Reverse-engineered from Xeno Configurator 1.5.2 (com.damien.damiensaber).
Messages are plain UTF-8 JSON: ``[type, {key: value}]``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_SERVICE_UUID = "0000dae0-0000-1000-8000-00805f9b34fb"
HANDSHAKE_CHAR_UUID = "0000dae1-0000-1000-8000-00805f9b34fb"
DATA_SERVICE_UUID = "00003ab0-0000-1000-8000-00805f9b34fb"
DATA_CHAR_UUID = "00003ab1-0000-1000-8000-00805f9b34fb"

MSG_WRITE = 2
MSG_STATE = 3
MSG_ERROR = 4

HANDSHAKE_KEY = "HandShake"
HANDSHAKE_VALUE = "HelloDamien"
AUTHORIZE_KEY = "Authorize"
AUTHORIZE_VALUE = "SaberOfDamien"
AUTHORIZE_OK = "AccessAllowed"

# Keys pushed by the saber that are not plain settings.
KEY_POWER_ON = "PowerOn"  # blade lit
KEY_BATTERY = "Power"  # battery percentage, read-only
KEY_COLOR = "BackgroundColor"
KEY_BRIGHTNESS = "Brightness"
KEY_VOLUME = "Volume"
KEY_FONT = "CurrentSoundPackageNo"
KEY_FONT_TOTAL = "TotalSoundPackage"
KEY_FONT_NAME = "fontname"
KEY_EFFECT = "CurrentLightEffect"
KEY_EFFECT_TOTAL = "TotalLightEffect"
KEY_HW_VERSION = "HardwareVersion"
KEY_SW_VERSION = "SoftwareVersion"
KEY_LOCKUP = "Lockup"  # toggled effect, echoed back as [3,{"Lockup":bool}]
KEY_DRAG = "Drag"  # toggled effect, echoed back as [3,{"Drag":bool}]

# Board configuration. Changing either is followed by KEY_RESTART, as the app does.
KEY_SOFTWARE_TYPE = "SoftwareType"  # blade firmware: "Pixel" or "RGB"
KEY_HARDWARE_TYPE = "HardwareType"  # board wiring: "Pixel" or "RGB" (app: needs soldering)
KEY_BUTTON_NUM = "ButtonNum"  # 1 or 2 buttons
KEY_RESTART = "Reset"  # reboots the saber; NOT a factory reset
KEY_SAVE = "SaveCfg"  # writes the current settings to the saber's flash
BLADE_TYPES = ("Pixel", "RGB")

# File-system commands (the saber exposes its SD card). Replies come back as
# state messages with the same key, e.g. [3,{"ls":[1,18]}].
FILE_LS = "ls"  # "/" -> font folder numbers; "/<n>" -> min/max index per sound type
FILE_DF = "df"  # "" -> [used, total]
FILE_FONTNAME = "fontname"  # reply: ["/<n>", "name"]
FILE_SETNAME = "setname"  # ["/<n>", "name"] renames a font
FILE_PLAY = "play"  # "/<n>/<file>.wav" plays it; "null" stops
FILE_CP = "cp"  # ["src", "dst"] -> true
FILE_RM = "rm"  # "path" -> true/false
FILE_TRANSFER = "FileTransfer"  # ["download"|"upload", path], seen from the saber

# Sound types in the order the saber reports their index ranges for `ls "/<n>"`.
SOUND_TYPES = (
    "begindrag",
    "beginlock",
    "beginmelt",
    "beginlightningblock",
    "blaster",
    "clash",
    "drag",
    "enddrag",
    "endlock",
    "endmelt",
    "endlightningblock",
    "font",
    "force",
    "hum",
    "in",
    "lock",
    "lightningblock",
    "melt",
    "out",
    "spin",
    "stab",
    "postoff",
    "preon",
    "sideout",
    "swing",
    "swingh",
    "swingl",
    "track",
)

_MAX_BUFFER = 8192
# Where a message starts: "[2,{", "[3,{" or "[4,{".
_MESSAGE_START = re.compile(r"\[\s*[234]\s*,\s*\{")


def _encode_value(value: Any) -> str:
    """Encode a value the way Type.sendData does (JSON literal)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_value(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"Unsupported value type: {type(value).__name__}")


def encode_command(key: str, value: Any) -> bytes:
    """Build a write message: ``[2,{"Key":value}]``."""
    if not key or '"' in key:
        raise ValueError(f"Invalid key: {key!r}")
    return f'[{MSG_WRITE},{{"{key}":{_encode_value(value)}}}]'.encode()


def handshake_message() -> bytes:
    return encode_command(HANDSHAKE_KEY, HANDSHAKE_VALUE)


def authorize_message() -> bytes:
    return encode_command(AUTHORIZE_KEY, AUTHORIZE_VALUE)


def encode_color(red: int, green: int, blue: int) -> bytes:
    rgb = [max(0, min(255, int(c))) for c in (red, green, blue)]
    return encode_command(KEY_COLOR, rgb)


def parse_color(value: Any) -> tuple[int, int, int] | None:
    """BackgroundColor arrives as [r,g,b] or as the string "[r,g,b]"."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            r, g, b = (max(0, min(255, int(c))) for c in value[:3])
        except (TypeError, ValueError):
            return None
        return r, g, b
    return None


def sound_file_name(sound_type: str, index: int) -> str:
    """Name of a sound file inside a font folder, e.g. ``clash (3).wav``."""
    if sound_type not in SOUND_TYPES:
        raise ValueError(f"Unknown sound type: {sound_type!r}")
    if index < 1:
        raise ValueError(f"Sound index starts at 1, got {index}")
    return f"{sound_type} ({index}).wav"


_SOUND_FILE = re.compile(r"^({}) \((\d+)\)\.wav$".format("|".join(SOUND_TYPES)))


def parse_sound_file_name(name: str) -> tuple[str, int] | None:
    """``'clash (3).wav'`` -> ``('clash', 3)``; None if not a saber sound file name."""
    match = _SOUND_FILE.match(name)
    if match is None or int(match.group(2)) < 1:
        return None
    return match.group(1), int(match.group(2))


def parse_sound_ranges(values: Any) -> dict[str, tuple[int, int]]:
    """Decode the reply to ``ls "/<n>"``: (min, max) index for each sound type.

    The saber sends two numbers per type, in SOUND_TYPES order; (0, 0) means
    the font has no file of that type.
    """
    if not isinstance(values, list) or len(values) != 2 * len(SOUND_TYPES):
        raise ValueError(f"Unexpected sound list: {values!r}")
    ranges: dict[str, tuple[int, int]] = {}
    for i, sound_type in enumerate(SOUND_TYPES):
        low, high = values[2 * i], values[2 * i + 1]
        if not isinstance(low, int) or not isinstance(high, int):
            raise ValueError(f"Unexpected sound list: {values!r}")
        if (low or high) and high >= low:
            ranges[sound_type] = (low, high)
    return ranges


def parse_font_folders(values: Any) -> list[int]:
    """Decode the reply to ``ls "/"``: the saber gives the first and last font folder."""
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(v, int) for v in values):
        raise ValueError(f"Unexpected font list: {values!r}")
    if len(values) < 2:
        return []
    first, last = values[0], values[-1]
    if first <= 0 and last <= 0:
        return []
    return list(range(first, last + 1))


def parse_font_name(value: Any) -> tuple[int, str] | None:
    """Decode ``["/<n>", "name"]`` (fontname and setname replies)."""
    if not isinstance(value, list) or len(value) < 2:
        return None
    folder, name = value[0], value[1]
    try:
        number = int(str(folder).strip("/"))
    except ValueError:
        return None
    return number, str(name)


def parse_message(raw: bytes | bytearray | str) -> tuple[int, dict[str, Any]] | None:
    """Parse one complete message. Returns (type, payload) or None."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    raw = raw.strip().strip("\x00")
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return _validate(data)


def _validate(data: Any) -> tuple[int, dict[str, Any]] | None:
    if (
        isinstance(data, list)
        and len(data) >= 2
        and isinstance(data[0], int)
        and isinstance(data[1], dict)
    ):
        return data[0], data[1]
    return None


class MessageAssembler:
    """Reassemble notifications that may be fragmented or concatenated.

    With a large MTU each notification is normally one message, but a small
    MTU (some proxies) splits long state dumps across several packets.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._decoder = json.JSONDecoder()

    def reset(self) -> None:
        self._buf = ""

    def feed(self, chunk: bytes | bytearray) -> list[tuple[int, dict[str, Any]]]:
        text = bytes(chunk).decode("utf-8", errors="replace").replace("\x00", "")
        if not self._buf:
            # Drop anything before the first message start (binary/YMODEM noise).
            start = text.find("[")
            if start < 0:
                return []
            text = text[start:]
        self._buf += text
        if len(self._buf) > _MAX_BUFFER:
            _LOGGER.debug("Dropping oversized buffer (%d chars)", len(self._buf))
            self._buf = ""
            return []

        messages: list[tuple[int, dict[str, Any]]] = []
        while self._buf:
            self._buf = self._buf.lstrip()
            if not self._buf:
                break
            if not self._buf.startswith("["):
                nxt = self._buf.find("[")
                self._buf = self._buf[nxt:] if nxt >= 0 else ""
                continue
            try:
                obj, end = self._decoder.raw_decode(self._buf)
            except ValueError:
                # Incomplete, or garbage (a lost fragment, binary noise). If a
                # new message starts further on, the start is lost: resync
                # there instead of waiting forever, which would drop every
                # later reply.
                restart = _MESSAGE_START.search(self._buf, 1)
                if restart is None:
                    break  # incomplete, wait for more data
                _LOGGER.debug("Dropping unparsable data: %r", self._buf[: restart.start()][:64])
                self._buf = self._buf[restart.start() :]
                continue
            self._buf = self._buf[end:]
            msg = _validate(obj)
            if msg is not None:
                messages.append(msg)
        return messages
