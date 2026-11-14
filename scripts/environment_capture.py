"""Environment-only manual job entry, disabled pending explicit execution authorization."""

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

# Preparation approval is not execution/upload approval. No workflow activates this.
COLLECTION_AUTHORIZED = False
ROOT = Path(__file__).resolve().parents[1]


def binding(env, product, tool, *, git, system):
    for value in (product, tool):
        evidence._pattern(value, r"[a-f0-9]{40}")
    if (
        env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_ACTIONS") != "true"
        or env.get("GITHUB_REPOSITORY") != "yizhiteamo/Clytie"
        or env.get("GITHUB_RUN_ATTEMPT") != "1"
        or system not in {"Windows", "Linux"}
        or env.get("GITHUB_JOB") != "environment_" + system.lower()
    ):
        raise ValueError("environment_ci_identity_invalid")
    evidence._pattern(env.get("GITHUB_RUN_ID"), r"[1-9][0-9]{0,19}")
    if git("rev-parse", "HEAD").strip() != tool or git("diff", "HEAD", "--name-only").strip():
        raise ValueError("environment_tool_changed")
    if git("cat-file", "-t", product).strip() != "commit":
        raise ValueError("environment_product_missing")
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
    if not COLLECTION_AUTHORIZED:
        raise ValueError("environment_collection_not_authorized")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True)

    expected = binding(os.environ, args.product, args.tool, git=git, system=platform.system())
    # Explicit runtime sources only: no node, hostname, address, user or environment dump.
    runtime = dict(
        environment=launch.environment(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
    )
    recipient = workflow.public_identity(ROOT)["recipient"]
    archive.seal(
        runtime,
        expected,
        Path(os.environ["RUNNER_TEMP"]) / "a43-environment" / expected["platform"],
        recipient,
    )
    # No upload, no output paths for Actions; the proposed archive needs separate approval.
    print("environment_encrypted_pending_verification_not_upload_authorized")


if __name__ == "__main__":
    main()
