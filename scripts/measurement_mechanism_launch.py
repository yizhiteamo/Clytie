"""One registered frozen mechanism execution; no loop, retry, upload or global ledger.

The outer operator owns permission and cross-runner admission. Child exit is
driver process evidence only; it never implies all worker/thread exits or pass.
"""

import argparse
import asyncio
import os
import platform
import stat
import subprocess
import sys
from pathlib import Path

try:
    from . import measurement_evidence as evidence
    from . import measurement_launch as launch
    from . import measurement_mechanism_plan as mechanism
    from . import measurement_plan as plan
    from . import measurement_preparation_evidence as prep
    from . import measurement_preparation_generate as preparation
    from . import measurement_preparation_transport as prep_transport
    from . import measurement_transport as transport
except ImportError:
    import measurement_evidence as evidence
    import measurement_launch as launch
    import measurement_mechanism_plan as mechanism
    import measurement_plan as plan
    import measurement_preparation_evidence as prep
    import measurement_preparation_generate as preparation
    import measurement_preparation_transport as prep_transport
    import measurement_transport as transport

ROOT = transport.ROOT
Error = evidence.EvidenceError


def checked(frozen, execution_id, version_directory, *, environ=None):
    """Rebuild from frozen inputs and current materials; never accept a manifest."""
    env = os.environ if environ is None else environ
    row = mechanism._select(frozen, execution_id)
    if (
        env.get("GITHUB_ACTIONS") != "true"
        or env.get("GITHUB_REPOSITORY") != transport.REPOSITORY
        or env.get("GITHUB_RUN_ATTEMPT") != "1"
        or row["platform"] != platform.system()
    ):
        raise Error("mechanism_workflow_identity_invalid")
    evidence._pattern(env.get("GITHUB_RUN_ID"), r"[1-9][0-9]{0,19}")
    evidence._pattern(env.get("GITHUB_JOB"), r"[a-z][a-z0-9_-]{0,60}")
    candidate = frozen["inputs"]["inventory"]["candidate"]
    preparation.current(ROOT, candidate)
    proof = prep_transport.material_identity(ROOT, version_directory, candidate)
    source, tools = mechanism.verify_materials(version_directory, ROOT)
    if (
        "scripts/measurement_mechanism_launch.py" not in launch.TOOL_FILES
        or "scripts/measurement_mechanism_launch.py" not in tools["files"]
        or proof["source_inventory_sha256"] != plan.digest(source)
        or proof["tool_sha256"] != tools["sha256"]
    ):
        raise Error("mechanism_material_changed")
    inputs = dict(
        source_inventory=source,
        tools=tools,
        runtime=preparation.runtime(),
        workflow=dict(run_id=int(env["GITHUB_RUN_ID"]), attempt=1, job_id=env["GITHUB_JOB"]),
    )
    manifest = mechanism.prepare_manifest(frozen, execution_id, **inputs)
    return manifest, inputs


def claim_started(frozen, execution_id, version_directory, *, environ=None):
    """Consume one child entry only after checking the fixed started record.

    Called before product activation or fixture setup. Partial/missing/changed
    records fail closed. This is repository-local, not cross-runner authorization.
    """
    manifest, inputs = checked(frozen, execution_id, version_directory, environ=environ)
    root = mechanism._registry()
    registration = evidence._json(prep._read_file(root / "plan.json", 64 * 1024))
    expected_registration = mechanism._registration(frozen, registration["workflow"])
    workflow = inputs["workflow"]
    if registration != expected_registration or workflow != dict(
        run_id=registration["workflow"]["run_id"],
        attempt=1,
        job_id=registration["workflow"]["jobs"][manifest["platform"]],
    ):
        raise Error("mechanism_registration_mismatch")
    expected = dict(
        format=1,
        plan_sha256=frozen["plan_sha256"],
        execution_id=execution_id,
        state="started_no_retry",
        manifest_sha256=plan.digest(manifest),
        workflow=workflow,
    )
    started = evidence._json(prep._read_file(root / (execution_id + ".started.json"), 64 * 1024))
    if started != expected:
        raise Error("mechanism_started_mismatch")
    entered = root / (execution_id + ".entered.json")
    mechanism._safe_new_path(entered)
    mechanism._write_exclusive(entered, dict(expected, state="child_entered_no_retry"))
    return manifest


def _child_environment(env):
    # No model/platform credential, deployment home, GH token or arbitrary
    # PYTHONPATH is required for a fresh offline product process.
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "GITHUB_ACTIONS",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
    }
    return {
        **{key: value for key, value in env.items() if key.upper() in allowed},
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }


