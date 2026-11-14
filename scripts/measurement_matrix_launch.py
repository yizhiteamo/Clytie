"""Registered four-arm process driver, without public upload or CI admission.

The outer caller must verify mechanism pass and Q/B admission first. This module
does not turn a pure planner's mechanism-pass hashes into verified evidence.
"""

import argparse
import asyncio
import os
import platform
import subprocess
from pathlib import Path

if __package__:
    from . import measurement_evidence as evidence
    from . import measurement_matrix_plan as matrix
    from . import measurement_mechanism_launch as common
    from . import measurement_plan as plan
    from . import measurement_preparation_generate as preparation
    from . import measurement_preparation_transport as prep_transport
    from . import measurement_transport as transport
    from . import measurement_version as versions
else:
    import measurement_evidence as evidence
    import measurement_matrix_plan as matrix
    import measurement_mechanism_launch as common
    import measurement_plan as plan
    import measurement_preparation_generate as preparation
    import measurement_preparation_transport as prep_transport
    import measurement_transport as transport
    import measurement_version as versions

ROOT = transport.ROOT
require = matrix.require


def verify_materials(frozen, directory):
    """Actual archive bytes for all arms and actual tool bytes at explicit T."""
    inventory = frozen["inputs"]["inventory"]
    require("tool_commit" in inventory, "matrix_explicit_tool_commit_required")
    preparation.current(ROOT, inventory["tool_commit"])
    tools = common.launch.tool_inventory(ROOT)
    require("scripts/measurement_matrix_launch.py" in tools["files"], "matrix_tool_not_inventoried")
    for path, expected in tools["files"].items():
        actual = versions._git(ROOT, "show", inventory["tool_commit"] + ":" + path)
        require(common.prep.digest(actual) == expected, "matrix_tool_commit_mismatch")
    require(
        common.prep.digest(common.prep._read_file(Path(__file__), 512 * 1024))
        == tools["files"]["scripts/measurement_matrix_launch.py"],
        "matrix_running_tool_mismatch",
    )
    sources = {}
    for mode in ("candidate", "legacy", "original"):
        source = Path(directory) / mode
        proof = prep_transport.fixed_source_identity(
            ROOT, source, inventory["candidate"], mode=mode
        )
        sources[mode] = versions.verify(source)
        require(
            proof["source_inventory_sha256"] == plan.digest(sources[mode])
            and proof["tool_sha256"] == tools["sha256"],
            "matrix_material_changed",
        )
    return sources, tools


def checked(frozen, execution_id, directory, *, environ=None):
    env = os.environ if environ is None else environ
    row = matrix.select(frozen, execution_id)
    require(
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_REPOSITORY") == transport.REPOSITORY
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and row["platform"] == platform.system()
        and env.get("GITHUB_JOB") == matrix.jobs(frozen)[matrix.group_key(row)],
        "matrix_workflow_identity_invalid",
    )
    evidence._pattern(env.get("GITHUB_RUN_ID"), r"[1-9][0-9]{0,19}")
    sources, tools = verify_materials(frozen, directory)
    inputs = dict(
        source_inventories=sources,
        tools=tools,
        runtime=preparation.runtime(),
        workflow=dict(run_id=int(env["GITHUB_RUN_ID"]), attempt=1, job_id=env["GITHUB_JOB"]),
    )
    return matrix.prepare_manifest(frozen, execution_id, **inputs), inputs


def claim_started(frozen, execution_id, directory, *, environ=None):
    manifest, inputs = checked(frozen, execution_id, directory, environ=environ)
    root = matrix._registry()
    registration = evidence._json(common.prep._read_file(root / "plan.json", 64 * 1024))
    require(
        registration == matrix._registration(frozen, registration["workflow"])
        and inputs["workflow"]["run_id"] == registration["workflow"]["run_id"],
        "matrix_registration_mismatch",
    )
    expected = dict(
        format=1,
        plan_sha256=frozen["plan_sha256"],
        execution_id=execution_id,
        state="started_no_retry",
        manifest_sha256=plan.digest(manifest),
        workflow=inputs["workflow"],
    )
    started = evidence._json(
        common.prep._read_file(root / (execution_id + ".started.json"), 64 * 1024)
    )
    require(started == expected, "matrix_started_mismatch")
    entered = root / (execution_id + ".entered.json")
    matrix.mechanism._safe_new_path(entered)
    matrix.mechanism._write_exclusive(entered, dict(expected, state="child_entered_no_retry"))
    return manifest


def launch_one(frozen, execution_id, directory, target, *, environ=None, spawn=subprocess.Popen):
    return common._launch_registered(
        frozen,
        execution_id,
        directory,
        target,
        environ=environ,
        spawn=spawn,
        check=checked,
        consume=matrix.register_started,
        root=ROOT,
        child_module="scripts.measurement_matrix_launch",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("child",))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--version-directory", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        frozen = evidence._json(common.prep._read_file(args.plan, 4 * 1024 * 1024))
        if __package__:
            from .measurement_trial import run_matrix
        else:
            from measurement_trial import run_matrix
        actual = asyncio.run(
            run_matrix(args.target, frozen, args.execution_id, args.version_directory)
        )
        print("matrix_driver_returned_acceptance_not_evaluated")
        return (
            0
            if actual["result"] == matrix.select(frozen, args.execution_id)["expected_result"]
            else 1
        )
    except (Exception, asyncio.CancelledError, KeyboardInterrupt):
        print("matrix_driver_failed_no_retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
