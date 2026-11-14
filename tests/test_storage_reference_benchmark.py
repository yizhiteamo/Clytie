"""固定旧候选基线在新进程运行，不与当前包混用。"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def test_reference_process_uses_exact_old_tree_and_keeps_measurements(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "reference"
    environment = dict(os.environ, PYTHONUTF8="1", CLYTIE_BENCHMARK_RUNNER_ID="fictional-runner")
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/benchmark_storage_reference.py"),
            "--output",
            str(output),
            "--scales",
            "small_text",
            "--requests",
            "4",
            "--interval",
            "0.01",
            "--repeats",
            "1",
            "--conditions-note",
            "fictional smoke",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    inventory = json.loads((output / "reference-inventory.json").read_text())
    conditions = json.loads((output / "conditions.json").read_text())
    assert conditions["commit"] == inventory["commit"] == "05ec93c7c4ae972b7475911b0aec9db57bb00390"
    assert conditions["runner_id"] == hashlib.sha256(b"fictional-runner").hexdigest()
    assert conditions["method_id"] == "fixed-query-coordinated-meta-fake-model-v1"
    entry = next(row for row in inventory["files"] if row["path"] == "scripts/benchmark_backup.py")
    assert entry["sha256"] == conditions["script_sha256"]
    assert (
        hashlib.sha256((output / "source" / entry["path"]).read_bytes()).hexdigest()
        == entry["sha256"]
    )
    summaries = json.loads((output / "summary.json").read_text())
    assert len(summaries) == 1
    assert summaries[0]["response_ms"]["count"] == 4
    assert summaries[0]["errors"] == []
    raw = json.loads((output / "small_text-baseline-repeat-1/result.json").read_text())
    assert len(raw["samples"]) == 4
    assert not raw["with_backup"]