def _raw_state(target, expected):
    """Only identify a handoff candidate; full five-file transport validation is later."""
    try:
        raw = prep._safe(target / "trial" / "raw")
        if {path.name for path in raw.iterdir()} != set(evidence.FILES):
            raise Error("unexpected_file_set")
        for name in evidence.FILES:
            path = prep._safe(raw / name)
            info = path.stat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > evidence.MAX_FILE
            ):
                raise Error("invalid_raw_file")
        actual = evidence._json(prep._read_file(raw / "run-manifest.json", prep.MAX_FILE))
        evidence.validate_run(actual)
        fixed = dict(actual, metrics=expected["metrics"], result=expected["result"])
        if plan.digest(fixed) != plan.digest(expected):
            raise Error("mechanism_raw_identity_mismatch")
        return "present_requires_transport_validation"
    except (ValueError, OSError, KeyError, TypeError):
        return "missing_or_invalid"


def launch_one(
    frozen, execution_id, version_directory, target, *, environ=None, spawn=subprocess.Popen
):
    """One registered spawn, no retry. Root must already register_plan externally."""
    env = os.environ if environ is None else environ
    before, inputs = checked(frozen, execution_id, version_directory, environ=env)
    expected = mechanism.register_started(frozen, execution_id, **inputs)
    if plan.digest(expected) != plan.digest(before):
        raise Error("mechanism_manifest_changed")
    # Everything after the started marker is consumed even on filesystem/spawn failure.
    target = prep._new(target)
    evidence._save(target / "frozen-plan.json", frozen)
    evidence._save(target / "expected.json", expected)
    process = None
    try:
        with (target / "child.log").open("xb") as output:
            process = spawn(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "scripts.measurement_mechanism_launch",
                    "child",
                    "--plan",
                    str(target / "frozen-plan.json"),
                    "--execution-id",
                    execution_id,
                    "--version-directory",
                    str(Path(version_directory).absolute()),
                    "--target",
                    str(target / "trial"),
                ],
                cwd=ROOT,
                env=_child_environment(env),
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            evidence._number(process.pid, integer=True, positive=True)
            evidence._save(
                target / "spawn.json",
                dict(
                    format=1,
                    pid=process.pid,
                    execution_id=execution_id,
                    state="spawned_exit_not_yet_confirmed",
                ),
            )
            code = process.wait()
            if type(code) is not int:
                raise Error("mechanism_exit_code_unconfirmed")
        evidence._save(
            target / "process-state.json",
            dict(
                format=1,
                execution_id=execution_id,
                state="driver_exited",
                driver_exit_confirmed=True,
                exit_code=code,
            ),
        )
    except BaseException:
        # Never signal/kill the process or interpret cancellation as confirmed exit.
        if not (target / "process-state.json").exists():
            evidence._save(
                target / "process-state.json",
                dict(
                    format=1,
                    execution_id=execution_id,
                    state="spawn_unconfirmed" if process is None else "child_exit_unconfirmed",
                    driver_exit_confirmed=False,
                ),
            )
        raise
    try:
        after, _ = checked(frozen, execution_id, version_directory, environ=env)
        if plan.digest(after) != plan.digest(before):
            raise Error("mechanism_material_changed")
    except BaseException:
        evidence._save(target / "materials-after.json", dict(format=1, matched=False))
        raise
    evidence._save(target / "materials-after.json", dict(format=1, matched=True))
    handoff = dict(
        format=1,
        execution_id=execution_id,
        driver_exit_confirmed=True,
        exit_code=code,
        raw_state=_raw_state(target, expected),
        transport_validation="not_run",
        measurement_acceptance="not_evaluated",
        source="trial/raw",
        expected="expected.json",
    )
    evidence._save(target / "handoff.json", handoff)
    return handoff


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("child",))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--version-directory", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        frozen = evidence._json(prep._read_file(args.plan, 4 * 1024 * 1024))
        # The module itself does not activate/import product code. Its dedicated
        # entry claims the marker, activates a verified tree, then runs the trial.
        if __package__:
            from .measurement_trial import run_mechanism
        else:
            from measurement_trial import run_mechanism
        actual = asyncio.run(
            run_mechanism(args.target, frozen, args.execution_id, args.version_directory)
        )
        print("mechanism_driver_returned_acceptance_not_evaluated")
        row = mechanism._select(frozen, args.execution_id)
        return 0 if actual["result"] == row["expected_result"] else 1
    except (Exception, asyncio.CancelledError, KeyboardInterrupt):
        print("mechanism_driver_failed_no_retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
