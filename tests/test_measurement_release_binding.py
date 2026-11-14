import copy
import itertools
import json
import threading
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from tests.measurement_mechanism_fixtures import OTHER, B, H, control, events, result, small_index
from tests.measurement_tools import load

observer_module = load("measurement_control_observation")


def sample():
    rows = events()
    rows.extend(
        [
            control(
                "request_started",
                7.5,
                stage_id=H,
                action="release",
                call_hash=OTHER,
                outcome="unknown",
            ),
            control("request_finished", 8.5, stage_id=H, action="release", call_hash=OTHER),
            control(
                "responsibility_observed", 11, source="durable_save", responsibility_state="ended"
            ),
        ]
    )
    for number, row in enumerate(rows, 1):
        row["id"] = B + number
    return rows


def check(tmp_path, rows):
    obj = small_index(tmp_path, rows)
    try:
        obj.slots()
        method = obj.authorized_release_durable_end
        return result(obj, "authorized_release_durable_end", method)
    finally:
        obj.db.close()


def test_normal_release_does_not_require_process_exit(tmp_path):
    assert check(tmp_path, sample()) == (("verified", "observed_predicate_only"),)


@pytest.mark.parametrize(
    "mutation, status, reason",
    [
        ("execution", "failed", "release_authority_changed"),
        ("duplicate", "failed", "release_request_ambiguous"),
        ("failed", "failed", "release_request_failed"),
        ("durable", "unverified", "durable_ended_missing"),
        ("early", "failed", "responsibility_ended_before_release"),
        ("generation", "failed", "release_authority_changed"),
        ("no_release", "unverified", "trusted_release_or_bound_exit_missing"),
        ("load_only", "unverified", "durable_ended_missing"),
    ],
)
def test_raw_one_variable(tmp_path, mutation, status, reason):
    rows = sample()
    release = [r for r in rows if r.get("action") == "release"]
    if mutation in {"execution", "generation"}:
        for row in release:
            row[mutation + "_hash"] = OTHER
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(release[0]))
        rows[-1]["id"] += 100
    elif mutation == "failed":
        release[1]["outcome"] = "failed"
    elif mutation == "durable":
        rows.pop()
    elif mutation == "early":
        rows[-1]["at_s"] = 7
    elif mutation == "no_release":
        rows = [r for r in rows if r.get("action") != "release"]
    elif mutation == "load_only":
        rows[-1]["source"] = "validated_ledger"
    assert (status, reason) in check(tmp_path, rows)


@pytest.mark.parametrize(
    "mutation, status, reason",
    [
        ("none", "verified", "observed_predicate_only"),
        ("missing_join", "unverified", "required_event_missing"),
        ("late_close", "failed", "snapshot_release_order_invalid"),
        ("late_join", "failed", "snapshot_retired_before_resource_exit"),
        ("wrong_operation", "failed", "snapshot_authorized_operation_mismatch"),
        ("missing_revoke", "unverified", "required_event_missing"),
        ("duplicate_acquire", "failed", "snapshot_acquisition_duplicate"),
    ],
)
def test_snapshot_raw_one_variable(tmp_path, mutation, status, reason):
    obj = small_index(tmp_path, sample())
    obj.slots()
    extra = []
    for event, at in (
        ("snapshot_acquired", 1),
        ("snapshot_stage", 4),
        ("snapshot_closed", 7),
        ("snapshot_thread_exited", 9),
        ("authorization_revoked", 10),
    ):
        row = control(
            event,
            at,
            stage_id=H,
            binding_version=1,
            child_sequence=None,
            resource_id=B + 999,
            view_id=2,
        )
        if event == "authorization_revoked":
            row.update(resource_id=None, view_id=None, stage_id=None)
        row["id"] = B + 101 + len(extra)
        extra.append(row)
    if mutation == "missing_join":
        extra = [r for r in extra if r["event"] != "snapshot_thread_exited"]
    elif mutation == "late_close":
        extra[2]["at_s"] = 8.2
    elif mutation == "late_join":
        extra[3]["at_s"] = 12
    elif mutation == "wrong_operation":
        for row in extra:
            row["operation_hash"] = OTHER
    elif mutation == "missing_revoke":
        extra.pop()
    elif mutation == "duplicate_acquire":
        extra.append(dict(extra[0], id=B + 900))
    try:
        for number, row in enumerate(extra, 100):
            observer_module.validate_record(row)
            obj.db.execute(
                "INSERT INTO rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "storage-phases.jsonl",
                    number,
                    row["id"],
                    row["phase"],
                    row["event"],
                    row["at_s"],
                    row["stage_id"],
                    row["budget_id"],
                    None,
                    None,
                    None,
                    json.dumps(row),
                ),
            )
        method = obj.observed_snapshot_release_order
        assert (status, reason) in result(obj, "observed_snapshot_release_order", method)
    finally:
        obj.db.close()


def observer(tmp_path, stack):
    io_module = load("measurement_io_observation")
    rows = []

    class Recorder:
        format_version = 6
        failed = False

        def append(self, name, row):
            if row.get("phase") == observer_module.PHASE:
                observer_module.validate_record(row)
            rows.append(row)

    recorder = Recorder()
    ticking = itertools.count(1)
    outer = SimpleNamespace(
        root=tmp_path,
        recorder=recorder,
        guard=threading.Lock(),
        sequence=B,
        process_sequence=0,
        now=lambda: next(ticking),
    )
    io = io_module.IoObservation(outer, 0)
    io.install(stack)
    binding = observer_module.ControlObservation(io)
    binding.install(stack)
    return binding, recorder, rows


