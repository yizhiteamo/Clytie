"""Handwritten fictional raw files through the existing streaming Index."""

import copy
import hashlib
import sqlite3

import pytest
from tests.measurement_mechanism_fixtures import (
    OTHER,
    B,
    H,
    analysis,
    draft,
    evidence,
    full_rows,
    manifest,
    write_source,
)
from tests.measurement_tools import load


@pytest.fixture(scope="session")
def wal_module():
    return load("measurement_wal_validation")


def state(generation, size, additional, page=4096):
    return dict(
        generation_hash=generation,
        physical_bytes=size,
        additional_bytes=additional,
        page_size=page if generation is not None else None,
    )


def history(states=None, *, wal=B + 500, offset=0):
    if states is None:
        states = [
            state(H, 128, 0),
            state(H, 192, 64),
            state(OTHER, 96, 160),
            state(OTHER, 64, 224),
            state(OTHER, 80, 240),
            state(OTHER, 80, 240),
        ]
    return [
        dict(
            id=B + 1000 + offset + n,
            process_sequence=0,
            phase="wal_growth",
            at_s=1.5 + offset + n,
            started_s=1 + offset + n,
            wal_id=wal,
            event="sampled" if n else "initialized",
            before=copy.deepcopy(states[n - 1]) if n else None,
            after=copy.deepcopy(after),
            returned_additional_bytes=after["additional_bytes"] if n else None,
            outcome="ok",
        )
        for n, after in enumerate(states)
    ]


def evaluate(tmp_path, module, growth, *, source_result="cancelled"):
    run = manifest()
    if source_result == "fail":
        run["result"] = "fail"
    rows = full_rows()
    rows["storage-phases.jsonl"].extend(growth)
    source = tmp_path / "source"
    write_source(source, run, rows)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    index = analysis.Index(source, tmp_path / "analysis", copy.deepcopy(run))
    try:
        report = module.WalPredicates(index).report()
    finally:
        index.db.close()
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    assert report["source_result_preserved"] == source_result
    assert report["mechanism_acceptance"] == "not_evaluated"
    assert report["requirements"]["view_limit_decision_and_release"]["status"] == "unverified"
    assert report["requirements"]["wal_sampling_completeness"]["status"] == "unverified"
    return report["requirements"]["observed_wal_growth_arithmetic"]


@pytest.mark.parametrize(
    "states",
    [
        None,
        [state(None, None, 0), state(None, None, 0)],
        [state(None, None, 0), state(H, 32, 32)],
        [state(H, 0, 0, None), state(H, 0, 0, None)],
        [state(H, 32, 0), state(OTHER, 0, 0, None), state(H, 32, 32)],
        [state(H, 32, 0), state(OTHER, 64, 64, 8192)],
    ],
)
def test_complete_legal_retained_state_histories(tmp_path, wal_module, states):
    assert evaluate(tmp_path, wal_module, history(states))["status"] == "verified"


def test_two_wals_and_source_failure_preserved(tmp_path, wal_module):
    rows = history() + history(wal=B + 501, offset=100)
    result = evaluate(tmp_path, wal_module, rows, source_result="fail")
    assert result["status"] == "verified"
    assert result["wal_count"] == 2
    assert result["successful_sample_count"] == 10


@pytest.mark.parametrize(
    "mutation,expected,reason",
    [
        ("empty", "unverified", "wal_observations_missing"),
        ("no_init", "unverified", "wal_initialization_missing"),
        ("no_samples", "unverified", "wal_sample_missing"),
        ("missing_middle", "failed", "wal_state_discontinuous"),
        ("duplicate_init", "failed", "wal_initialization_duplicate_or_late"),
        ("before_present", "failed", "wal_initialization_before_present"),
        ("init_nonzero", "failed", "wal_initialization_nonzero"),
        ("init_return", "failed", "wal_initialization_return_present"),
        ("wrong_growth", "failed", "wal_growth_arithmetic_mismatch"),
        ("wrong_return", "failed", "wal_return_mismatch"),
        ("missing_return", "unverified", "wal_return_missing"),
        ("unknown_state", "unverified", "wal_state_field_unknown"),
        ("missing_before", "unverified", "wal_state_missing"),
        ("missing_after", "unverified", "wal_state_missing"),
        ("absent_contradiction", "failed", "wal_absent_state_contradiction"),
        ("disappear", "failed", "wal_retained_state_disappeared"),
        ("page_changed", "failed", "wal_generation_page_changed"),
        ("wrong_generation", "failed", "wal_growth_arithmetic_mismatch"),
        ("overlap", "failed", "wal_calls_overlap_or_reorder"),
        ("failed", "unverified", "wal_call_not_successful"),
        ("cancelled", "unverified", "wal_call_not_successful"),
        ("unknown", "unverified", "wal_call_not_successful"),
    ],
)
def test_single_variable_mutations(tmp_path, wal_module, mutation, expected, reason):
    rows = history()
    last = rows[-1]
    if mutation == "empty":
        rows = []
    elif mutation == "no_init":
        rows.pop(0)
    elif mutation == "no_samples":
        rows = rows[:1]
    elif mutation == "missing_middle":
        rows.pop(2)
    elif mutation == "duplicate_init":
        extra = copy.deepcopy(rows[0])
        extra.update(id=B + 9000, started_s=1.6, at_s=1.7)
        rows.insert(1, extra)
    elif mutation == "before_present":
        rows[0]["before"] = copy.deepcopy(rows[0]["after"])
    elif mutation == "init_nonzero":
        rows[0]["after"]["additional_bytes"] = 1
    elif mutation == "init_return":
        rows[0]["returned_additional_bytes"] = 0
    elif mutation == "wrong_growth":
        last["after"]["additional_bytes"] += 1
    elif mutation == "wrong_return":
        last["returned_additional_bytes"] += 1
    elif mutation == "missing_return":
        last["returned_additional_bytes"] = None
    elif mutation == "unknown_state":
        last["after"]["additional_bytes"] = None
    elif mutation == "missing_before":
        last["before"] = None
    elif mutation == "missing_after":
        last["after"] = None
    elif mutation == "absent_contradiction":
        last["after"]["generation_hash"] = None
    elif mutation == "disappear":
        last["after"] = state(None, None, 240)
    elif mutation == "page_changed":
        last["after"]["page_size"] = 8192
    elif mutation == "wrong_generation":
        last["after"]["generation_hash"] = H
    elif mutation == "overlap":
        last["started_s"] = rows[-2]["at_s"] - 0.1
    else:
        last.update(outcome=mutation, returned_additional_bytes=None)
    result = evaluate(tmp_path, wal_module, rows)
    assert result["status"] == expected
    assert reason in result["reasons"]


