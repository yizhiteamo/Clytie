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
    assert "github.run_number == 2 && github.run_attempt == 1" in job["if"]
    steps = job["steps"]
    assert len(steps) == 5
    assert steps[0]["with"]["ref"] == "23b0c773529c8f0957a0b5c818bfe0cdcf8a42dc"
    assert steps[0]["with"]["persist-credentials"] is False
    assert all("upload-artifact" not in s.get("uses", "") for s in steps)
    assert steps[-1]["run"].split() == [
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "scripts.startup_diagnostic",
    ]
