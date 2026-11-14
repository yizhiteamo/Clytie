"""Four-arm manifest preparation and local registration; no execution permission.

All source inventories must come from the existing fixed Git-object verifier.
This value-level preparation does not authenticate GitHub, consume a remote
budget, or make a caller-supplied mechanism-pass hash into verified evidence.
"""

import copy
from pathlib import Path

if __package__:
    from . import measurement_evidence as evidence
    from . import measurement_mechanism_plan as mechanism
    from . import measurement_plan as plan
else:
    import measurement_evidence as evidence
    import measurement_mechanism_plan as mechanism
    import measurement_plan as plan

ROOT = Path(__file__).resolve().parents[1]


def require(condition, code):
    if not condition:
        raise evidence.EvidenceError(code)


def select(frozen, execution_id):
    plan.validate_plan(frozen)
    require(frozen["inputs"]["kind"] == "matrix", "matrix_plan_required")
    require(len(frozen["runs"]) == 96, "matrix_budget_changed")
    rows = [row for row in frozen["runs"] if row["execution_id"] == execution_id]
    require(len(rows) == 1, "matrix_execution_unknown")
    return rows[0]


def source_mode(row):
    return {
        "baseline": "legacy",
        "original": "original",
        "none": "candidate",
        "candidate": "candidate",
    }[row["arm"]]


def group_key(row):
    return f"{row['platform'].lower()}_{row['scale']}_g{row['group']}"


def jobs(frozen):
    """Fixed 24 groups in plan order; each group must run its four arms serially."""
    plan.validate_plan(frozen)
    require(frozen["inputs"]["kind"] == "matrix", "matrix_plan_required")
    result = {
        group_key(row): "matrix_" + group_key(row) for row in frozen["runs"] if row["position"] == 0
    }
    require(len(result) == 24, "matrix_group_budget_changed")
    return result


def group_rows(frozen, platform, scale, group):
    """Keep all four arms on one job/environment, in the approved fixed order."""
    plan.validate_plan(frozen)
    require(frozen["inputs"]["kind"] == "matrix", "matrix_plan_required")
    require(platform in plan.PLATFORMS and scale in plan.SCALES, "matrix_group_unknown")
    require(type(group) is int and 1 <= group <= 3, "matrix_group_unknown")
    rows = [
        r
        for r in frozen["runs"]
        if (r["platform"], r["scale"], r["group"]) == (platform, scale, group)
    ]
    require(len(rows) == 4, "matrix_group_incomplete")
    require([r["arm"] for r in rows] == list(plan.ORDERS[group - 1]), "matrix_order_changed")
    require(len({plan.digest(r["window"]) for r in rows}) == 1, "matrix_window_changed")
    return rows


def prepare_manifest(frozen, execution_id, *, source_inventories, tools, runtime, workflow):
    row = select(frozen, execution_id)
    evidence._fields(source_inventories, ("candidate", "legacy", "original"))
    mechanism._check_materials(frozen, source_inventories["candidate"], tools)
    candidate = frozen["inputs"]["inventory"]["candidate"]
    for mode, source in source_inventories.items():
        evidence._fields(
            source, ("format", "mode", "candidate", "source_commit", "archive_sha256", "files")
        )
        expected = {
            "legacy": frozen["references"]["baseline"],
            "original": frozen["references"]["original"],
            "candidate": candidate,
        }[mode]
        require(
            type(source["format"]) is int
            and source["format"] == 1
            and source["mode"] == mode
            and source["candidate"] == candidate
            and source["source_commit"] == expected,
            "matrix_source_mismatch",
        )
        evidence._pattern(source["archive_sha256"], r"[a-f0-9]{64}")
        require(type(source["files"]) is dict and bool(source["files"]), "matrix_source_empty")
    require(
        source_inventories[source_mode(row)]["source_commit"] == row["source_commit"],
        "matrix_source_mismatch",
    )
    evidence._fields(runtime, ("environment", "python", "sqlite"))
    require(
        plan.digest(runtime) == frozen["inputs"]["inventory"]["environments"][row["platform"]],
        "matrix_environment_mismatch",
    )
    evidence._fields(workflow, ("run_id", "job_id", "attempt"))
    require(type(workflow["attempt"]) is int and workflow["attempt"] == 1, "matrix_retry_forbidden")
    return mechanism._manifest(
        frozen,
        row,
        runtime=runtime,
        workflow=workflow,
        phase="baseline" if row["arm"] == "baseline" else "matrix",
    )


