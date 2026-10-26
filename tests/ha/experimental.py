"""Enable experimental features in tests the way script/build adds them."""

from collections.abc import Iterator
import contextlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch

from custom_components.xeno_saber_ble import services

ROOT = Path(__file__).resolve().parents[2] / "experimental"
# feature folder -> (module name, services.py attribute)
FEATURES = {
    "board_actions": ("board", "_board"),
    "settings_writes": ("settings_writes", "_settings_writes"),
}


@contextlib.contextmanager
def enabled(*features: str) -> Iterator[dict[str, ModuleType]]:
    modules: dict[str, ModuleType] = {}
    with contextlib.ExitStack() as stack:
        for feature in features:
            module_name, attribute = FEATURES[feature]
            name = f"custom_components.xeno_saber_ble.{module_name}"
            spec = importlib.util.spec_from_file_location(
                name, ROOT / feature / f"{module_name}.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            stack.callback(sys.modules.pop, name, None)
            spec.loader.exec_module(module)
            stack.enter_context(patch.object(services, attribute, module))
            modules[feature] = module
        stack.enter_context(patch.object(services, "_EXPERIMENTAL", list(modules.values())))
        yield modules
