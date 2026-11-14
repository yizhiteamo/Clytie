"""Prepare a frozen mechanism manifest and explicitly consume local admission.

No CLI, trial import, product activation, geometry generation or execution. The
pure preparer accepts verified inventories; ``verify_materials`` is the explicit
read-only adapter for actual source and tool files. Neither schema validity nor
the local registry grants execution permission. Workflow run/attempt checks and
a manually frozen, durable cross-runner ledger remain the caller's obligation.

The registry has one fixed repository-local root, independent of output paths.
It never deletes a started marker, retries, resumes, or repairs partial writes.
Copying a checkout or replacing a runner is NOT globally prevented by this root.
"""

import copy
import os
from pathlib import Path

try:
    from . import measurement_evidence as evidence
    from . import measurement_launch as launch
    from . import measurement_plan as plan_tools
    from . import measurement_version as version
except ImportError:
    import measurement_evidence as evidence
    import measurement_launch as launch
    import measurement_plan as plan_tools
    import measurement_version as version

ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SEQUENCES = (0, 1)


class MechanismPlanError(ValueError):
    """Fixed diagnostic codes only, without paths or supplied identifiers."""


def _require(condition, code):
    if not condition:
        raise MechanismPlanError(code)


def _select(frozen, execution_id):
    plan_tools.validate_plan(frozen)
    _require(frozen["inputs"]["kind"] == "mechanism", "mechanism_plan_required")
    selected = [row for row in frozen["runs"] if row["execution_id"] == execution_id]
    _require(len(selected) == 1, "mechanism_execution_unknown")
    row = selected[0]
    _require(type(row["attempt"]) is int and row["attempt"] == 1, "mechanism_retry_forbidden")
    return row


def verify_materials(version_directory, tool_root):
    """Read and verify full source/tool inventories, without activating code.

    Call immediately before preparing/consuming admission, and recheck after
    execution in the future outer runner. This is not a filesystem lock.
    """
    return version.verify(version_directory), launch.tool_inventory(tool_root)


def _check_materials(frozen, source_inventory, tools):
    expected = frozen["inputs"]["inventory"]
    _require("source_inventory_sha256" in expected, "mechanism_source_binding_missing")
    evidence._fields(
        source_inventory,
        ("format", "mode", "candidate", "source_commit", "archive_sha256", "files"),
    )
    _require(
        type(source_inventory["format"]) is int
        and source_inventory["format"] == 1
        and source_inventory["mode"] == "candidate"
        and source_inventory["candidate"] == expected["candidate"]
        and source_inventory["source_commit"] == expected["candidate"],
        "mechanism_source_mismatch",
    )
    _require(
        plan_tools.digest(source_inventory) == expected["source_inventory_sha256"],
        "mechanism_source_inventory_mismatch",
    )
    evidence._fields(tools, ("files", "sha256"))
    _require(
        type(tools["files"]) is dict
        and set(tools["files"]) == set(launch.TOOL_FILES)
        and plan_tools.digest(tools["files"]) == tools["sha256"]
        and tools["sha256"] == expected["tool_sha256"],
        "mechanism_tools_mismatch",
    )
    _require(
        tools["files"]["scripts/measurement_fixture.py"] == expected["fixture_sha256"],
        "mechanism_fixture_mismatch",
    )


def prepare_manifest(frozen, execution_id, *, source_inventory, tools, runtime, workflow):
    """Pure plan-to-format-six conversion; no caller-supplied manifest accepted.

    ``runtime`` is exactly {environment, python, sqlite}; its canonical digest
    must match the selected platform's frozen environment hash. Missing other
    platform evidence is never filled here. ``workflow`` is {run_id, job_id,
    attempt}; the caller must authenticate it against the real workflow run.
    No windows, cost profiles, geometry, seeds or warmups are inferred.
    """
    row = _select(frozen, execution_id)
    _check_materials(frozen, source_inventory, tools)
    evidence._fields(runtime, ("environment", "python", "sqlite"))
    evidence._fields(workflow, ("run_id", "job_id", "attempt"))
    _require(
        type(workflow["attempt"]) is int and workflow["attempt"] == 1, "mechanism_retry_forbidden"
    )
    expected = frozen["inputs"]["inventory"]
    _require(
        plan_tools.digest(runtime) == expected["environments"][row["platform"]],
        "mechanism_environment_mismatch",
    )
    return _manifest(frozen, row, runtime=runtime, workflow=workflow, phase="mechanism")


