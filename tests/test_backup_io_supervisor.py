import asyncio

from tests.test_supervisor import make, spin


async def test_supervisor_routes_budget_without_blocking_health_or_stop(tmp_path):
    svc, launcher, clock = await make(tmp_path)
    child = launcher.children[0][1]
    try:
        control = svc.backup_io
        worker = control.register()
        control.authorize(worker, "backup_a")

        async def sink(reply):
            assert reply.get("result") == {}

        await control.dispatch(
            worker,
            {
                "type": "backup_io",
                "id": "worker_acquire",
                "operation_id": "backup_a",
                "arguments": {"action": "acquire", "stage": "worker"},
            },
            sink,
        )
        svc.authorize_backup_capture("companion_0", "backup_a")
        await child.queue.put(
            {
                "type": "backup_io",
                "seq": 3,
                "id": "companion_acquire",
                "operation_id": "backup_a",
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
                "operation_id": "backup_a",
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