@pytest.mark.parametrize(
    "field", ["wal_id", "id", "process_sequence", "started_s", "returned_additional_bytes"]
)
def test_bool_is_not_number(tmp_path, wal_module, field):
    rows = history()
    rows[-1][field] = True
    with pytest.raises(evidence.EvidenceError):
        evaluate(tmp_path, wal_module, rows)


@pytest.mark.parametrize("field", ["physical_bytes", "additional_bytes", "page_size"])
def test_state_bool_rejected(tmp_path, wal_module, field):
    rows = history()
    rows[-1]["after"][field] = True
    with pytest.raises(evidence.EvidenceError):
        evaluate(tmp_path, wal_module, rows)


@pytest.mark.parametrize(
    "mutation", ["missing_field", "extra_field", "duplicate_id", "failed_with_return"]
)
def test_index_schema_and_duplicate_rejections(tmp_path, wal_module, mutation):
    rows = history()
    if mutation == "missing_field":
        del rows[-1]["after"]["page_size"]
    elif mutation == "extra_field":
        rows[-1]["after"]["extra"] = 0
    elif mutation == "duplicate_id":
        rows.append(copy.deepcopy(rows[-1]))
    else:
        rows[-1]["outcome"] = "failed"
    with pytest.raises((evidence.EvidenceError, sqlite3.IntegrityError)):
        evaluate(tmp_path, wal_module, rows)


def test_missing_does_not_hide_later_contradiction(tmp_path, wal_module):
    rows = history()[1:]
    rows[-1]["returned_additional_bytes"] += 1
    assert evaluate(tmp_path, wal_module, rows)["status"] == "failed"


def test_failed_call_does_not_hide_later_contradiction(tmp_path, wal_module):
    rows = history()
    rows[1].update(outcome="failed", returned_additional_bytes=None)
    rows[-1]["returned_additional_bytes"] += 1
    assert evaluate(tmp_path, wal_module, rows)["status"] == "failed"


def test_unchanged_omission_not_claimed_detectable(tmp_path, wal_module):
    # Removing the final no-change sample leaves valid observed arithmetic; the
    # separate completeness requirement must remain unverified (evaluate checks).
    assert evaluate(tmp_path, wal_module, history()[:-1])["status"] == "verified"


def test_failed_initialization_and_no_success_never_pass(tmp_path, wal_module):
    rows = history()[:1]
    rows[0].update(outcome="failed", after=dict.fromkeys(rows[0]["after"]))
    assert evaluate(tmp_path, wal_module, rows)["status"] == "unverified"


def test_binding_without_growth_is_unverified(tmp_path, wal_module):
    binding = dict(
        id=B + 9000,
        process_sequence=0,
        phase="wal_binding",
        at_s=1,
        wal_id=B + 500,
        snapshot_id=B + 501,
        limits_id=B + 502,
        view_id=2,
        database_kind="memory",
        database_hash=H,
        outcome="ok",
    )
    result = evaluate(tmp_path, wal_module, [binding])
    assert result["status"] == "unverified"
    assert "wal_initialization_missing" in result["reasons"]


def test_earlier_failed_call_not_erased_by_later_success(tmp_path, wal_module):
    rows = history()
    # A real failed sample retains state; a later successful sample may proceed.
    extra = copy.deepcopy(rows[0])
    extra.update(
        id=B + 9000,
        event="sampled",
        started_s=1.6,
        at_s=1.7,
        before=copy.deepcopy(rows[0]["after"]),
        outcome="failed",
    )
    rows.insert(1, extra)
    assert evaluate(tmp_path, wal_module, rows)["status"] == "unverified"


def test_aggregate_only_adds_scoped_subreport(tmp_path):
    run, rows = manifest(), full_rows()
    rows["storage-phases.jsonl"].extend(history())
    source = tmp_path / "source"
    write_source(source, run, rows)
    report = draft.aggregate(source, tmp_path / "analysis", copy.deepcopy(run))
    assert (
        report["wal_growth"]["requirements"]["observed_wal_growth_arithmetic"]["status"]
        == "verified"
    )
    assert report["requirements"]["snapshot_wal_limits"]["status"] == "unverified"
    assert report["mechanism_acceptance"] == "not_evaluated"
    assert report["source_result_preserved"] == "cancelled"
