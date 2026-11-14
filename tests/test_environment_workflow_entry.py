from pathlib import Path

import yaml


def test_environment_entry_is_fixed_manual_once_and_cipher_only():
    root = Path(__file__).resolve().parents[1]
    value = yaml.safe_load((root / ".github/workflows/environment.yml").read_text())
    assert value["on"] == {"workflow_dispatch": None}
    assert value["permissions"] == {"contents": "read"}
    assert value["concurrency"] == {"group": "a43-environment-once", "cancel-in-progress": False}
    assert set(value["jobs"]) == {"environment_windows", "environment_linux"}
    for platform, runner in (("Windows", "windows-latest"), ("Linux", "ubuntu-latest")):
        job = value["jobs"]["environment_" + platform.lower()]
        assert job["runs-on"] == runner
        assert "strategy" not in job
        assert "github.run_number == 1 && github.run_attempt == 1" in job["if"]
        assert "github.ref == 'refs/heads/main'" in job["if"]
        steps = job["steps"]
        assert len(steps) == 5
        assert steps[0]["with"] == {
            "ref": "5025b999b2eb7b0cc444192fb5d1be2adc0e13fb",
            "fetch-depth": 0,
            "persist-credentials": False,
        }
        assert steps[1]["with"] == {"python-version": "3.10"}
        assert steps[2]["run"].splitlines() == [
            "python -m pip install uv==0.9.5",
            "uv sync --locked",
        ]
        assert steps[3]["run"].split() == [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "scripts.environment_capture",
            "--product",
            "e4dfacca7e3493918ded15a82239139f8394e763",
            "--tool",
            "5025b999b2eb7b0cc444192fb5d1be2adc0e13fb",
        ]
        assert "if" not in steps[4]  # Default success-only; no always() upload.
        upload = steps[4]["with"]
        assert upload == {
            "name": "a43-environment-${{ github.run_id }}-" + platform,
            "path": "\n".join(
                "${{ runner.temp }}/a43-environment/" + platform + "/" + name
                for name in ("environment.age", "environment-receipt.json")
            )
            + "\n",
            "retention-days": 14,
            "if-no-files-found": "error",
            "overwrite": False,
        }