async def test_actual_control_revoked_before_durable_end_while_process_alive(tmp_path):
    from clytie.backup.io_control import IoControl
    from clytie.plugin_host.locking import process_identity
    from clytie.sdk.testing import MockClock

    with ExitStack() as stack:
        binding, recorder, rows = observer(tmp_path, stack)
        ctrl = IoControl(MockClock(), home=tmp_path)
        session = ctrl.register()
        await ctrl.authorize_execution(session, "fictional_operation", process_identity())
        endpoint = ctrl.sessions[session].endpoints["fictional_operation"]
        await endpoint.request(dict(action="acquire", stage="fictional_stage"))
        await endpoint.request(dict(action="release", stage="fictional_stage"))
        assert await ctrl.retire_operation("fictional_operation")
        revoked = next(r for r in rows if r.get("event") == "authorization_revoked")
        ended = next(r for r in rows if r.get("responsibility_state") == "ended")
        assert revoked["at_s"] < ended["at_s"]
        assert revoked["execution_hash"] == ended["execution_hash"]
        assert session in ctrl.sessions
        assert "fictional_operation" not in ctrl.sessions[session].endpoints
        assert not recorder.failed


def test_child_binding_uses_supplied_full_identity_and_wait_event(tmp_path):
    with ExitStack() as stack:
        binding, recorder, rows = observer(tmp_path, stack)

        class Child:
            pass

        child = Child()
        identity = dict(pid=17, started_at="fictional_creation_time")
        binding.child_registered(child, 1, "fictional_operation", identity)
        assert [r["event"] for r in rows if r.get("event") == "execution_waited"] == []
        binding.child_waited(child)  # Fictional proof injection, not an actual-process test.
        binding.child_waited(child)
        selected = [r for r in rows if r.get("event") in {"execution_bound", "execution_waited"}]
        assert len(selected) == 2
        assert (
            selected[0]["execution_hash"]
            == selected[1]["execution_hash"]
            == observer_module.identity_hash(identity)
        )
        assert selected[0]["child_sequence"] == selected[1]["child_sequence"] == 1
        assert "fictional_creation_time" not in json.dumps(selected)
        assert not recorder.failed
        invalid = dict(selected[0], child_sequence=True)
        with pytest.raises(observer_module.BindingError):
            observer_module.validate_record(invalid)


@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_snapshot_identity_close_and_thread_exit(tmp_path, cancel):
    import sqlite3

    from clytie.backup.capture import capture_group
    from clytie.backup.coordination import CaptureCoordinator, CaptureError
    from clytie.backup.io_channel import IoChannel
    from clytie.backup.io_client import io_scope
    from clytie.sdk.testing import MockClock

    path = tmp_path / "fictional.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE fictional(value BLOB)")
    writer.execute("INSERT INTO fictional VALUES(zeroblob(?))", (12 * 1000,))
    writer.commit()
    try:
        with ExitStack() as stack:
            binding, recorder, rows = observer(tmp_path, stack)
            views = load("measurement_views").Views(
                recorder, now=binding.io.observer.now, first_id=2
            )
            stack.enter_context(views.install())
            channel = IoChannel(lambda value: None)
            # Fake local budget transport; actual channel client, snapshot thread,
            # SQLite connections, backup batches, close and thread exit.
            charges = []

            def transport(operation, arguments, **kwargs):
                if arguments["action"] == "charge":
                    charges.append(arguments)
                return {}

            channel.call = transport
            client = channel.client("fictional_operation", check=lambda: None)

            def checkpoint():
                # Third charge can only occur after the first real backup progress
                # callback: initial reserve has one read and one write.
                if cancel and len(charges) >= 3:
                    raise CaptureError("cancelled")

            with io_scope(client):
                view = await capture_group(
                    {"memory": path},
                    CaptureCoordinator(tmp_path / "capture.lock", MockClock()),
                    epoch="fictional",
                )
                if cancel:
                    with pytest.raises(CaptureError):
                        await view.materialize(tmp_path / "output", pages=1, check=checkpoint)
                else:
                    await view.materialize(tmp_path / "output", pages=1, check=checkpoint)
            assert not view._worker.thread.is_alive() and not view._worker._connections
            selected = [r for r in rows if r.get("event", "").startswith("snapshot_")]
            assert [r["event"] for r in selected] == [
                "snapshot_acquired",
                "snapshot_stage",
                "snapshot_closed",
                "snapshot_thread_exited",
            ]
            assert len({r["resource_id"] for r in selected}) == 1
            assert {r["operation_hash"] for r in selected} == {
                observer_module.token_hash("fictional_operation")
            }
            assert selected[0]["stage_id"] is None
            assert selected[1]["stage_id"] is not None
            assert len({r["stage_id"] for r in selected[1:]}) == 1
            assert {r["view_id"] for r in selected[1:]} == {2}
            assert selected[2]["at_s"] < selected[3]["at_s"]
            assert not recorder.failed
    finally:
        writer.close()
