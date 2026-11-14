"""One approved environment-only dispatch; no product tests or measurements."""

import argparse
import os
import platform
import sqlite3
import subprocess
from pathlib import Path

from . import environment_evidence as archive
from . import measurement_evidence as evidence
from . import measurement_launch as launch
from . import measurement_mechanism_workflow as workflow

PRODUCT = "e4dfacca7e3493918ded15a82239139f8394e763"
PUBLIC_FINGERPRINT = "09d84206c57a4adb3efd462881c0801b7cd9fcadcf103d176ff3b6915fc31eaf"
WORKFLOW = "yizhiteamo/Clytie/.github/workflows/environment.yml@refs/heads/main"
ROOT = Path(__file__).resolve().parents[1]


def binding(env, product, tool, *, git, system):
    for value in (product, tool):
        evidence._pattern(value, r"[a-f0-9]{40}")
    if (
        env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_ACTIONS") != "true"
        or env.get("GITHUB_REPOSITORY") != "yizhiteamo/Clytie"
        or env.get("GITHUB_RUN_ATTEMPT") != "1"
        or env.get("GITHUB_RUN_NUMBER") != "1"
        or env.get("GITHUB_REF") != "refs/heads/main"
        or env.get("GITHUB_WORKFLOW_REF") != WORKFLOW
        or product != PRODUCT
        or system not in {"Windows", "Linux"}
        or env.get("GITHUB_JOB") != "environment_" + system.lower()
    ):
        raise ValueError("environment_ci_identity_invalid")
    evidence._pattern(env.get("GITHUB_RUN_ID"), r"[1-9][0-9]{0,19}")
    if git("rev-parse", "HEAD").strip() != tool or git("diff", "HEAD", "--name-only").strip():
        raise ValueError("environment_tool_changed")
    if git("cat-file", "-t", product).strip() != "commit":
        raise ValueError("environment_product_missing")
    if git(
        "diff",
        "--name-only",
        product,
        tool,
        "--",
        "src",
        "plugins",
        "schemas",
        "pyproject.toml",
        "uv.lock",
    ).strip():
        raise ValueError("environment_product_changed")
    if git("ls-files", "--others", "--exclude-standard", "--", "scripts").strip():
        raise ValueError("environment_untracked_tool")
    return dict(
        product_commit=product,
        tool_commit=tool,
        platform=system,
        run_id=int(env["GITHUB_RUN_ID"]),
        job_id=env["GITHUB_JOB"],
        attempt=1,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True)
    parser.add_argument("--tool", required=True)
    args = parser.parse_args(argv)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True)

    expected = binding(os.environ, args.product, args.tool, git=git, system=platform.system())
    public = workflow.public_identity(ROOT)
    if public["fingerprint"] != PUBLIC_FINGERPRINT:
        raise ValueError("environment_public_identity_changed")
    claim(Path(os.environ["RUNNER_TEMP"]), expected)
    # Explicit runtime sources only: no node, hostname, address, user or environment dump.
    runtime = dict(
        environment=launch.environment(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
    )
    recipient = public["recipient"]
    archive.seal(
        runtime,
        expected,
        Path(os.environ["RUNNER_TEMP"]) / "a43-environment" / expected["platform"],
        recipient,
    )
    # The dedicated workflow uploads exactly the two fixed files, only after success.
    print("environment_encrypted_pending_local_verification")


def claim(temporary, expected):
    """Exclusive per-job claim before probing; never remove failed attempts."""
    root = archive.prep._safe(temporary) / "a43-environment-claims"
    root.mkdir(exist_ok=True)
    archive.prep._safe(root)
    path = root / (expected["job_id"] + ".json")
    archive.prep._save_bytes(path, archive.prep.canonical(expected))


if __name__ == "__main__":
    main()
