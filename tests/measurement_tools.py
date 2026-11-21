"""Import a repository script without relying on the pytest entry point."""

import importlib.util
import sys
from pathlib import Path


def load(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    if name in sys.modules:
        existing = sys.modules[name]
        if Path(existing.__file__).resolve() != path.resolve():
            raise ValueError("test_tool_origin_mismatch")
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
