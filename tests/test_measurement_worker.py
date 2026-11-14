import json
import os
import subprocess
import sys

from tests.test_backup_source import fictional
from tests.test_measurement_evidence import load_tool

from clytie.backup.journal import BackupJournal
from clytie.clock import SystemClock

load_tool("measurement_observation")
load_tool("measurement_recording")
worker = load_tool("measurement_worker")


def test_real_cancelled_worker_keeps_content_free_persistent_phase_evidence(tmp_path):
    home, _ = fictional(tmp_path)
    root = home.parent
    (root / "measurement-fixture.json").write_text(
        json.dumps({"format": 1, "source": "generated_fictional_only"}), encoding="utf-8"
    )
    operation = "a" * 32
    journal, _ = BackupJournal.accept(home, SystemClock(), operation, "create", {})
    env = dict(os.environ)
    env.pop("CLYTIE_HOME", None)
    result = subprocess.run(
        [sys.executable, str(worker.__file__), str(root), operation, "0"],
        input=b"cancel\n",
        capture_output=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert BackupJournal.load(journal.path, SystemClock()).value["phase"] == "cancelled"
    raw = (root / "child-storage-phases.jsonl").read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows and any(row["phase"] == "fsync" for row in rows)
    assert all(
        set(row) == {"id", "request_id", "phase", "started_s", "finished_s", "outcome"}
        for row in rows
    )
    assert str(root).encode() not in raw


def test_worker_observation_refuses_unmarked_directory_before_product_start(tmp_path):
    result = subprocess.run(
        [sys.executable, str(worker.__file__), str(tmp_path), "a" * 32, "0"],
        input=b"",
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "child-storage-phases.jsonl").exists()
