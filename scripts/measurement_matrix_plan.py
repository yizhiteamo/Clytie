"""Pure four-arm manifest preparation. No execution or public-upload permission.

All source inventories must come from the existing fixed Git-object verifier.
This value-level preparation does not authenticate GitHub, consume a remote
budget, or make a caller-supplied mechanism-pass hash into verified evidence.
"""

if __package__:
    from . import measurement_evidence as evidence
    from . import measurement_mechanism_plan as mechanism
    from . import measurement_plan as plan
else:
    import measurement_evidence as evidence
    import measurement_mechanism_plan as mechanism
    import measurement_plan as plan


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
