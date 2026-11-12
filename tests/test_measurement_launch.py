import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.measurement_tools import load

evidence = load("measurement_evidence")
launch = load("measurement_launch")
versions = load("measurement_version")

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_tool_inventory_contains_every_runtime_adapter_and_scanner():
    inventory = launch.tool_inventory()
    assert inventory["files"]["scripts/measurement_fixture.py"]
    assert inventory["files"]["scripts/measurement_trial.py"]
    assert inventory["files"][".pre-commit-config.yaml"]
    assert len(inventory["sha256"]) == 64
    assert len(inventory["files"]) == len(launch.TOOL_FILES)


def test_real_fresh_process_four_arms_preserve_samples_and_production_identity():
    parent = Path(tempfile.mkdtemp(prefix="cl-")).resolve()
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    values = []
    for arm, mode in (
        ("baseline", "legacy"),
        ("none", "candidate"),
        ("original", "original"),
        ("candidate", "candidate"),
    ):
        version = parent / (arm + "-product")
        inventory = versions.prepare(ROOT, version, mode=mode, candidate=candidate)
        output = parent / arm
        child = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/measurement_launch.py"),
                "--version-directory",
                str(version),
                "--output",
                str(output),
                "--arm",
                arm,
                "--scale",
                "small_text",
            ],
            cwd=version / "source",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        (parent / (arm + "-diagnostic.log")).write_bytes(child.stdout)
        assert child.returncode == 0, child.stdout.decode("utf-8", errors="replace")
        before = json.loads((parent / (arm + "-before.json")).read_bytes())
        expected = before["expected"]
        contents = {name: (output / "raw" / name).read_bytes() for name in evidence.FILES}
        evidence.validate_files(contents, expected)
        actual = json.loads(contents["run-manifest.json"])
        assert actual["source_commit"] == inventory["source_commit"]
        assert actual["candidate"] == candidate
        assert actual["tool_sha256"] == launch.tool_inventory()["sha256"]
        rows = [json.loads(row) for row in contents["requests.jsonl"].splitlines()]
        business = [row for row in rows if row["kind"] == "business"]
        assert len(business) == expected["load"]["request_count"]
        assert all(row["outcome"] == "ok" for row in business)
        resources = [json.loads(row) for row in contents["resources.jsonl"].splitlines()]
        if arm == "candidate":
            assert any(row["logical_io_bytes"] > 0 for row in resources)
        else:
            assert all(row["logical_io_bytes"] is None for row in resources)
        if arm in {"baseline", "none"}:
            assert all(row["backup_phase"] == "not_applicable" for row in resources)
            assert all(row["backup_bytes"] == 0 for row in resources)
        backups = [row for row in rows if row["kind"] == "backup"]
        if arm in {"original", "candidate"}:
            assert len(backups) == 1 and backups[0]["outcome"] == "ok"
            assert actual["metrics"]["backup_bytes"] > 0
            journal = json.loads(
                (output / "home/supervisor/backup-operations" / ("e" * 32 + ".json")).read_bytes()
            )
            assert journal["phase"] == "committed"
            assert journal["result"]["creation"] == "complete"
        else:
            assert not backups and actual["metrics"]["backup_bytes"] == 0
        # A tiny functional fixture may exceed its preset observation window;
        # retain that failure rather than asserting CI throughput or retiming it.
        assert actual["result"] in {"pass", "fail"}
        values.append(actual["metrics"]["logical"])
        versions.verify(version)
    assert all(value == values[0] for value in values)
