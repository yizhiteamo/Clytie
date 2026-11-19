"""The enforcement checks must detect aliases, not only string spellings."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "architecture_checks", ROOT / "scripts/check_architecture.py"
)
assert SPEC and SPEC.loader
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime as dt\ndt.now()",
        "import time as t\nt.sleep(1)",
        "from asyncio import sleep as pause\nawait pause(1)",
        "from asyncio import wait_for as wait\nawait wait(operation(), 2)",
        "from random import choice as choose\nchoose([1])",
    ],
)
def test_rejects_time_and_random_aliases(source):
    assert CHECKS.inspect_source(source)


@pytest.mark.parametrize(
    "source",
    ["import clytie.config", "from clytie import kernel", "from plugins.other import service"],
)
def test_rejects_plugin_imports_outside_sdk(source):
    assert CHECKS.inspect_source(source, plugin=True)


def test_accepts_sdk_and_injected_dependencies():
    assert (
        CHECKS.inspect_source(
            "import clytie.sdk\nfrom clytie.sdk.types import Actor\n"
            "from random import Random\nrng = Random(0)\n"
            "await ctx.clock.sleep(1)\nctx.random.choice([1])",
            plugin=True,
        )
        == []
    )


def test_project_architecture():
    assert CHECKS.main() == 0


def test_architecture_uses_installed_manifest_ids_and_exact_testing_exemption(
    tmp_path, monkeypatch
):
    manifest = tmp_path / "plugins" / "different-directory" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"id":"fictional-platform"}', encoding="utf-8")
    root = tmp_path / "src" / "clytie"
    exempt = root / "sdk" / "testing" / "fake.py"
    exempt.parent.mkdir(parents=True)
    exempt.write_text('CHANNEL = "fictional-platform"', encoding="utf-8")
    monkeypatch.setattr(CHECKS, "ROOT", tmp_path)
    assert CHECKS.main() == 0
    sibling = root / "sdk" / "testing_helpers.py"
    sibling.write_text('CHANNEL = "fictional-platform"', encoding="utf-8")
    assert CHECKS.main() == 1


def test_system_clock_adapter_is_exact_path_and_exact_api_only():
    source = "from datetime import datetime as dt\nfrom time import monotonic as m\nimport asyncio\ndt.now()\nm()\nasyncio.sleep(1)"
    assert CHECKS.inspect_source(source, relative_path="src/clytie/clock.py") == []
    for path in ("src/clytie/memory/clock.py", "plugins/x/clock.py"):
        assert len(CHECKS.inspect_source(source, relative_path=path)) == 3
    assert CHECKS.inspect_source("import time\ntime.sleep(1)", relative_path="src/clytie/clock.py")
    assert CHECKS.inspect_source(
        "import random\nrandom.random()", relative_path="src/clytie/clock.py"
    )
    assert CHECKS.inspect_source("from clytie.clock import SystemClock", plugin=True)
