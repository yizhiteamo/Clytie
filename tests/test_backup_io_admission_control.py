import asyncio
import threading

import pytest

from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.plugin_host.locking import process_identity
from clytie.sdk.testing import MockClock


class Records:
    def __init__(self):
        self.calls = []
        self.state = "ready"

    def note(self, name, *args):
        self.calls.append((name, threading.get_ident(), args))

    def check(self, controller):
        self.note("check", controller)
        return {"state": self.state, "blockers": []}

    def reserve(self, *args, **kwargs):
        self.note("reserve", *args)
        return "generation_a"

    def bind(self, *args):
        self.note("bind", *args)
        return "generation_a"

    def register(self, *args):
        self.note("register", *args)
        return "generation_a"

    def release(self, *args, **kwargs):
        self.note("release", *args)

    def ended(self, *args):
        self.note("ended", *args)


async def test_admission_metadata_runs_outside_loop_and_retired_token_cannot_return():
    control = IoControl(MockClock())
    records = control.responsibilities = Records()
    session = control.register()
    await control.reserve(session, "operation_a")
    assert not control.sessions[session].endpoints
    await control.bind_execution(session, "operation_a", {"pid": 1, "started_at": "fictional"})
    assert "operation_a" in control.sessions[session].endpoints
    await control.retire_operation("operation_a")
    with pytest.raises(IoBudgetError, match="not_authorized"):
        control.authorize(session, "operation_a")
    assert [row[0] for row in records.calls] == ["reserve", "bind", "release"]
    assert all(row[1] != threading.get_ident() for row in records.calls)


async def test_unknown_admission_is_not_accepted_and_query_stays_available():
    control = IoControl(MockClock())
    control.responsibilities = records = Records()
    records.state = "blocked"
    with pytest.raises(IoBudgetError, match="recheck_required"):
        await control.ensure_ready()
    assert (await control.recheck())["state"] == "blocked"
    assert not control.sessions


async def test_cancelled_reservation_waits_real_metadata_exit_and_releases_unexecuted_intent():
    control = IoControl(MockClock())
    control.responsibilities = records = Records()
    entered, release = threading.Event(), threading.Event()

    def reserve(*args, **kwargs):
        records.note("reserve", *args)
        entered.set()
        assert release.wait(5)
        return "generation_a"

    records.reserve = reserve
    session = control.register()
    task = asyncio.create_task(control.reserve(session, "operation_a"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await control.budget.clock.sleep(0)
        assert not task.done()
        assert not control.sessions[session].endpoints
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [row[0] for row in records.calls] == ["reserve", "release"]


async def test_end_requires_real_exit_and_persists_without_replaying_operation():
    control = IoControl(MockClock())
    control.responsibilities = records = Records()
    session = control.register()
    await control.authorize_execution(
        session, "operation_a", {"pid": 1, "started_at": "fictional"}, role="recovery"
    )
    with pytest.raises(IoBudgetError, match="exit_unconfirmed"):
        await control.ended(session, confirmed=False)
    assert [row[0] for row in records.calls] == ["register"]
    await control.ended(session, confirmed=True)
    assert [row[0] for row in records.calls] == ["register", "ended"]


async def test_real_durable_control_holds_conflict_until_stage_and_executor_resources_end(tmp_path):
    first = IoControl(MockClock(), home=tmp_path)
    second = IoControl(MockClock(), home=tmp_path)
    session = first.register()
    await first.authorize_execution(session, "operation_a", process_identity())
    endpoint = first.sessions[session].endpoints["operation_a"]
    await endpoint.request({"action": "acquire", "stage": "phase_a"})
    assert not await first.retire_operation("operation_a")
    with pytest.raises(IoBudgetError, match="recheck_required"):
        await second.ensure_ready()
    # Real completion precedes durable release. Business retirement alone is
    # insufficient while a native reader still has the stage.
    await endpoint.request({"action": "release", "stage": "phase_a"})
    assert await first.retire_operation("operation_a")
    await second.ensure_ready()
    replies = []

    async def send(reply):
        replies.append(reply)

    await first.dispatch(
        session,
        {
            "type": "backup_io",
            "id": "stale",
            "operation_id": "operation_a",
            "arguments": {"action": "acquire", "stage": "later"},
        },
        send,
    )
    assert replies[-1]["error"] == "backup_io_operation_not_authorized"
    assert first.budget.status()["active"] is None
    assert (await second.recheck())["state"] == "ready"


async def test_metadata_completion_does_not_race_this_controllers_queries(tmp_path, monkeypatch):
    control = IoControl(MockClock(), home=tmp_path)
    session = control.register()
    await control.authorize_execution(session, "operation_a", process_identity())
    entered, release = threading.Event(), threading.Event()
    original = control.responsibilities._save

    def paused_save(value):
        entered.set()
        assert release.wait(5)
        return original(value)

    monkeypatch.setattr(control.responsibilities, "_save", paused_save)
    closing = asyncio.create_task(control.ended(session, confirmed=True))
    checking = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        checking = asyncio.create_task(control.recheck())
        await control.budget.clock.drain(20)
        assert not checking.done()
    finally:
        release.set()
        await closing
    assert (await checking)["state"] == "ready"
    assert not control.sessions


async def test_actual_exit_revokes_memory_authority_but_failed_persistence_stays_unknown(
    tmp_path, monkeypatch
):
    control = IoControl(MockClock(), home=tmp_path)
    session = control.register(storage_owner=True)
    await control.authorize_execution(session, "operation_a", process_identity())
    original = control.responsibilities.index_path.read_bytes()

    def unavailable(*args):
        raise IoBudgetError("backup_io_recheck_required")

    monkeypatch.setattr(control.responsibilities, "ended", unavailable)
    with pytest.raises(IoBudgetError, match="recheck_required"):
        await control.ended(session, confirmed=True)
    assert session not in control.sessions
    assert control.responsibilities.index_path.read_bytes() == original
    # Persistence has no trusted end evidence. A different controller must not
    # infer release from our in-memory removal (this test process is still alive).
    assert (await IoControl(MockClock(), home=tmp_path).recheck())["state"] == "blocked"
