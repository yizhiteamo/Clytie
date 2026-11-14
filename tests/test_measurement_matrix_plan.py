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
