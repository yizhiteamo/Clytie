"""Fictional value-level preparation; no formal measurement is executed."""

import copy

import pytest
from tests.measurement_tools import load
from tests.test_measurement_mechanism_plan import materials
from tests.test_measurement_plan import profiles

matrix = load("measurement_matrix_plan")
plan = load("measurement_plan")


def prepared():
    old, source, tools, runtime, workflow = materials()
    inv = old["inputs"]["inventory"]
    gate = {k: inv[k] for k in ("candidate", "fixture_sha256", "tool_sha256")}
    gate.update(plan_sha256="a" * 64, results_sha256="b" * 64)
    frozen = plan.freeze_plan(
        plan.draft_plan(
            "matrix",
            inv,
            old["inputs"]["geometry"],
            profiles=profiles(),
            mechanism_pass=gate,
        )
    )
    sources = {"candidate": source}
    for mode, key in (("legacy", "baseline"), ("original", "original")):
        sources[mode] = dict(source, mode=mode, source_commit=plan.REFERENCES[key])
    return frozen, sources, tools, runtime, workflow


def arguments(data, index):
    frozen, sources, tools, runtime, workflow = data
    row = frozen["runs"][index]
    return dict(
        source_inventories=sources,
        tools=tools,
        runtime=runtime[row["platform"]],
        workflow=dict(
            run_id=workflow["run_id"], attempt=1, job_id=workflow["jobs"][row["platform"]]
        ),
    )


@pytest.mark.parametrize("index", range(96))
def test_every_approved_arm_selects_its_actual_source_and_phase(index):
    data = prepared()
    frozen = data[0]
    row = frozen["runs"][index]
    before = copy.deepcopy(data)
    result = matrix.prepare_manifest(frozen, row["execution_id"], **arguments(data, index))
    assert result["source_commit"] == row["source_commit"]
    assert result["phase"] == ("baseline" if row["arm"] == "baseline" else "matrix")
    assert result["arm"] == row["arm"] and result["result"] == "incomplete"
    assert result["execution"]["order"] == list(plan.ORDERS[row["group"] - 1])
    assert result["load"] == row["window"]["load"]
    assert data == before


def test_group_order_budget_and_same_environment_window():
    frozen = prepared()[0]
    for platform in plan.PLATFORMS:
        rows = [r for r in frozen["runs"] if r["platform"] == platform]
        assert len([r for r in rows if r["arm"] == "baseline"]) == 12
        assert len([r for r in rows if r["arm"] != "baseline"]) == 36
        for scale in plan.SCALES:
            for group in (1, 2, 3):
                selected = matrix.group_rows(frozen, platform, scale, group)
                assert [r["position"] for r in selected] == [0, 1, 2, 3]


@pytest.mark.parametrize("change", ["source", "runtime", "attempt", "missing", "execution"])
def test_changed_binding_refused(change):
    data = prepared()
    frozen = data[0]
    kwargs = arguments(data, 0)
    execution_id = frozen["runs"][0]["execution_id"]
    if change == "source":
        kwargs["source_inventories"]["legacy"]["source_commit"] = "c" * 40
    elif change == "runtime":
        kwargs["runtime"]["environment"]["cpu_count"] = 4
    elif change == "attempt":
        kwargs["workflow"]["attempt"] = 2
    elif change == "missing":
        del kwargs["source_inventories"]["original"]
    else:
        execution_id = "f" * 64
    with pytest.raises(ValueError):
        matrix.prepare_manifest(frozen, execution_id, **kwargs)


def test_unverified_matrix_cannot_be_frozen():
    old = materials()[0]
    draft = plan.draft_plan(
        "matrix", old["inputs"]["inventory"], old["inputs"]["geometry"], profiles=profiles()
    )
    assert "mechanism_pass_evidence_missing" in draft["gaps"]
    with pytest.raises(ValueError):
        plan.freeze_plan(draft)