def _manifest(frozen, row, *, runtime, workflow, phase):
    """Shared serialization only; callers must perform their own scope checks."""
    expected = frozen["inputs"]["inventory"]
    execution_id = row["execution_id"]
    metrics = copy.deepcopy(frozen["inputs"]["geometry"][row["platform"]]["scales"][row["scale"]])
    # No backup exists at preparation. Geometry values are frozen inputs, not
    # claims of observations from this execution; result remains incomplete.
    metrics["backup_bytes"] = 0
    manifest = dict(
        format=6,
        source="generated_fictional_only",
        method="legal_storage_v1",
        candidate=expected["candidate"],
        reference=frozen["references"]["original"],
        baseline_reference=frozen["references"]["baseline"],
        source_commit=row["source_commit"],
        **workflow,
        platform=row["platform"],
        seed=frozen["seed"],
        scale=row["scale"],
        group=row["group"],
        arm=row["arm"],
        phase=phase,
        plan_sha256=frozen["plan_sha256"],
        parameters=copy.deepcopy(frozen["parameters"]),
        load=copy.deepcopy(row["window"]["load"]),
        fixture_version=frozen["fixture_version"],
        fixture_sha256=expected["fixture_sha256"],
        tool_sha256=expected["tool_sha256"],
        execution=dict(
            order=copy.deepcopy(row["order"]),
            position=row["position"],
            warmup_requests=frozen["inputs"]["profiles"][row["platform"]][row["scale"]][
                "warmup_requests"
            ],
        ),
        **copy.deepcopy(runtime),
        metrics=metrics,
        result="incomplete",
        observer_sequences=list(OBSERVER_SEQUENCES),
        scenario=row["scenario"],
        execution_id=execution_id,
    )
    evidence.validate_run(manifest)
    return manifest


def _registry():
    # Never take this path from output, manifest, execution_id or CLI arguments.
    target = ROOT / ".cache" / "measurement-mechanism-admission"
    _safe_new_path(target)
    return target


def _safe_new_path(path):
    # The archival helper requires an existing leaf. For a new exclusive file,
    # inspect the nearest existing ancestor (including dangling-link metadata).
    for ancestor in (path, *path.parents):
        try:
            ancestor.lstat()
        except FileNotFoundError:
            continue
        evidence._safe_path(ancestor)
        return
    raise MechanismPlanError("mechanism_registration_path_invalid")


def _write_exclusive(path, value):
    # An interrupted write remains a consumed/blocked record. No cleanup branch.
    with path.open("xb") as output:
        output.write(launch.canonical(value))
        output.flush()
        os.fsync(output.fileno())


def _registration(frozen, workflow_run):
    plan_tools.validate_plan(frozen)
    _require(frozen["inputs"]["kind"] == "mechanism", "mechanism_plan_required")
    _require(
        "source_inventory_sha256" in frozen["inputs"]["inventory"],
        "mechanism_source_binding_missing",
    )
    evidence._fields(workflow_run, ("run_id", "attempt", "jobs"))
    evidence._number(workflow_run["run_id"], integer=True, positive=True)
    _require(
        type(workflow_run["attempt"]) is int and workflow_run["attempt"] == 1,
        "mechanism_retry_forbidden",
    )
    evidence._fields(workflow_run["jobs"], plan_tools.PLATFORMS)
    for job in workflow_run["jobs"].values():
        evidence._pattern(job, r"[a-z][a-z0-9_-]{0,60}")
    return dict(
        format=1,
        scope="repository_local_not_cross_runner_authorization",
        plan_sha256=frozen["plan_sha256"],
        workflow=copy.deepcopy(workflow_run),
    )


def register_plan(frozen, workflow_run):
    """Explicitly bind the repository-local root once to one plan/workflow run.

    An identical binding can be read again; this does not consume a scenario.
    Different plans/runs, damaged records and failed writes are never repaired.
    This does not replace the external frozen ledger on ephemeral CI runners.
    """
    record = _registration(frozen, workflow_run)
    root = _registry()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "plan.json"
    _safe_new_path(path)
    try:
        _write_exclusive(path, record)
    except FileExistsError:
        _require(evidence._json(path.read_bytes()) == record, "mechanism_registration_conflict")
    return copy.deepcopy(record)


def register_started(frozen, execution_id, *, source_inventory, tools, runtime, workflow):
    """Consume one preplanned execution, returning its freshly derived manifest.

    Caller must retain this marker even if subsequent setup/child/output fails.
    There is deliberately no output-directory or arbitrary-manifest argument.
    No execution is invoked here. A process failure after exclusive creation
    cannot make the execution eligible again, including an empty marker.
    """
    manifest = prepare_manifest(
        frozen,
        execution_id,
        source_inventory=source_inventory,
        tools=tools,
        runtime=runtime,
        workflow=workflow,
    )
    root = _registry()
    binding = root / "plan.json"
    evidence._safe_path(binding)
    record = evidence._json(binding.read_bytes())
    expected = _registration(frozen, record["workflow"])
    _require(record == expected, "mechanism_registration_conflict")
    _require(
        workflow
        == dict(
            run_id=record["workflow"]["run_id"],
            attempt=record["workflow"]["attempt"],
            job_id=record["workflow"]["jobs"][manifest["platform"]],
        ),
        "mechanism_workflow_mismatch",
    )
    started = root / (execution_id + ".started.json")
    _safe_new_path(started)
    try:
        _write_exclusive(
            started,
            dict(
                format=1,
                plan_sha256=frozen["plan_sha256"],
                execution_id=execution_id,
                state="started_no_retry",
                manifest_sha256=plan_tools.digest(manifest),
                workflow=copy.deepcopy(workflow),
            ),
        )
    except FileExistsError:
        raise MechanismPlanError("mechanism_execution_already_started") from None
    return manifest
