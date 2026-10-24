from pathlib import Path
import sys

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]

# A bare ``pytest`` (as in CI) does not put the repository on sys.path.
_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# Recent versions of the test plugin import their own (empty) ``custom_components``
# package first; put this repository's components in front so HA's loader finds
# xeno_saber_ble (and its brand/ folder) where a real install would have it.
import custom_components  # noqa: E402

_REPO_COMPONENTS = str(Path(__file__).resolve().parents[2] / "custom_components")
if _REPO_COMPONENTS not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_COMPONENTS)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
