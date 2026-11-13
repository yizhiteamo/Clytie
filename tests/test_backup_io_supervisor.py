import asyncio
import json

from tests.test_supervisor import make, spin

from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.plugin_host.locking import file_lock, process_identity


async def test_supervisor_routes_budget_without_blocking_health_or_stop(tmp_path):
    svc, launcher, clock = await make(tmp_path)
    child = launcher.children[0][1]
    try:
        control = svc.backup_io
        worker = control.register()
        await control.authorize_execution(worker, "a" * 32, process_identity())

        async def sink(reply):
            assert reply.get("result") == {}

        await control.dispatch(
            worker,
            {
                "type": "backup_io",
                "id": "worker_acquire",
                "operation_id": "a" * 32,
                "arguments": {"action": "acquire", "stage": "worker"},
            },
            sink,
        )
        await svc.authorize_backup_capture("companion_0", "a" * 32)
        await child.queue.put(
            {
                "type": "backup_io",
                "seq": 3,
                "id": "companion_acquire",
                "operation_id": "a" * 32,
                "arguments": {"action": "acquire", "stage": "capture"},
            }
        )
        await spin()
        assert control.budget.status()["waiting"] == 1
        await child.heartbeat(inflight_tasks=2)
        await spin()
        assert svc.rows["companion_0"]["inflight_tasks"] == 2
        await control.dispatch(
            worker,
            {
                "type": "backup_io",
                "id": "worker_release",
                "operation_id": "a" * 32,
                "arguments": {"action": "release", "stage": "worker"},
            },
            sink,
        )
        await spin()
        assert any(
            row.get("id") == "companion_acquire" and row.get("result") == {}
            for row in child.commands
        )
        await control.ended(worker, confirmed=True)
    finally:
        await svc.stop()
    assert svc.backup_io.budget.status()["active"] is None
    assert not svc.backup_io.sessions


async def test_supervisor_pressure_messages_do_not_renew_heartbeat(tmp_path):
    svc, launcher, clock = await make(tmp_path, config={"hang_seconds": 2})
    child = launcher.children[0][1]
    try:
        for _ in range(4):
            await child.queue.put(
                {
                    "type": "storage_pressure",
                    "seq": 3,
                    "sample": {
                        "source": "owner_control_cache",
                        "ready_wait_seconds": 0,
                        "execution_seconds": 0,
                        "finalizer_wait_seconds": 0,
                        "finalizer_capacity_blocked": False,
                    },
                }
            )
            await clock.advance_and_drain(0.6)
            await spin()
        assert child.killed
        assert svc.rows["companion_0"]["restart_reason"] == "heartbeat_timeout"
    finally:
        await svc.stop()


async def test_stop_kill_releases_budget_even_before_monitor_observes_exit(tmp_path):
    svc, launcher, clock = await make(tmp_path, config={"stop": {"timeout_seconds": 1}})
    child = launcher.children[0][1]
    original_ended = svc.backup_io.ended
    seen = []

    async def ended(session, *, confirmed):
        seen.append(confirmed)
        await original_ended(session, confirmed=confirmed)

    svc.backup_io.ended = ended
    # Hold the monitor on its current receive; the shutdown path independently
    # observes the real wait completion after kill, then cancels the monitor.
    svc.monitors["companion_0"].cancel()
    await asyncio.gather(svc.monitors["companion_0"], return_exceptions=True)
    svc.monitors["companion_0"] = asyncio.create_task(clock.sleep(100))
    session = svc.backup_io_sessions["companion_0"]
    assert svc.backup_io.sessions[session].disconnected
    # Fake child's stop is deliberately ignored; kill still confirms exit.
    original_send = child.send

    async def send(message):
        if message["type"] != "stop":
            await original_send(message)

    child.send = send
    stopping = asyncio.create_task(svc.stop_companion("companion_0"))
    try:
        await spin()
        await clock.advance_and_drain(1.1)
        await spin()
        await stopping
        assert child.killed
        assert seen == [True]
        assert not svc.backup_io.sessions
    finally:
        await svc.stop()


async def test_durable_release_failure_does_not_skip_other_children_or_daemon_cleanup(
    tmp_path, monkeypatch
):
    svc, launcher, clock = await make(tmp_path, count=2)
    failed_session = svc.backup_io_sessions["companion_0"]
    for index, cid in enumerate(svc.companions):
        await svc.backup_io.authorize_execution(
            svc.backup_io_sessions[cid], str(index + 1) * 32, process_identity()
        )
    original_ended = svc.backup_io.responsibilities.ended

    def fail_one(controller, session):
        if session == failed_session:
            raise IoBudgetError("backup_io_coordination_busy")
        return original_ended(controller, session)

    monkeypatch.setattr(svc.backup_io.responsibilities, "ended", fail_one)
    try:
        await svc.stop("cancel")
        assert all(child.exited.is_set() and not child.killed for _, child in launcher.children)
        assert all(task.done() for task in svc.monitors.values())
        assert svc.ticker.done() and svc.lock is None
        assert svc.rows["companion_0"]["state"] == "stopped"
        assert svc.rows["companion_0"]["last_heavy_io_release"] == {
            "state": "unverified",
            "reason": "backup_io_coordination_busy",
        }
        events = [json.loads(line) for line in svc.audit.path.read_text().splitlines()]
        assert any(row["operation"] == "stop" for row in events)
        assert any(
            row["operation"] == "backup_io_release_unverified"
            and row["reason"] == "backup_io_coordination_busy"
            and row["result"] == "failed"
            for row in events
        )
        with file_lock(
            tmp_path / "run/supervisor.lock",
            {**process_identity(), "operation": "fictional_lock_verification"},
        ):
            pass
        assert (await IoControl(clock, home=tmp_path).recheck())["state"] == "blocked"
    finally:
        for _, child in launcher.children:
            if not child.exited.is_set():
                await child.kill()
            await child.wait()
        for task in (*svc.monitors.values(), svc.ticker):
            task.cancel()
        await asyncio.gather(*svc.monitors.values(), svc.ticker, return_exceptions=True)
        if svc.lock is not None:
            svc.lock.__exit__(None, None, None)
            svc.lock = None
