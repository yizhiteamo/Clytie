"""The diagnostic must import its plugin before pytest adjusts sys.path."""

import subprocess
import sys
from pathlib import Path

import pytest
from tests.measurement_tools import load

entry = load("startup_diagnostic")


def test_module_entry_is_current_interpreter_and_exact_targets():
    args = entry.command()
    assert args[:3] == [sys.executable, "-m", "pytest"]
    assert args[3:5] == ["-p", "tests.startup_diagnostic_plugin"]
    assert args[-4:] == list(entry.TARGETS)
    assert "--collect-only" not in args
    assert entry.command(collect_only=True).count("--collect-only") == 1


def test_entry_preserves_failure_and_has_no_retry(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 4)

    monkeypatch.chdir(Path(entry.__file__).resolve().parents[1])
    monkeypatch.setattr(entry.subprocess, "run", run)
    assert entry.main([]) == 4
    assert calls == [(entry.command(), {"check": False})]


def test_wrong_working_directory_refused_without_launch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.subprocess, "run", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(SystemExit) as failure:
        entry.main([])
    assert failure.value.code == 2
