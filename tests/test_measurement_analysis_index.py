"""Offline metadata-only checks and single-variable corruptions."""

import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.measurement_mechanism_fixtures import (
    B,
    H,
    analysis,
    draft,
    evaluate,
    evidence,
    full_rows,
    manifest,
    write_source,
)


def test_disk_backed_existing_semantics(tmp_path):
    result = evaluate(tmp_path)
    for key in (
        "resource_lifecycles",
        "io_lifecycles",
        "native_lifecycles",
        "planned_cancel_binding",
    ):
        assert result["requirements"][key]["status"] == "verified"
    assert result["mechanism_acceptance"] == "not_evaluated"
    assert result["source_result_preserved"] == "cancelled"
    assert result["requirements"]["wal_associations"]["status"] == "unverified"
    pressure = result["pressure_and_accounting"]
    assert pressure["mechanism_acceptance"] == "not_evaluated"
    assert pressure["native_internal_read_bytes"] is None
    assert pressure["requirements"]["pressure_owner_identity"]["status"] == "unverified"
    assert pressure["requirements"]["token_16_4_mibps_512kib_model"]["status"] == "unverified"


@pytest.mark.parametrize(
    "mutation,requirement,status",
    [
        ("cancel_missing", "planned_cancel_binding", "unverified"),
        ("cancel_extra", "planned_cancel_binding", "failed"),
        ("cancel_wrong_source", "planned_cancel_binding", "failed"),
        ("cancel_committed", "planned_cancel_binding", "failed"),
        ("cancel_rejected", "planned_cancel_binding", "failed"),
        ("release_missing", "resource_lifecycles", "unverified"),
        ("release_duplicate", "resource_lifecycles", "failed"),
        ("release_failed", "resource_lifecycles", "failed"),
        ("io_duplicate_end", "io_lifecycles", "failed"),
        ("io_missing_end", "io_lifecycles", "unverified"),
        ("io_missing_start", "io_lifecycles", "unverified"),
        ("native_missing_close", "native_lifecycles", "unverified"),
        ("native_missing_start", "native_lifecycles", "unverified"),
        ("native_duplicate_close", "native_lifecycles", "failed"),
        ("native_close_after_finish", "native_lifecycles", "failed"),
    ],
)
def test_lifecycle_mutations(tmp_path, mutation, requirement, status):
    rows = full_rows()
    storage = rows["storage-phases.jsonl"]
    if mutation == "cancel_missing":
        storage[:] = [r for r in storage if r.get("event") != "accepted"]
    elif mutation == "cancel_extra":
        row = copy.deepcopy(next(r for r in storage if r.get("event") == "requested"))
        row["id"] = B + 900
        storage.append(row)
    elif mutation == "cancel_wrong_source":
        next(r for r in storage if r.get("event") == "triggered")["source_row_id"] = B + 60
    elif mutation == "cancel_committed":
        next(r for r in storage if r.get("event") == "terminal")["terminal_phase"] = "committed"
    elif mutation == "cancel_rejected":
        row = next(r for r in storage if r.get("event") == "accepted")
        row.update(event="rejected", cancel_requested=False)
    elif mutation == "release_missing":
        rows["release-events.jsonl"].pop()
    elif mutation == "release_duplicate":
        rows["release-events.jsonl"].append(copy.deepcopy(rows["release-events.jsonl"][-1]))
    elif mutation == "release_failed":
        rows["release-events.jsonl"][-1]["event"] = "failed"
    elif mutation in {"io_duplicate_end", "native_duplicate_close"}:
        phase, event = (
            ("file_batch", "finished")
            if mutation.startswith("io")
            else ("native_validation", "connection_closed")
        )
        row = copy.deepcopy(
            next(r for r in storage if r["phase"] == phase and r.get("event") == event)
        )
        row["id"] = B + 900
        storage.append(row)
    elif mutation in {"io_missing_start", "native_missing_start"}:
        phase = "file_batch" if mutation.startswith("io") else "native_validation"
        storage[:] = [
            r for r in storage if not (r["phase"] == phase and r.get("event") == "started")
        ]
    elif mutation == "native_close_after_finish":
        row = next(r for r in storage if r.get("event") == "connection_closed")
        storage.remove(row)
        storage.append(row)
    else:
        phase, event = (
            ("file_batch", "finished")
            if mutation.startswith("io")
            else ("native_validation", "connection_closed")
        )
        storage[:] = [r for r in storage if not (r["phase"] == phase and r.get("event") == event)]
    result = evaluate(tmp_path, rows)
    assert result["requirements"][requirement]["status"] == status


