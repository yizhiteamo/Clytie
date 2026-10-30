from clytie.sdk.testing import MockClock
from clytie.supervisor.heartbeat import Heartbeat
from clytie.supervisor.service import Supervisor

from .test_supervisor import FakeLauncher, spin


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
