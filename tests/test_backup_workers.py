import asyncio
import json
import subprocess
import sys

import pytest
from tests.test_backup_restore import configured_package

from clytie.backup.journal import BackupJournal
from clytie.backup.restore import read_state
from clytie.backup.workers import BackupWorkers, WorkerError
from clytie.clock import SystemClock
from clytie.plugin_host.support import timed
from clytie.sdk.testing import MockClock


async def completed(workers, operation_id):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while True:
        result = next(item for item in workers.status() if item.get("id") == operation_id)
        if result["phase"] in {"committed", "failed", "cancelled", "recovery_blocked"}:
            await workers.stop("wait")
            return result
        if loop.time() >= deadline:
            break
        await SystemClock().sleep(0.02)
        if isinstance(workers.clock, MockClock):
            workers.clock.advance(0.02)
    pytest.fail(
        "backup worker did not finish: "
        + repr(
            {
                "phase": result.get("phase"),
                "reason": result.get("reason"),
                "tracked": len(workers.tasks),
                "process_returncodes": [p.returncode for p in workers.processes.values()],
                "io": workers.io_control.budget.status(),
            }
        )
    )


async def test_real_worker_plan_apply_and_duplicate_id(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    workers = BackupWorkers(manager.home, MockClock())
    op = "a" * 32
    accepted = await workers.execute(
        "restore_plan", dict(source=str(package), key_path=str(key), target=str(target)), op
    )
    assert accepted["id"] == op
    planned = await completed(workers, op)
    assert planned["phase"] == "committed"
    assert planned["result"]["plan_id"] == op
    workers = BackupWorkers(manager.home, MockClock())
    applied = await workers.execute("restore_apply", {"plan_id": op}, "b" * 32)
    assert applied["id"] == "b" * 32
    result = await completed(workers, "b" * 32)
    assert result["result"]["restore_readiness"] == "ready_for_switch"
    assert read_state(target)["data_state"] == "ready_for_switch"
    third = BackupWorkers(manager.home, MockClock())
    duplicate = await third.execute("restore_apply", {"plan_id": "c" * 32}, "b" * 32)
    assert duplicate == result
    assert not third.tasks
    journal = BackupJournal.load(BackupJournal.path_for(manager.home, "b" * 32), MockClock())
    assert journal.value["delivery"] == "received"
    assert journal.value["executor"]["pid"]
    assert "arguments" not in result


async def test_disconnect_does_not_cancel_accepted_background_task(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    workers = BackupWorkers(manager.home, MockClock())
    op = "d" * 32
    waiter = asyncio.create_task(
        workers.execute(
            "restore_plan", dict(source=str(package), key_path=str(key), target=str(target)), op
        )
    )
    while not workers.tasks:
        await SystemClock().sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    result = await completed(workers, op)
    assert result["phase"] == "committed"


async def test_disconnect_during_preview_admission_finishes_same_journal(tmp_path, monkeypatch):
    import threading

    from clytie.backup.restore import RestoreManager

    manager, package, key, target, workspace = configured_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, workspace)
    entered, proceed = threading.Event(), threading.Event()
    original = RestoreManager.admit

    def paused(self, plan_id, operation_id):
        result = original(self, plan_id, operation_id)
        entered.set()
        assert proceed.wait(10)
        return result

    monkeypatch.setattr(RestoreManager, "admit", paused)
    workers = BackupWorkers(manager.home, MockClock())

    def track(journal):
        ready = asyncio.get_running_loop().create_future()
        ready.set_result(True)
        return ready

    monkeypatch.setattr(workers, "_track", track)
    caller = asyncio.create_task(
        workers.execute("restore_apply", {"plan_id": plan["id"]}, "7" * 32)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        proceed.set()
    await workers.stop("wait")
    assert workers.query("7" * 32)["phase"] == "accepted"
    assert manager.previews.get("instance", plan["id"])["operation_id"] == "7" * 32


async def test_same_instance_has_no_second_writer_and_stop_rejects_admission(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(tmp_path, monkeypatch)
    from clytie.plugin_host.locking import file_lock, process_identity

    with file_lock(
        manager.home / "run/backup-worker.lock", dict(**process_identity(), operation="backup")
    ):
        workers = BackupWorkers(manager.home, MockClock())
        with pytest.raises(WorkerError, match="backup_busy"):
            await workers.execute(
                "restore_plan",
                dict(source=str(package), key_path=str(key), target=str(target)),
                "e" * 32,
            )
    await workers.stop("cancel")
    with pytest.raises(WorkerError, match="supervisor_stopping"):
        await workers.execute("restore_plan", {}, "f" * 32)


def test_child_eof_before_delivery_never_starts_restore(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(tmp_path, monkeypatch)
    op = "1" * 32
    journal, _ = BackupJournal.accept(
        manager.home,
        MockClock(),
        op,
        "restore_plan",
        dict(source=str(package), key_path=str(key), target=str(target)),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "clytie.backup.worker", str(manager.home), op],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output, _ = process.communicate(b"", timeout=30)
    assert process.returncode == 0
    assert json.loads(output.splitlines()[-1])["type"] == "result"
    value = BackupJournal.load(journal.path, MockClock()).value
    assert value["phase"] == "cancelled"
    assert value.get("delivery") != "received"
    assert not target.exists()
    assert not manager._path(op).exists()


HOOK_WORKER = """
import json, sys, threading
from pathlib import Path
from clytie.backup import worker
from clytie.sdk.testing import MockClock
home, op, hook, target = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
original_commit = worker.WorkerJournal.commit
def traced_commit(self, result):
    try:
        return original_commit(self, result)
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
worker.WorkerJournal.commit = traced_commit
def emit(value):
    print(json.dumps(value), flush=True)
if hook == "plan_published":
    original = worker.JournalRestore._publish_plan
    def pause(self, plan):
        original(self, plan)
        emit({"type":"checkpoint"})
        threading.Event().wait()
    worker.JournalRestore._publish_plan = pause
elif hook in {"restore_published", "cancel_published"}:
    original = worker.JournalRestore._finish
    def pause(self, plan, state):
        result = original(self, plan, state)
        emit({"type":"checkpoint"})
        if hook == "cancel_published":
            self.controls.cancelled.wait()
        else:
            threading.Event().wait()
        return result
    worker.JournalRestore._finish = pause
else:
    original = worker.Controls.check
    def pause(self):
        if (target / "supervisor/restore.json").exists() and not getattr(self,"paused",False):
            self.paused = True
            emit({"type":"checkpoint"})
            if hook == "external_copying":
                from clytie.backup.journal import BackupJournal
                while not BackupJournal.load(self.journal.path, self.journal.clock).value["cancel_requested"]:
                    threading.Event().wait(0.01)
            else:
                threading.Event().wait()
        original(self)
    worker.Controls.check = pause
result = worker.execute(home, op, stream=sys.stdin.buffer.raw, emit=emit, clock=MockClock())
emit({"type":"result", "result":result})
if len(sys.argv) > 5:
    while not Path(sys.argv[5]).exists():
        threading.Event().wait(0.01)
"""


def started_worker(home, op, hook, target, *, exit_gate=None):
    import queue
    import threading

    from clytie.backup.io_control import IoControl

    process = subprocess.Popen(
        [sys.executable, "-c", HOOK_WORKER, str(home), op, hook, str(target)]
        + ([str(exit_gate)] if exit_gate is not None else []),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process.messages = queue.Queue()

    def control_pipe():
        async def serve():
            clock = MockClock()
            control = IoControl(clock)
            session = control.register()
            control.authorize(session, op)

            async def send(value):
                process.stdin.write(json.dumps(value).encode() + b"\n")
                process.stdin.flush()

            try:
                while raw := await asyncio.to_thread(process.stdout.readline):
                    message = json.loads(raw)
                    if message["type"] != "backup_io":
                        process.messages.put(message)
                        continue
                    response = control.dispatch(session, message, send)
                    while not response.done():
                        await SystemClock().sleep(0.01)
                        clock.advance(0.01)
                    await response
            except Exception as exc:
                process.messages.put(exc)
            finally:
                await asyncio.to_thread(process.wait)
                await control.ended(session, confirmed=True)

        asyncio.run(serve())

    process.controller = threading.Thread(target=control_pipe, daemon=True)
    process.controller.start()
    assert worker_message(process)["type"] == "ready"
    process.stdin.write(b"start\n")
    process.stdin.flush()
    assert worker_message(process)["type"] == "started"
    assert worker_message(process)["type"] == "checkpoint"
    return process


def worker_message(process):
    value = process.messages.get(timeout=15)
    if isinstance(value, Exception):
        raise value
    return value


def finish_child(process, *, kill=True):
    if kill and process.poll() is None:
        process.kill()
    process.wait(timeout=15)
    process.controller.join(timeout=15)
    assert not process.controller.is_alive()
    process.stdin.close()
    process.stdout.close()
    process.stderr.close()


@pytest.mark.parametrize("hook", ["plan_published", "restore_copying", "restore_published"])
async def test_killed_worker_verifies_registered_artifacts_without_replay(
    tmp_path, monkeypatch, hook
):
    from clytie.backup.workers import executor_alive, worker_locked

    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    op = "2" * 32
    if hook == "plan_published":
        operation, args = (
            "restore_plan",
            dict(source=str(package), key_path=str(key), target=str(target)),
        )
    else:
        plan = manager.plan(package, key, target, workspace)
        operation, args = "restore_apply", dict(plan_id=plan["id"])
    journal, _ = BackupJournal.accept(manager.home, MockClock(), op, operation, args)
    process = started_worker(manager.home, op, hook, target)
    finish_child(process)
    value = BackupJournal.load(journal.path, MockClock()).value
    assert value["delivery"] == "received"
    package.write_bytes(b"source must not be unpacked again")
    for _ in range(100):
        if executor_alive(value["executor"]) is False and not worker_locked(manager.home):
            break
        await SystemClock().sleep(0.01)
    workers = BackupWorkers(manager.home, MockClock())
    await workers.recover_pending()
    verified = BackupJournal.load(journal.path, MockClock()).value
    assert verified["executor"] == value["executor"]
    assert verified["recovery_executor"] != verified["executor"]
    assert executor_alive(verified["recovery_executor"]) is False
    result = workers.query(op)
    if hook == "restore_copying":
        assert result["phase"] == "recovery_blocked"
        assert read_state(target)["data_state"] == "recovery_blocked"
        assert not (target / "companions").exists()
    else:
        assert result["phase"] == "committed"
        assert result["result"]["verification"] == "passed"
    await workers.stop("wait")


def test_cancel_during_publishing_finishes_commit(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    plan = manager.plan(package, key, target, workspace)
    op = "3" * 32
    journal, _ = BackupJournal.accept(
        manager.home, MockClock(), op, "restore_apply", dict(plan_id=plan["id"])
    )
    process = started_worker(manager.home, op, "cancel_published", target)
    process.stdin.write(b"cancel\n")
    process.stdin.flush()
    result = worker_message(process)
    finish_child(process, kill=False)
    assert result["result"]["phase"] == "committed"
    assert (
        BackupJournal.load(journal.path, MockClock()).value["result"]["restore_readiness"]
        == "ready_for_switch"
    )


PARENT_WORKER = """
import json, sys, threading
from pathlib import Path
from clytie.backup import worker
from clytie.sdk.testing import MockClock
home, op, target, checkpoint, mode = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
original = worker.Controls.check
def pause(self):
    if (target / "supervisor/restore.json").exists() and not getattr(self,"paused",False):
        self.paused = True
        checkpoint.write_text("checkpoint")
        if mode == "parent_only":
            self.cancelled.wait()
        else:
            threading.Event().wait()
    original(self)
worker.Controls.check = pause
def emit(value):
    print(json.dumps(value),flush=True)
result = worker.execute(home,op,stream=sys.stdin.buffer.raw,emit=emit)
emit({"type":"result","result":result})
"""


@pytest.mark.parametrize("mode", ["parent_only", "whole_tree"])
async def test_parent_or_tree_kill_never_reexecutes_restore(tmp_path, monkeypatch, mode):
    import os
    import signal

    from clytie.backup.workers import executor_alive

    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    plan = manager.plan(package, key, target, workspace)
    op = "4" * 32
    checkpoint = tmp_path / "checkpoint"
    code = """
import asyncio, json, sys
from pathlib import Path
from clytie.backup.workers import BackupWorkers
from clytie.clock import SystemClock
child_code = sys.argv[6]
original = asyncio.create_subprocess_exec
async def spawn(executable, *args, **kwargs):
    return await original(executable,"-c",child_code,args[2],args[3],sys.argv[3],sys.argv[4],sys.argv[5],**kwargs)
asyncio.create_subprocess_exec = spawn
async def main():
    from clytie.sdk.testing import MockClock
    clock=MockClock()
    workers=BackupWorkers(Path(sys.argv[1]),clock)
    async def tick():
        while True:
            await SystemClock().sleep(0.01)
            clock.advance(0.01)
    ticking=asyncio.create_task(tick())
    await workers.execute("restore_apply",{"plan_id":sys.argv[2]},"4"*32)
    print("controller_started",flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(manager.home),
            plan["id"],
            str(target),
            str(checkpoint),
            mode,
            PARENT_WORKER,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_identity = None
    try:
        assert parent.stdout.readline().strip() == "controller_started"
        for _ in range(500):
            if checkpoint.exists():
                break
            await SystemClock().sleep(0.01)
        else:
            pytest.fail("child did not reach real copying checkpoint")
        child_identity = BackupJournal.load(
            BackupJournal.path_for(manager.home, op), MockClock()
        ).value["executor"]
        parent.kill()
        parent.wait(timeout=15)
        if mode == "whole_tree" and executor_alive(child_identity) is True:
            os.kill(
                child_identity["pid"], signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL
            )
        workers = BackupWorkers(manager.home, SystemClock())
        await workers.recover_pending()
        for _ in range(500):
            state = workers.query(op)
            if state["phase"] in {"cancelled", "recovery_blocked", "failed"}:
                break
            await SystemClock().sleep(0.02)
            await workers.recover_pending()
        assert workers.query(op)["phase"] in {"cancelled", "recovery_blocked"}
        await workers.stop("wait")
        assert read_state(target)["data_state"] in {"incomplete", "recovery_blocked"}
        assert not (target / "companions").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=15)
        parent.stdout.close()
        parent.stderr.close()
        for _ in range(100):
            if not child_identity or executor_alive(child_identity) is False:
                break
            await SystemClock().sleep(0.01)
        if child_identity and executor_alive(child_identity) is True:
            try:
                os.kill(
                    child_identity["pid"],
                    signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL,
                )
            except OSError:
                if executor_alive(child_identity) is True:
                    raise


async def test_restarted_controller_tracks_and_cancels_live_executor(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    plan = manager.plan(package, key, target, workspace)
    op = "5" * 32
    BackupJournal.accept(manager.home, MockClock(), op, "restore_apply", {"plan_id": plan["id"]})
    process = started_worker(manager.home, op, "external_copying", target)
    workers = BackupWorkers(manager.home, SystemClock())
    try:
        await workers.recover_pending()
        assert op in workers.tasks
        await workers.stop("cancel")
        assert workers.query(op)["phase"] == "cancelled"
        assert not workers.tasks
        finish_child(process, kill=False)
    finally:
        if process.poll() is None:
            finish_child(process)


async def test_startup_handshake_wait_is_bounded_and_queryable(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    clock = MockClock()
    workers = BackupWorkers(manager.home, clock, startup_timeout=1)
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    op = "6" * 32
    waiter = asyncio.create_task(
        workers.execute(
            "restore_plan", dict(source=str(package), key_path=str(key), target=str(target)), op
        )
    )
    while clock.pending_sleep_count == 0:
        await SystemClock().sleep(0.01)
    clock.advance(2)
    result = await waiter
    assert result["id"] == op
    assert result["cancel_requested"]
    assert result["phase"] == "accepted"
    release.set()
    result = await completed(workers, op)
    assert result["phase"] == "cancelled"
    assert not manager._path(op).exists()


async def test_stop_reminder_never_kills_publishing_executor(tmp_path, monkeypatch):
    from clytie.backup.io_budget import IoBudgetError
    from clytie.backup.workers import executor_alive, worker_locked

    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    plan = manager.plan(package, key, target, workspace)
    op = "7" * 32
    BackupJournal.accept(manager.home, MockClock(), op, "restore_apply", {"plan_id": plan["id"]})
    exit_gate = tmp_path / "allow-fictional-worker-exit"
    process = started_worker(manager.home, op, "cancel_published", target, exit_gate=exit_gate)
    clock = MockClock()
    workers = BackupWorkers(manager.home, clock)
    stopping = None
    try:
        await workers.recover_pending()
        stopping = asyncio.create_task(workers.stop("cancel"))
        await clock.drain()
        assert workers.cancel_since is not None
        initial = workers.cancel_since
        clock.advance(119)
        assert not workers.still_finishing
        workers.escalate()
        assert workers.cancel_since == initial
        clock.advance(2)
        assert workers.still_finishing
        assert process.poll() is None
        assert not stopping.done()
        process.stdin.write(b"cancel\n")
        process.stdin.flush()
        response = await timed(asyncio.to_thread(worker_message, process), SystemClock(), 15)
        assert response["type"] == "result"
        assert response["result"]["phase"] == "committed"
        replacement = BackupWorkers(manager.home, clock)
        with pytest.raises(IoBudgetError, match="backup_io_recheck_required"):
            await replacement.io_control.ensure_ready()
        with pytest.raises(IoBudgetError, match="backup_io_recheck_required"):
            await replacement.execute("verify", {"backup_id": "b" * 32}, "c" * 32)
        assert not BackupJournal.path_for(manager.home, "c" * 32).exists()
        # 固定最后一次轮询先于真实退出；机器快慢不能改变此顺序。
        await clock.advance_and_drain(1)
        assert process.poll() is None
        assert not stopping.done()
        assert clock.pending_sleep_count == 1
        exit_gate.write_text("release", encoding="utf-8")
        assert await asyncio.to_thread(process.wait, timeout=15) == 0
        value = BackupJournal.load(BackupJournal.path_for(manager.home, op), clock).value
        assert executor_alive(value["executor"]) is False
        assert not worker_locked(manager.home)
        assert (await replacement.io_control.recheck())["state"] == "ready"
        await clock.drain()
        assert not stopping.done()
        assert clock.pending_sleep_count == 1
        # 外部执行者已退出，显式唤醒注入时钟上的观察者；不能停在裸 await。
        await clock.advance_and_drain(0.05)
        assert stopping.done()
        await stopping
        assert workers.query(op)["phase"] == "committed", process.stderr.read().decode(
            errors="replace"
        )
        assert not workers.tasks
    finally:
        finish_child(process)
        for task in tuple(workers.tasks.values()):
            task.cancel()
        await asyncio.gather(*tuple(workers.tasks.values()), return_exceptions=True)
        if stopping is not None:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)


def test_worker_evidence_waits_for_concurrent_cancel_writer(tmp_path):
    import threading

    from clytie.backup.worker import WorkerJournal
    from clytie.plugin_host.locking import file_lock, process_identity

    journal, _ = BackupJournal.accept(
        tmp_path, MockClock(), "8" * 32, "restore_apply", {"plan_id": "9" * 32}
    )
    entered, release = threading.Event(), threading.Event()

    def hold():
        with file_lock(
            journal.path.with_suffix(".lock"),
            dict(**process_identity(), operation="fictional_cancel_writer"),
        ):
            entered.set()
            release.wait()

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(5)
    timer = threading.Timer(0.03, release.set)
    timer.start()
    try:
        WorkerJournal.load(journal.path, MockClock()).request_cancel()
    finally:
        release.set()
        thread.join(5)
        timer.join(5)
    assert BackupJournal.load(journal.path, MockClock()).value["cancel_requested"]
