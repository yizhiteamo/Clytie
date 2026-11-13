"""Tiny handwritten event mutations, never formal measurement."""

import copy
import hashlib
import json

import pytest
from tests.measurement_mechanism_fixtures import (
    OTHER,
    B,
    analysis,
    draft,
    events,
    evidence,
    result,
    small_index,
)


def test_driver_and_close_in_grant_outer_finished_after_release(tmp_path):
    obj = small_index(tmp_path, events())
    assert result(obj, "slot_intervals", obj.slots)[0][0] == "verified"
    assert result(obj, "slot_control_identity", obj.control)[0][0] == "verified"
    assert result(obj, "native_grant_containment", obj.native)[0][0] == "verified"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("missing_close", "unverified"),
        ("duplicate_close", "failed"),
        ("late_driver", "failed"),
        ("late_close", "failed"),
        ("unbound", "unverified"),
        ("disabled", "unverified"),
        ("changed_stage", "failed"),
        ("no_connection", "unverified"),
    ],
)
def test_native_one_variable(tmp_path, mutation, expected):
    rows = events()
    if mutation == "missing_close":
        rows = [r for r in rows if r.get("event") != "connection_closed"]
    elif mutation == "duplicate_close":
        row = copy.deepcopy(next(r for r in rows if r.get("event") == "connection_closed"))
        row["id"] = B + 50
        rows.append(row)
    elif mutation in {"late_driver", "late_close"}:
        target = "driver_finished" if mutation == "late_driver" else "connection_closed"
        next(r for r in rows if r.get("event") == target)["at_s"] = 9
    elif mutation == "changed_stage":
        next(r for r in rows if r.get("event") == "driver_finished")["stage_id"] = OTHER
    elif mutation == "no_connection":
        rows = [
            r for r in rows if r.get("event") not in {"connection_attached", "connection_closed"}
        ]
    else:
        for row in rows:
            if row["phase"] == "native_validation":
                if mutation == "unbound":
                    row.update(stage_id=None, io_scope_state="unbound", stage_thread_matches=None)
                else:
                    row["io_scope_state"] = "disabled"
    obj = small_index(tmp_path, rows)
    obj.slots()
    assert result(obj, "native_grant_containment", obj.native)[0][0] == expected


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("missing_end", "unverified"),
        ("failed_acquire", "failed"),
        ("missing_endpoint", "unverified"),
        ("missing_load", "unverified"),
        ("missing_responsibility", "unverified"),
        ("wrong_controller", "failed"),
        ("call_changed", "failed"),
        ("duplicate_call", "failed"),
        ("missing_session", "unverified"),
    ],
)
def test_control_one_variable(tmp_path, mutation, expected):
    rows = events()
    removes = {
        "missing_end": "request_finished",
        "missing_endpoint": "endpoint_bound",
        "missing_load": "ledger_loaded",
        "missing_responsibility": "responsibility_observed",
        "missing_session": "session_registered",
    }
    if mutation in removes:
        rows = [r for r in rows if r.get("event") != removes[mutation]]
    elif mutation == "failed_acquire":
        next(r for r in rows if r.get("event") == "request_finished")["outcome"] = "failed"
    elif mutation == "call_changed":
        next(r for r in rows if r.get("event") == "request_finished")["session_hash"] = OTHER
    elif mutation == "wrong_controller":
        for row in rows:
            if row.get("event") in {"request_started", "request_finished"}:
                row["controller_hash"] = OTHER
    elif mutation == "duplicate_call":
        row = copy.deepcopy(next(r for r in rows if r.get("event") == "request_started"))
        row["id"] = B + 50
        rows.append(row)
    obj = small_index(tmp_path, rows)
    obj.slots()
    assert any(
        status == expected for status, _ in result(obj, "slot_control_identity", obj.control)
    )


def test_overlapping_other_budget_detected_without_identity_map(tmp_path):
    rows = events()
    for event, at in (("granted", 3), ("released", 4)):
        rows.append(
            dict(
                id=B + len(rows) + 1,
                phase="io_slot",
                event=event,
                at_s=at,
                budget_id=B + 101,
                stage_id=OTHER,
                process_sequence=0,
            )
        )
    obj = small_index(tmp_path, rows)
    assert result(obj, "slot_intervals", obj.slots) == (("failed", "observed_global_slot_overlap"),)


def test_empty_cannot_verify(tmp_path):
    obj = small_index(tmp_path, [])
    for name, method in (
        ("slot_intervals", obj.slots),
        ("slot_control_identity", obj.control),
        ("native_grant_containment", obj.native),
        ("batch_grant_containment", obj.batches),
    ):
        assert result(obj, name, method)[0][0] == "unverified"


@pytest.mark.parametrize("field,value", [("id", True), ("at_s", True), ("budget_id", True)])
def test_bool_not_numeric(tmp_path, field, value):
    rows = events()
    rows[0][field] = value
    with pytest.raises(ValueError):
        small_index(tmp_path, rows)


def test_strict_duplicate_key_and_line_cap(tmp_path):
    raw = tmp_path / "rows.jsonl"
    raw.write_bytes(b'{"a":1,"a":2}\n')
    with pytest.raises(ValueError):
        next(analysis.lines(raw, hashlib.sha256()))
    raw.write_bytes(b"x" * (evidence.MAX_LINE + 1))
    with pytest.raises(draft.Invalid, match="line_limit"):
        next(analysis.lines(raw, hashlib.sha256()))


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("none", "verified"),
        ("missing", "unverified"),
        ("duplicate", "failed"),
        ("failed", "failed"),
        ("arrival", "failed"),
        ("window", "failed"),
    ],
)
def test_complete_request_plan_mutations(tmp_path, mutation, expected):
    obj = small_index(tmp_path, [])
    obj.run = dict(
        format=6,
        arm="candidate",
        result="cancelled",
        scenario="cancel",
        load=dict(request_count=2, interval_seconds=1, window_seconds=2, backup_start_seconds=0),
    )
    rows = [
        dict(id=1, kind="business", scheduled_s=0, started_s=0, finished_s=0.1, outcome="ok"),
        dict(id=2, kind="business", scheduled_s=1, started_s=1, finished_s=1.1, outcome="ok"),
        dict(id=1, kind="backup", scheduled_s=0, started_s=0, finished_s=1.5, outcome="cancelled"),
    ]
    if mutation == "missing":
        rows.pop(1)
    elif mutation == "duplicate":
        rows[1]["id"] = 1
    elif mutation == "failed":
        rows[1]["outcome"] = "failed"
    elif mutation == "arrival":
        rows[1]["scheduled_s"] = 0.5
    elif mutation == "window":
        rows[2]["finished_s"] = 3
    for n, row in enumerate(rows, 1):
        obj.db.execute(
            "INSERT INTO rows(file,n,j) VALUES(?,?,?)", ("requests.jsonl", n, json.dumps(row))
        )
    assert result(obj, "request_plan_window", obj.requests)[0][0] == expected


def test_missing_native_does_not_claim_complete(tmp_path):
    rows = [row for row in events() if row["phase"] != "native_validation"]
    obj = small_index(tmp_path, rows)
    obj.slots()
    assert result(obj, "native_grant_containment", obj.native) == (
        ("unverified", "no_native_events"),
    )