def _registry():
    # Output path and caller identifiers cannot select a fresh admission root.
    root = ROOT / ".cache" / "measurement-matrix-admission"
    mechanism._safe_new_path(root)
    return root


def _registration(frozen, workflow):
    evidence._fields(workflow, ("run_id", "attempt", "jobs"))
    evidence._number(workflow["run_id"], integer=True, positive=True)
    require(type(workflow["attempt"]) is int and workflow["attempt"] == 1, "matrix_retry_forbidden")
    require(workflow["jobs"] == jobs(frozen), "matrix_jobs_changed")
    return dict(
        format=1,
        scope="repository_local_not_cross_runner_authorization",
        plan_sha256=frozen["plan_sha256"],
        workflow=copy.deepcopy(workflow),
    )


def register_plan(frozen, workflow):
    """Bind a local checkout once. Q/B remote admission remains mandatory.

    This cannot prevent a new checkout/runner from re-registering. A production
    caller must authenticate the fixed run/attempt and serial jobs before using
    this primitive; it does not verify mechanism-pass evidence or authorize CI.
    """
    record = _registration(frozen, workflow)
    root = _registry()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "plan.json"
    mechanism._safe_new_path(path)
    try:
        mechanism._write_exclusive(path, record)
    except FileExistsError:
        require(evidence._json(path.read_bytes()) == record, "matrix_registration_conflict")
    return copy.deepcopy(record)


def register_started(frozen, execution_id, **inputs):
    """Consume a fixed arm once, after preceding arm's encrypted handoff.

    Completion records are local workflow handoffs, never measurement acceptance
    or authority to upload. Partial records remain consumed; no repair/retry.
    """
    manifest = prepare_manifest(frozen, execution_id, **inputs)
    row = select(frozen, execution_id)
    root = _registry()
    binding = root / "plan.json"
    evidence._safe_path(binding)
    record = evidence._json(binding.read_bytes())
    require(record == _registration(frozen, record["workflow"]), "matrix_registration_conflict")
    require(
        inputs["workflow"]
        == dict(
            run_id=record["workflow"]["run_id"],
            attempt=1,
            job_id=record["workflow"]["jobs"][group_key(row)],
        ),
        "matrix_workflow_mismatch",
    )
    if row["position"]:
        previous = group_rows(frozen, row["platform"], row["scale"], row["group"])[
            row["position"] - 1
        ]
        path = root / (previous["execution_id"] + ".completed.json")
        mechanism._safe_new_path(path)
        require(path.is_file(), "earlier_slot_not_complete")
        expected = prepare_manifest(frozen, previous["execution_id"], **inputs)
        require(
            evidence._json(path.read_bytes())
            == dict(
                format=1,
                plan_sha256=frozen["plan_sha256"],
                execution_id=previous["execution_id"],
                manifest_sha256=plan.digest(expected),
                state="driver_success_encrypted_pending_verification",
                workflow=inputs["workflow"],
            ),
            "earlier_slot_not_complete",
        )
    started = root / (execution_id + ".started.json")
    mechanism._safe_new_path(started)
    try:
        mechanism._write_exclusive(
            started,
            dict(
                format=1,
                plan_sha256=frozen["plan_sha256"],
                execution_id=execution_id,
                state="started_no_retry",
                manifest_sha256=plan.digest(manifest),
                workflow=copy.deepcopy(inputs["workflow"]),
            ),
        )
    except FileExistsError:
        raise evidence.EvidenceError("matrix_execution_already_started") from None
    return manifest