@pytest.mark.parametrize("mutation", ["partial", "tool", "environment", "logical", "bool"])
def test_expected_full_frozen_identity(mutation):
    run, expected = manifest(), manifest()
    if mutation == "partial":
        expected = {"candidate": run["candidate"]}
    elif mutation == "tool":
        expected["tool_sha256"] = "b" * 64
    elif mutation == "environment":
        expected["environment"]["cpu_count"] = 2
    elif mutation == "logical":
        expected["metrics"]["logical"]["asset_bytes"] += 1
    else:
        expected["run_id"] = True
    with pytest.raises(ValueError):
        analysis.identity_matches(run, expected)


def test_only_observed_outcome_physical_metrics_can_differ():
    run, expected = manifest(), manifest()
    expected["result"] = "incomplete"
    expected["metrics"]["backup_bytes"] = 512
    assert analysis.identity_matches(run, expected)


def test_extra_source_rejected(tmp_path):
    source = tmp_path / "source"
    run = manifest()
    write_source(source, run, full_rows())
    (source / "unexpected.txt").write_text("fictional")
    with pytest.raises(draft.Invalid, match="unexpected_file_set"):
        draft.aggregate(source, tmp_path / "analysis", run)


def test_nested_analysis_target_rejected_without_source_mutation(tmp_path):
    source = tmp_path / "source"
    run = manifest()
    write_source(source, run, full_rows())
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    target = source / "new" / "analysis"
    with pytest.raises(draft.Invalid, match="analysis_target_inside_source"):
        draft.aggregate(source, target, run)
    assert not target.parent.exists()
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before


