"""Translation files must match strings.json and cover every key the code uses."""

import json
from pathlib import Path
import re
import string

import pytest

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "xeno_saber_ble"
SOURCE = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
TRANSLATIONS = sorted((COMPONENT / "translations").glob("*.json"))


def _flatten(data, prefix=""):
    out = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


FLAT_SOURCE = _flatten(SOURCE)


def test_english_matches_source():
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == SOURCE


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.stem)
def test_same_keys_and_placeholders(path):
    flat = _flatten(json.loads(path.read_text(encoding="utf-8")))
    assert set(flat) == set(FLAT_SOURCE), f"{path.name}: key mismatch"
    for key, text in flat.items():
        assert isinstance(text, str) and text.strip(), f"{path.name}: empty {key}"
        assert _placeholders(text) == _placeholders(FLAT_SOURCE[key]), (
            f"{path.name}: placeholders differ in {key}"
        )


def test_language_codes_look_valid():
    for path in TRANSLATIONS:
        assert re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2,4})?", path.stem), path.name


def test_code_keys_exist_in_strings():
    """Every translation_key referenced in the Python code has a string."""
    code = "\n".join(p.read_text(encoding="utf-8") for p in COMPONENT.glob("*.py"))
    used = set(re.findall(r'translation_key\s*=\s*"([a-z0-9_]+)"', code))
    # services.py raises validation errors through a helper that takes the key.
    services = (COMPONENT / "services.py").read_text(encoding="utf-8")
    used |= set(re.findall(r'"([a-z0-9_]+)"', services)) & set(SOURCE["exceptions"])
    entity_keys = {k for platform in SOURCE["entity"].values() for k in platform}
    exception_keys = set(SOURCE["exceptions"])
    missing = used - entity_keys - exception_keys
    assert not missing, f"keys used in code but missing from strings.json: {missing}"
    assert exception_keys <= used, "unused exception strings"
