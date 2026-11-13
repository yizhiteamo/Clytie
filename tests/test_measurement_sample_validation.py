import copy
import json

import pytest
from tests.measurement_tools import load

samples = load("measurement_sample_validation")


def fixture(arm="candidate"):
    run = dict(
        arm=arm, load=dict(request_count=3, interval_seconds=0.025, backup_start_seconds=0.01)
    )
    rows = [
        dict(
            id=i + 1,
            kind="business",
            scheduled_s=1 + i * 0.025,
            started_s=1 + i * 0.025,
            finished_s=1.01 + i * 0.025,
            outcome="ok",
        )
        for i in range(3)
    ]
    if arm in {"candidate", "original"}:
        rows.append(
            dict(
                id=100,
                kind="backup",
                scheduled_s=1.01,
                started_s=1.01,
                finished_s=1.04,
                outcome="ok",
            )
        )
    return run, rows


def check(run, rows):
    validator = samples.RequestPlan(run)
    for row in rows:
        validator.add(row)
    validator.complete()


@pytest.mark.parametrize("arm", ["baseline", "none", "original", "candidate"])
def test_all_arms_keep_exact_arrivals_and_complete_stream(arm):
    check(*fixture(arm))


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "reordered",
        "retimed",
        "busy",
        "early",
        "overlap",
        "backup_missing",
        "backup_duplicate",
        "backup_retimed",
    ],
)
def test_claimed_pass_cannot_drop_slow_samples_or_change_plan(damage):
    run, rows = fixture()
    if damage == "missing":
        rows.pop(1)
    elif damage == "duplicate":
        rows.insert(1, copy.deepcopy(rows[0]))
    elif damage == "reordered":
        rows[0], rows[1] = rows[1], rows[0]
    elif damage == "retimed":
        rows[1]["scheduled_s"] += 0.01
    elif damage == "busy":
        rows[1]["outcome"] = "busy"
    elif damage == "early":
        rows[1]["started_s"] -= 0.02
    elif damage == "overlap":
        rows[0]["finished_s"] = rows[1]["started_s"] + 0.01
    elif damage == "backup_missing":
        rows.pop()
    elif damage == "backup_duplicate":
        rows.append(copy.deepcopy(rows[-1]))
    else:
        rows[-1]["scheduled_s"] += 0.01
    with pytest.raises(samples.SampleError):
        check(run, rows)


def test_backlog_is_retained_without_retiming_arrivals():
    run, rows = fixture("none")
    for index, row in enumerate(rows):
        row.update(started_s=2 + index, finished_s=3 + index)
    check(run, rows)


@pytest.mark.parametrize(
    "damage",
    [
        "unplanned",
        "duplicate_start",
        "missing_start",
        "duplicate_exit",
        "early_exit",
        "still_alive",
    ],
)
def test_process_exit_must_bind_frozen_child_with_acquisition(damage):
    events = samples.ProcessExits(dict(observer_sequences=[0, 1]))
    start = dict(id=1, resource="process", event="acquired", at_s=1)
    end = dict(id=1, resource="process", event="exit_verified", at_s=2)
    with pytest.raises(samples.SampleError):
        if damage == "unplanned":
            events.add({**start, "id": 2})
        elif damage == "missing_start":
            events.add(end)
        else:
            events.add(start)
            if damage == "duplicate_start":
                events.add(start)
            elif damage == "early_exit":
                events.add({**end, "at_s": 0})
            elif damage == "duplicate_exit":
                events.add(end)
                events.add(end)
            else:
                events.complete()


def test_formal_archive_pass_requires_complete_plan_through_real_validator(tmp_path):
    from tests.test_measurement_io_observation import observer_for, run_v5
    from tests.test_measurement_manifest import contents, save_manifest

    evidence = load("measurement_evidence")
    source = tmp_path / "source"
    run = run_v5(source)
    run.update(result="pass", method="legal_storage_v1", phase="matrix", arm="none")
    run["source_commit"] = run["candidate"]
    run["execution"]["position"] = run["execution"]["order"].index("none")
    save_manifest(source, run)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        pass
    (source / "storage-phases.jsonl").write_bytes(bytes(recorder.rows["storage-phases.jsonl"]))
    rows = [
        dict(
            id=i + 1,
            kind="business",
            scheduled_s=1 + i * run["load"]["interval_seconds"],
            started_s=1 + i * run["load"]["interval_seconds"],
            finished_s=1.001 + i * run["load"]["interval_seconds"],
            outcome="ok",
        )
        for i in range(run["load"]["request_count"])
    ]
    target = source / "requests.jsonl"
    target.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows))
    evidence.validate_files(contents(source), run)
    target.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows[:-1]))
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(contents(source), run)
    # Safe failed raw evidence remains archivable, never promoted to a pass.
    run["result"] = "incomplete"
    save_manifest(source, run)
    evidence.validate_files(contents(source), run)
