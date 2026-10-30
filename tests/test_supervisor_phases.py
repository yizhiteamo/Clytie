from clytie.sdk.testing import MockClock
from clytie.supervisor.heartbeat import Heartbeat
from clytie.supervisor.service import Supervisor

from .test_supervisor import FakeLauncher, spin


async def test_migration_silence_confirms_other_companions_and_warns_once(tmp_path):
    from .test_supervisor_runtime import FakeNotifier

    clock, launcher = MockClock(), FakeLauncher()
    svc = Supervisor(tmp_path, ["companion_a", "companion_b"], launcher, clock)
    notifier = FakeNotifier(clock)
    svc.notifications.plugins = [(notifier, False)]
    await svc.start()
    await spin()
    a, b = (child for _, child in launcher.children)
    a.ack = False
    await a.heartbeat(phase="migrating")
    await b.ready()
    await spin()
    assert await svc.silence(True) == {
        "companion_a": "pending_migrating",
        "companion_b": "confirmed",
    }
    for seconds in (31, 1, 6 * 3600 + 1, 1):
        clock.advance(seconds)
        await b.heartbeat(phase="running", silenced=True)
        await spin()
    notices = [n for n in notifier.calls if n.dedupe_key == "startup_stalled:companion_a"]
    assert len(notices) == 1 and not a.killed
    await svc.stop()


async def test_orphan_cleanup_after_startup_is_bounded(tmp_path, monkeypatch):
    import asyncio
    import io
    from types import SimpleNamespace

    import pytest

    import clytie.supervisor.entry as entry

    clock, calls = MockClock(), []

    async def stop(mode, *, orphan):
        calls.append((mode, orphan))
        await clock.sleep(100)

    def exit(code):
        raise RuntimeError(f"fake_exit_{code}")

    monkeypatch.setattr(entry.os, "_exit", exit)
    runtime = SimpleNamespace(stop=stop, session_ending=lambda: calls.append("ending"))
    task = asyncio.create_task(entry.orphan_exit(runtime, clock, io.BytesIO()))
    await spin()
    clock.advance(5)
    await spin()
    with pytest.raises(RuntimeError, match="fake_exit_0"):
        await task
    assert calls == ["ending", ("cancel", True)]


async def starting(tmp_path, *, ack=False):
    clock = MockClock()
    launcher = FakeLauncher(ack=ack)
    svc = Supervisor(
        tmp_path,
        ["companion_a"],
        launcher,
        clock,
        config={"startup_seconds": 40, "hang_seconds": 10},
    )
    await svc.start()
    await spin()
    return svc, launcher.children[0][1], clock


async def test_migration_stall_never_killed_and_silence_returns_pending(tmp_path):
    svc, child, clock = await starting(tmp_path)
    try:
        await child.heartbeat(phase="migrating")
        await spin()
        assert await svc.silence(True) == {"companion_a": "pending_migrating"}
        for _ in range(3):
            clock.advance(50)
            await spin()
        assert not child.killed
        assert svc.rows["companion_a"]["migration_stalled"]
        assert "startup_stalled:companion_a" in svc.problems
        await child.heartbeat(phase="starting", silenced=True)
        await child.ready()
        await spin()
        assert svc.rows["companion_a"]["silence_status"] == "confirmed"
    finally:
        await svc.stop()


async def test_starting_accumulates_only_nonmigration_time(tmp_path):
    svc, child, clock = await starting(tmp_path)
    try:
        for _ in range(3):
            clock.advance(8)
            await child.heartbeat(phase="starting")
            await spin()
        await child.heartbeat(phase="migrating")
        await spin()
        clock.advance(100)
        await child.heartbeat(phase="starting")
        await spin()
        assert not child.killed
        for _ in range(2):
            clock.advance(8)
            await child.heartbeat(phase="starting")
            await spin()
        assert child.killed
        assert svc.rows["companion_a"]["restart_reason"] == "startup_timeout"
    finally:
        await svc.stop()


async def test_starting_without_heartbeat_is_killed(tmp_path):
    svc, child, clock = await starting(tmp_path)
    clock.advance(10)
    await spin()
    assert child.killed
    await svc.stop()


def test_independent_heartbeat_uses_cache_and_preserves_event_loop_stall_detection():
    clock = MockClock()
    sent = []
    heartbeat = Heartbeat(clock, lambda kind, **value: sent.append((kind, value)))
    heartbeat.update({"sending": 2}, phase="migrating")
    clock.advance(100)
    assert heartbeat.pulse()
    assert sent[-1][1]["phase"] == "migrating"
    heartbeat.update(phase="running")
    clock.advance(30)
    assert not heartbeat.pulse()