def registered(tmp_path, monkeypatch):
    data = prepared()
    monkeypatch.setattr(matrix, "ROOT", tmp_path)
    workflow = dict(run_id=123, attempt=1, jobs=matrix.jobs(data[0]))
    matrix.register_plan(data[0], workflow)
    return data, workflow


def test_matrix_registration_has_24_fixed_serial_groups(tmp_path, monkeypatch):
    data, workflow = registered(tmp_path, monkeypatch)
    jobs = matrix.jobs(data[0])
    assert len(jobs) == 24 and len(set(jobs.values())) == 24
    assert list(jobs) == [matrix.group_key(row) for row in data[0]["runs"] if row["position"] == 0]
    assert matrix.register_plan(data[0], workflow)["workflow"] == workflow
    with pytest.raises(ValueError):
        matrix.register_plan(data[0], dict(workflow, run_id=124))


def test_matrix_slot_claim_is_exclusive_and_group_ordered(tmp_path, monkeypatch):
    data, workflow = registered(tmp_path, monkeypatch)
    frozen = data[0]

    def inputs(index):
        kwargs = arguments(data, index)
        kwargs["workflow"] = dict(
            run_id=123, attempt=1, job_id=workflow["jobs"][matrix.group_key(frozen["runs"][index])]
        )
        return kwargs

    with pytest.raises(ValueError, match="earlier_slot_not_complete"):
        matrix.register_started(frozen, frozen["runs"][1]["execution_id"], **inputs(1))
    first = frozen["runs"][0]["execution_id"]
    manifest = matrix.register_started(frozen, first, **inputs(0))
    with pytest.raises(ValueError, match="already_started"):
        matrix.register_started(frozen, first, **inputs(0))
    # Driver success by itself is not a completed encrypted handoff.
    with pytest.raises(ValueError, match="earlier_slot_not_complete"):
        matrix.register_started(frozen, frozen["runs"][1]["execution_id"], **inputs(1))
    assert manifest["job_id"] == workflow["jobs"][matrix.group_key(frozen["runs"][0])]


def test_matrix_registration_rejects_retries_and_changed_jobs(tmp_path, monkeypatch):
    data = prepared()
    monkeypatch.setattr(matrix, "ROOT", tmp_path)
    jobs = matrix.jobs(data[0])
    for workflow in (
        dict(run_id=123, attempt=2, jobs=jobs),
        dict(run_id=123, attempt=1, jobs=dict(jobs, extra="matrix_other")),
        dict(run_id=True, attempt=1, jobs=jobs),
    ):
        with pytest.raises(ValueError):
            matrix.register_plan(data[0], workflow)


@pytest.mark.parametrize("changed", [None, "plan_sha256", "manifest_sha256", "workflow", "state"])
def test_next_arm_requires_exact_previous_handoff(tmp_path, monkeypatch, changed):
    data, workflow = registered(tmp_path, monkeypatch)
    frozen = data[0]
    rows = frozen["runs"]
    kwargs = arguments(data, 0)
    kwargs["workflow"] = dict(
        run_id=123, attempt=1, job_id=workflow["jobs"][matrix.group_key(rows[0])]
    )
    manifest = matrix.register_started(frozen, rows[0]["execution_id"], **kwargs)
    record = dict(
        format=1,
        plan_sha256=frozen["plan_sha256"],
        execution_id=rows[0]["execution_id"],
        manifest_sha256=plan.digest(manifest),
        state="driver_success_encrypted_pending_verification",
        workflow=kwargs["workflow"],
    )
    if changed:
        record[changed] = "fictional_invalid"
    # Synthetic local handoff; this test does not grant production admission.
    matrix.mechanism._write_exclusive(
        matrix._registry() / (rows[0]["execution_id"] + ".completed.json"), record
    )
    if changed:
        with pytest.raises(ValueError, match="earlier_slot_not_complete"):
            matrix.register_started(frozen, rows[1]["execution_id"], **kwargs)
    else:
        next_manifest = matrix.register_started(frozen, rows[1]["execution_id"], **kwargs)
        assert next_manifest["execution"]["position"] == 1
        assert next_manifest["job_id"] == manifest["job_id"]
