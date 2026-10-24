"""Unit tests for the protocol encoder/decoder (no BLE, no Home Assistant)."""

import importlib
import json
from pathlib import Path
import sys
import types

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "xeno_saber_ble"
_pkg = types.ModuleType("_xsb_standalone")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules.setdefault("_xsb_standalone", _pkg)
p = importlib.import_module("_xsb_standalone.protocol")


# Byte-exact strings taken from the decompiled app (utils/f.java, vo/Type.java).
def test_handshake_matches_app():
    assert p.handshake_message() == b'[2,{"HandShake":"HelloDamien"}]'


def test_authorize_matches_app():
    assert p.authorize_message() == b'[2,{"Authorize":"SaberOfDamien"}]'


def test_bool_command_matches_type_senddata():
    # Type.PowerOn.write(ctx, Boolean.valueOf(true)) -> "[2,{\"PowerOn\":true}]"
    assert p.encode_command("PowerOn", True) == b'[2,{"PowerOn":true}]'
    assert p.encode_command("PowerOn", False) == b'[2,{"PowerOn":false}]'


def test_int_command():
    assert p.encode_command("Volume", 60) == b'[2,{"Volume":60}]'
    assert p.encode_command("CurrentSoundPackageNo", 0) == b'[2,{"CurrentSoundPackageNo":0}]'


def test_color_is_raw_array_not_quoted():
    # DeviceColorPickerActivity writes "[r,g,b]" with format=false (no quotes)
    assert p.encode_color(255, 0, 128) == b'[2,{"BackgroundColor":[255,0,128]}]'
    assert p.encode_color(300, -5, 10) == b'[2,{"BackgroundColor":[255,0,10]}]'


def test_string_value_is_quoted():
    assert p.encode_command("HardwareType", "X") == b'[2,{"HardwareType":"X"}]'


def test_bool_is_not_encoded_as_int():
    assert p.encode_command("BGMOn", True) != b'[2,{"BGMOn":1}]'


def test_all_encoded_messages_are_valid_json():
    for key, val in [("PowerOn", True), ("Volume", 3), ("BackgroundColor", [1, 2, 3]), ("x", "a\"b")]:
        assert json.loads(p.encode_command(key, val)) == [2, {key: val}]


def test_parse_state_and_error():
    assert p.parse_message(b'[3,{"Power":87,"PowerOn":true}]') == (3, {"Power": 87, "PowerOn": True})
    assert p.parse_message('[4,{"Volume":"out of range"}]') == (4, {"Volume": "out of range"})
    assert p.parse_message(b"\x01\x02garbage") is None
    assert p.parse_message(b'{"not":"array"}') is None


def test_parse_color_both_forms():
    assert p.parse_color([10, 20, 30]) == (10, 20, 30)
    assert p.parse_color("[10,20,30]") == (10, 20, 30)
    assert p.parse_color("nope") is None
    assert p.parse_color(None) is None


def test_assembler_single():
    a = p.MessageAssembler()
    assert a.feed(b'[3,{"PowerOn":true}]') == [(3, {"PowerOn": True})]


def test_assembler_fragmented():
    a = p.MessageAssembler()
    msg = b'[3,{"HardwareVersion":"V3.1","SoftwareVersion":"1.2.3","Power":55,"BackgroundColor":[0,0,255]}]'
    out = []
    for i in range(0, len(msg), 20):  # 23-byte MTU -> 20-byte payloads
        out += a.feed(msg[i : i + 20])
    assert out == [(3, json.loads(msg)[1])]


def test_assembler_concatenated_and_noise():
    a = p.MessageAssembler()
    out = a.feed(b'\x00\x15junk[3,{"Volume":5}][3,{"Power":9}]')
    assert out == [(3, {"Volume": 5}), (3, {"Power": 9})]


def test_assembler_ignores_non_message_json():
    a = p.MessageAssembler()
    assert a.feed(b"[1,2,3]") == []
    assert a.feed(b'[3,{"Volume":1}]') == [(3, {"Volume": 1})]


