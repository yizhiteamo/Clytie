from pathlib import Path

import yaml


def test_fixed_windows_diagnostic_no_upload_no_retry():
    root = Path(__file__).resolve().parents[1]
    value = yaml.safe_load((root / ".github/workflows/startup-diagnostic.yml").read_text())
    assert value["on"] == {"workflow_dispatch": None}
    assert value["permissions"] == {"contents": "read"}
    assert value["concurrency"]["cancel-in-progress"] is False
    assert set(value["jobs"]) == {"windows_startup_diagnostic"}
    job = value["jobs"]["windows_startup_diagnostic"]
    assert job["runs-on"] == "windows-latest"
    assert "strategy" not in job
    assert "github.run_number == 1 && github.run_attempt == 1" in job["if"]
    steps = job["steps"]
    assert len(steps) == 5
    assert steps[0]["with"]["ref"] == "003a001d7078513d360d19e1a400c52d990451e2"
    assert steps[0]["with"]["persist-credentials"] is False
    assert all("upload-artifact" not in s.get("uses", "") for s in steps)
    assert steps[-1]["run"].split() == [
        "uv",
        "run",
        "--no-sync",
        "pytest",
        "-p",
        "tests.startup_diagnostic_plugin",
        "-q",
        "-s",
        "--tb=no",
        "tests/test_backup_maintenance.py::test_real_maintenance_empty_or_invalid_home_never_starts_business",
        "tests/test_api_startup_read.py::test_read_denial_then_readiness_sends_one_ack",
        "tests/test_api_startup_read.py::test_other_read_io_errors_fail_explicitly_without_retry",
        "tests/test_api_startup_read.py::test_real_windows_read_denial_then_release_without_permission_changes",
    ]
