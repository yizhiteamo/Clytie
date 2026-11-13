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


def resource_fixture(arm="candidate"):
    run = dict(format=5, arm=arm, observer_sequences=[0, 1])
    rows = []
    resources = [("reference", 1), ("slot", 1), ("process", 1)]
    if arm != "baseline":
        resources.insert(0, ("thread", 1))
        resources.extend([("thread", 2), ("view", 2)])
    for resource, identifier in resources:
        rows.append(dict(resource=resource, id=identifier, event="acquired", at_s=1))
    for resource, identifier in reversed(resources):
        event = "exit_verified" if resource in {"thread", "process"} else "released"
        rows.append(dict(resource=resource, id=identifier, event=event, at_s=2))
    return run, rows


def check_resources(run, rows):
    validator = samples.ResourceReleases(run)
    for row in rows:
        validator.add(row)
    validator.complete()


@pytest.mark.parametrize("arm", ["candidate", "original", "none", "baseline"])
def test_resource_completion_keeps_distinct_resource_namespaces(arm):
    check_resources(*resource_fixture(arm))


@pytest.mark.parametrize(
    "damage",
    [
        "owner_missing",
        "snapshot_cannot_exit_owner",
        "view_owner_collision",
        "view_without_thread",
        "view_after_thread_exit",
        "view_not_released",
        "reference_not_released",
        "slot_not_released",
        "process_not_exited",
        "thread_not_exited",
        "duplicate_acquire",
        "duplicate_release",
        "release_without_acquire",
        "release_before_acquire_time",
        "thread_only_released",
        "view_only_exited",
        "unknown_then_release",
        "failed_then_release",
        "cancel_without_exit",
        "io_return_without_exit",
        "duplicate_cancel",
        "unplanned_process",
        "wrong_reference_id",
        "empty",
        "exit_before_view_release",
    ],
)
def test_resource_completion_rejects_incomplete_or_ambiguous_stream(damage):
    run, rows = resource_fixture()
    if damage == "empty":
        rows.clear()
    elif damage == "owner_missing":
        rows = [row for row in rows if (row["resource"], row["id"]) != ("thread", 1)]
    elif damage == "snapshot_cannot_exit_owner":
        rows = [
            row
            for row in rows
            if not (
                row["resource"] == "thread" and row["id"] == 1 and row["event"] == "exit_verified"
            )
        ]
    elif damage == "view_owner_collision":
        for row in rows:
            if row["resource"] == "view":
                row["id"] = 1
    elif damage == "view_without_thread":
        rows = [row for row in rows if (row["resource"], row["id"]) != ("thread", 2)]
    elif damage == "view_after_thread_exit":
        for row in rows:
            if (row["resource"], row["id"]) == ("thread", 2) and row["event"] == "exit_verified":
                rows.remove(row)
                rows.insert(5, row)
                break
    elif damage.endswith("_not_released") or damage.endswith("_not_exited"):
        resource = damage.split("_")[0]
        rows = [
            row for row in rows if not (row["resource"] == resource and row["event"] != "acquired")
        ]
    elif damage == "duplicate_acquire":
        rows.insert(1, copy.deepcopy(rows[0]))
    elif damage == "duplicate_release":
        rows.append(copy.deepcopy(rows[-1]))
    elif damage == "release_without_acquire":
        rows.pop(0)
    elif damage == "release_before_acquire_time":
        rows[-1]["at_s"] = 0
    elif damage == "thread_only_released":
        rows[-1]["event"] = "released"
    elif damage == "view_only_exited":
        next(row for row in rows if row["resource"] == "view" and row["event"] == "released")[
            "event"
        ] = "exit_verified"
    elif damage in {"unknown_then_release", "failed_then_release"}:
        rows.insert(1, {**rows[0], "event": damage.split("_")[0]})
    elif damage in {"cancel_without_exit", "io_return_without_exit", "duplicate_cancel"}:
        rows[-1]["event"] = (
            "io_returned" if damage == "io_return_without_exit" else "cancel_requested"
        )
        if damage == "duplicate_cancel":
            rows.append(copy.deepcopy(rows[-1]))
    elif damage == "unplanned_process":
        next(row for row in rows if row["resource"] == "process")["id"] = 2
    elif damage == "wrong_reference_id":
        next(row for row in rows if row["resource"] == "reference" and row["event"] == "released")[
            "id"
        ] = 2
    elif damage == "exit_before_view_release":
        thread = next(
            row
            for row in rows
            if row["resource"] == "thread" and row["id"] == 2 and row["event"] == "exit_verified"
        )
        rows.remove(thread)
        rows.insert(6, thread)
    with pytest.raises(samples.SampleError):
        check_resources(run, rows)


def test_resource_completion_accepts_observed_cancellation_then_real_exit():
    run, rows = resource_fixture()
    rows.insert(1, {**rows[0], "event": "cancel_requested"})
    rows.insert(2, {**rows[0], "event": "io_returned"})
    check_resources(run, rows)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_resource_completion_cannot_reinterpret_historical_formats(version):
    run, _ = resource_fixture()
    with pytest.raises(samples.SampleError, match="resource_format_invalid"):
        samples.ResourceReleases({**run, "format": version})


@pytest.mark.parametrize("result", ["fail", "cancelled", "incomplete"])
def test_uncertain_resource_stream_stays_archivable_without_complete_release(tmp_path, result):
    from tests.test_measurement_io_observation import observer_for, run_v5
    from tests.test_measurement_manifest import contents, save_manifest

    evidence = load("measurement_evidence")
    source = tmp_path / "source"
    run = run_v5(source)
    run["result"] = result
    save_manifest(source, run)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        pass
    (source / "storage-phases.jsonl").write_bytes(bytes(recorder.rows["storage-phases.jsonl"]))
    (source / "release-events.jsonl").write_text(
        json.dumps(dict(resource="thread", id=1, event="unknown", at_s=1)) + "\n",
        encoding="utf-8",
    )
    evidence.validate_files(contents(source), run)
    run["result"] = "pass"
    save_manifest(source, run)
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(contents(source), run)
