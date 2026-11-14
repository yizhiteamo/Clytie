import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from tests.measurement_tools import load

ROOT = Path(__file__).resolve().parents[1]
versions = load("measurement_version")


def head():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()


def test_fixed_version_inventory_rejects_modified_and_extra_product_files(tmp_path):
    directory = tmp_path / "old"
    inventory = versions.prepare(ROOT, directory, mode="legacy", candidate=head())
    assert inventory["source_commit"] == versions.LEGACY
    path = directory / "source/src/clytie/__init__.py"
    original = path.read_bytes()
    path.write_bytes(original + b"\n# altered\n")
    with pytest.raises(ValueError, match="source_changed"):
        versions.verify(directory)
    path.write_bytes(original)
    (directory / "source/unexpected.txt").write_text("fictional", encoding="utf-8")
    with pytest.raises(ValueError, match="source_changed"):
        versions.verify(directory)


def test_version_switch_requires_empty_import_context_and_current_candidate(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="candidate_changed"):
        versions.prepare(ROOT, tmp_path / "wrong", mode="legacy", candidate="a" * 40)
    monkeypatch.setitem(sys.modules, "clytie", object())
    with pytest.raises(ValueError, match="fresh_process_required"):
        versions.activate(tmp_path / "absent")


@pytest.mark.parametrize("scale", ["small_text", "large_asset"])
def test_real_isolated_versions_share_legal_fixture_and_production_business_and_backup(scale):
    # Short, fresh paths avoid platform path-length artifacts. Files intentionally
    # remain available for failed-run diagnosis; no bulk cleanup.
    parent = Path(tempfile.mkdtemp(prefix="cv-")).resolve()
    outcomes = []
    for mode in ("legacy", "original", "candidate"):
        directory = parent / mode
        versions.prepare(ROOT, directory, mode=mode, candidate=head())
        output = parent / (mode + "-run")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/measurement_probe.py"),
                "--version-directory",
                str(directory),
                "--output",
                str(output),
                "--scale",
                scale,
            ],
            cwd=directory / "source",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        (parent / (mode + "-diagnostic.log")).write_bytes(result.stdout)
        assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
        row = json.loads((output / "probe-result.json").read_bytes())
        assert row["requests_completed"] == 3
        assert row["owner_exited"] and row["views_released"] and row["processes_exited"]
        assert row["complete_backup"] == (mode != "legacy")
        assert row["process_count"] == (0 if mode == "legacy" else 1)
        assert row["responsibility_verified"] == (mode == "candidate")
        assert row["observed_commits"] >= 3
        assert "benchmark_payload" not in row["metrics"]
        outcomes.append(row)
        versions.verify(directory)
    # Compare logical work, not database byte layouts or observed syscall counts.
    assert outcomes[0]["metrics"]["logical"] == outcomes[1]["metrics"]["logical"]
    assert outcomes[1]["metrics"]["logical"] == outcomes[2]["metrics"]["logical"]
    if scale == "large_asset":
        assert outcomes[0]["metrics"]["logical"]["asset_registrations"] == 1