def test_encode_rejects_bad_keys_and_values():
    import pytest

    with pytest.raises(ValueError):
        p.encode_command("", 1)
    with pytest.raises(ValueError):
        p.encode_command('Bad"Key', 1)
    with pytest.raises(TypeError):
        p.encode_command("Volume", 1.5)


def test_parse_color_rejects_non_numeric():
    assert p.parse_color(["red", 0, 0]) is None
    assert p.parse_color("not json") is None
    assert p.parse_color([1, 2]) is None


def test_assembler_skips_noise_and_bounds_its_buffer():
    a = p.MessageAssembler()
    assert a.feed(b"\x00\x01binary noise") == []  # nothing that starts a message
    assert a.feed(b'[3,{"a":1}]  junk [3,{"b":2}]') == [(3, {"a": 1}), (3, {"b": 2})]
    assert a.feed(b'[3,{"c":') == []  # incomplete: kept for the next chunk
    assert a.feed(b'3}]') == [(3, {"c": 3})]
    assert a.feed(b"[" + b"9" * (p._MAX_BUFFER + 10)) == []  # oversized: dropped
    assert a.feed(b'[3,{"d":4}]') == [(3, {"d": 4})]
    assert a.feed(b'[3,{"e":5}] trailing') == [(3, {"e": 5})]
    assert a.feed(b'[3,{"f":6}]') == [(3, {"f": 6})]


# File-system commands (command/LSFileCommondType.java) and their replies (b6/a.java).
def test_file_commands_match_app():
    assert p.encode_command(p.FILE_LS, "/") == b'[2,{"ls":"/"}]'
    assert p.encode_command(p.FILE_DF, "") == b'[2,{"df":""}]'
    assert p.encode_command(p.FILE_SETNAME, ["/3", "Vader"]) == b'[2,{"setname":["/3","Vader"]}]'
    assert p.encode_command(p.FILE_PLAY, "null") == b'[2,{"play":"null"}]'
    assert p.encode_command(p.FILE_CP, ["/1/a.wav", "/2/a.wav"]) == (
        b'[2,{"cp":["/1/a.wav","/2/a.wav"]}]'
    )


def test_parse_font_folders():
    assert p.parse_font_folders([1, 18]) == list(range(1, 19))
    assert p.parse_font_folders([3]) == []
    assert p.parse_font_folders([0, 0]) == []
    assert p.parse_font_folders(None) == []
    import pytest

    with pytest.raises(ValueError):
        p.parse_font_folders("1,2")


def test_parse_font_name():
    assert p.parse_font_name(["/3", "Obi-Wan"]) == (3, "Obi-Wan")
    assert p.parse_font_name(["three", "x"]) is None
    assert p.parse_font_name("Vader") is None


def test_parse_sound_ranges():
    values = [0, 0] * len(p.SOUND_TYPES)
    values[2 * p.SOUND_TYPES.index("clash") : 2 * p.SOUND_TYPES.index("clash") + 2] = [1, 8]
    values[2 * p.SOUND_TYPES.index("hum") : 2 * p.SOUND_TYPES.index("hum") + 2] = [1, 1]
    assert p.parse_sound_ranges(values) == {"clash": (1, 8), "hum": (1, 1)}
    import pytest

    with pytest.raises(ValueError):
        p.parse_sound_ranges([1, 2])
    with pytest.raises(ValueError):
        p.parse_sound_ranges(["a"] * (2 * len(p.SOUND_TYPES)))


def test_assembler_resyncs_after_lost_fragments():
    a = p.MessageAssembler()
    assert a.feed(b'[3,{"Volume":') == []  # its end is lost
    assert a.feed(b'[3,{"ls":[1,18]}]') == [(3, {"ls": [1, 18]})]
    assert a.feed(b"[\x01\x00\xffbinary") == []
    assert a.feed(b'[3,{"df":[1,2]}][4,{"x":1}]') == [(3, {"df": [1, 2]}), (4, {"x": 1})]
    # A fragmented message is still waited for.
    assert a.feed(b'[3,{"a":[1,') == [] and a.feed(b"2]}]") == [(3, {"a": [1, 2]})]