def test_analysis_target_linked_ancestor_rejected_before_mkdir(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run = manifest()
    write_source(source, run, full_rows())
    ancestor = tmp_path / "linked_ancestor"
    ancestor.mkdir()
    target = ancestor / "new" / "analysis"
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    original = Path.lstat

    def flagged(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == ancestor:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", flagged)
    with pytest.raises(evidence.EvidenceError, match="linked_path"):
        draft.aggregate(source, target, run)
    assert not target.parent.exists()
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before


def test_real_hardlink_rejected(tmp_path):
    original, link = tmp_path / "original", tmp_path / "link"
    original.write_text("{}\n")
    try:
        os.link(original, link)
    except PermissionError:
        pytest.skip("sandbox_denied_real_hardlink_creation_original_failure_retained")
    with pytest.raises(draft.Invalid, match="source_changed_or_linked"):
        tuple(analysis.lines(link, hashlib.sha256()))


def test_source_change_during_read_rejected(tmp_path):
    path = tmp_path / "source"
    path.write_text("{}\n")
    with pytest.raises(draft.Invalid, match="source_changed_or_linked"):
        with analysis.safe_stream(path) as stream:
            stream.read()
            with path.open("ab") as append:
                append.write(b"{}\n")


def test_failed_index_closes_connection(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run = manifest()
    write_source(source, run, full_rows())
    connection, original = [], analysis.sqlite3.connect

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        connection.append(db)
        return db

    monkeypatch.setattr(analysis.sqlite3, "connect", connect)
    with pytest.raises(ValueError):
        draft.aggregate(source, tmp_path / "analysis", {"candidate": run["candidate"]})
    with pytest.raises(analysis.sqlite3.ProgrammingError):
        connection[0].execute("SELECT 1")


def test_historical_format_rejected(tmp_path):
    run = manifest()
    run["format"] = 5
    with pytest.raises(draft.Invalid, match="candidate_v6_required"):
        evaluate(tmp_path, run=run)


def test_disk_map_mutations_flush_and_do_not_accumulate(tmp_path):
    db = analysis.sqlite3.connect(tmp_path / "state.sqlite")
    values = analysis.DiskMap(db, "test")
    for n in range(100):
        values[n] = dict(events={"acquired"}, done=False)
        values[n]["events"].add("released")
        values[n]["done"] = True
        values.flush()
        assert not values.cache
    assert all(
        value["done"] and value["events"] == {"acquired", "released"} for value in values.values()
    )
    assert not values.cache
    assert analysis.RequiredKeys({1, 2}).issubset(values)
    db.close()


def test_opened_hardlink_count_rejected(tmp_path, monkeypatch):
    path = tmp_path / "original"
    path.write_text("{}\n")
    original = os.fstat

    def linked(fd):
        info = original(fd)
        return SimpleNamespace(
            **{
                key: getattr(info, key)
                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            },
            st_nlink=2,
        )

    monkeypatch.setattr(os, "fstat", linked)
    with pytest.raises(draft.Invalid, match="source_changed_or_linked"):
        tuple(analysis.lines(path, hashlib.sha256()))


def test_reparse_attribute_rejected(tmp_path, monkeypatch):
    path = tmp_path / "original"
    path.write_text("{}\n")
    original = Path.lstat

    def flagged(value, *args, **kwargs):
        info = original(value, *args, **kwargs)
        if value == path:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", flagged)
    with pytest.raises(evidence.EvidenceError, match="linked_path"):
        tuple(analysis.lines(path, hashlib.sha256()))


@pytest.mark.parametrize(
    "mutation,status",
    [
        ("none", "verified"),
        ("missing", "unverified"),
        ("duplicate", "failed"),
        ("wrong_view", "failed"),
        ("cross_snapshot", "failed"),
    ],
)
def test_wal_binding_reuse_and_cross_group_rules(tmp_path, mutation, status):
    rows = full_rows()
    storage = rows["storage-phases.jsonl"]
    release = rows["release-events.jsonl"]
    for number in (2, 3):
        release.insert(-1, dict(id=number, resource="thread", event="acquired", at_s=1))
        release.insert(-1, dict(id=number, resource="view", event="acquired", at_s=2))
        release.insert(-1, dict(id=number, resource="view", event="released", at_s=7))
        release.insert(-1, dict(id=number, resource="thread", event="exit_verified", at_s=8))
        storage.append(
            dict(
                id=B + number + 950,
                process_sequence=0,
                phase="wal_growth",
                at_s=2,
                wal_id=B + number + 500,
                event="initialized",
                started_s=2,
                before=None,
                after=dict(generation_hash=H, physical_bytes=0, page_size=4096, additional_bytes=0),
                returned_additional_bytes=0,
                outcome="ok",
            )
        )
        storage.append(
            dict(
                id=B + number + 960,
                process_sequence=0,
                phase="wal_binding",
                at_s=2,
                wal_id=B + number + 500,
                snapshot_id=B + number + 600,
                limits_id=B + number + 700,
                view_id=number,
                database_kind="memory",
                database_hash=H,
                outcome="ok",
            )
        )
    binding = next(row for row in storage if row["phase"] == "wal_binding")
    if mutation == "missing":
        storage.remove(binding)
    elif mutation == "duplicate":
        extra = copy.deepcopy(binding)
        extra["id"] = B + 999
        storage.append(extra)
    elif mutation == "wrong_view":
        binding["view_id"] = 4
    elif mutation == "cross_snapshot":
        binding["snapshot_id"] = B + 603
    result = evaluate(tmp_path, rows)
    assert result["requirements"]["wal_associations"]["status"] == status


@pytest.mark.parametrize(
    "mutation,status",
    [
        ("none", "verified"),
        ("missing_exit", "unverified"),
        ("duplicate_exit", "failed"),
        ("wrong_process", "failed"),
    ],
)
def test_process_exit_ordinals(tmp_path, mutation, status):
    run, rows = manifest(), full_rows()
    run["observer_sequences"] = [0, 1]
    released = rows["release-events.jsonl"]
    released.insert(0, dict(id=1, resource="process", event="acquired", at_s=0))
    if mutation != "missing_exit":
        released.append(
            dict(
                id=2 if mutation == "wrong_process" else 1,
                resource="process",
                event="exit_verified",
                at_s=13,
            )
        )
    if mutation == "duplicate_exit":
        released.append(copy.deepcopy(released[-1]))
    result = evaluate(tmp_path, rows, run)
    assert result["requirements"]["resource_lifecycles"]["status"] == status


@pytest.mark.parametrize("late,status", [(False, "verified"), (True, "failed")])
def test_native_outer_finish_in_complete_window(tmp_path, late, status):
    rows = full_rows()
    rows["requests.jsonl"][1]["finished_s"] = 12
    if late:
        next(
            row
            for row in rows["storage-phases.jsonl"]
            if row["phase"] == "native_validation" and row["event"] == "finished"
        )["at_s"] = 14
    result = evaluate(tmp_path, rows)
    assert result["requirements"]["native_and_child_exit_window"]["status"] == status
