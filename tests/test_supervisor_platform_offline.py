"""B10：用假心跳、假通知插件和模拟时钟验证平台离线提醒。"""

import asyncio
from datetime import timedelta

import pytest

from clytie.sdk.testing import MockClock
from clytie.supervisor.common import read
from clytie.supervisor.service import Supervisor

from .test_supervisor import FakeLauncher, make, spin
from .test_supervisor_runtime import FakeNotifier


def attach_notifier(svc):
    notifier = FakeNotifier(svc.clock)
    svc.notifications.plugins = [(notifier, False)]
    return notifier


async def heartbeat(child, channels, **fields):
    await child.heartbeat(channels=channels, **fields)
    await spin()


async def advance_offline(clock, child, seconds):
    while seconds:
        step = min(600, seconds)
        clock.advance(step)
        await heartbeat(child, {"fake-channel": "offline"})
        seconds -= step


async def test_platform_offline_threshold_repeat_duration_and_online_recovery(tmp_path):
    svc, launcher, clock = await make(tmp_path, config={"hang_seconds": 600})
    child = launcher.children[0][1]
    notifier = attach_notifier(svc)
    key = "platform_offline:companion_0:fake-channel"
    since = clock.now().isoformat()
    try:
        await heartbeat(
            child,
            {"fake-channel": "connecting"},
            account="fictional-account",
            group="fictional-group",
            content="fictional-message",
        )
        clock.advance(600)
        await heartbeat(child, {"fake-channel": "unknown"})
        await advance_offline(clock, child, 299)
        assert not notifier.calls
        row = read(tmp_path / "run/health.json")["companions"]["companion_0"]
        assert row["channel_offline_since"] == {"fake-channel": since}
        await advance_offline(clock, child, 1)
        assert [n.dedupe_key for n in notifier.calls] == [key]
        assert notifier.calls[0].body == (
            "companion_0 的平台 fake-channel 已离线 15 分钟。"
            "消息发不出去，请检查协议端是否在运行、账号是否仍然登录。"
        )
        await advance_offline(clock, child, 3 * 3600 - 1)
        assert len(notifier.calls) == 1
        await advance_offline(clock, child, 1)
        assert [n.dedupe_key for n in notifier.calls] == [key, key]
        assert "已离线 195 分钟" in notifier.calls[-1].body
        await heartbeat(child, {"fake-channel": "online"})
        assert svc.rows["companion_0"]["channel_offline_since"] == {}
        clock.advance(1)
        await spin()
        assert key not in read(tmp_path / "supervisor/notify-state.json")
        assert len(notifier.calls) == 2
        await heartbeat(child, {"fake-channel": "offline"})
        await advance_offline(clock, child, 899)
        assert len(notifier.calls) == 2
        await advance_offline(clock, child, 1)
        assert [n.dedupe_key for n in notifier.calls] == [key] * 3
    finally:
        await svc.stop()


@pytest.mark.parametrize("reported_during_start", [False, True])
async def test_initial_offline_interval_starts_at_ready(tmp_path, reported_during_start):
    clock, launcher = MockClock(), FakeLauncher()
    svc = Supervisor(tmp_path, ["companion_a"], launcher, clock, config={"hang_seconds": 600})
    await svc.start()
    await spin()
    child = launcher.children[0][1]
    notifier = attach_notifier(svc)
    channels = {"fake-channel": "offline"} if reported_during_start else {}
    try:
        await heartbeat(child, channels, phase="migrating")
        clock.advance(100)
        await heartbeat(child, channels, phase="starting")
        row = svc.rows["companion_a"]
        assert row["channel_offline_since"] == {}
        since = clock.now().isoformat()
        await child.ready()
        await spin()
        clock.advance(5)
        await heartbeat(child, {"fake-channel": "offline"})
        assert row["channel_offline_since"] == {"fake-channel": since}
        await advance_offline(clock, child, 894)
        assert not notifier.calls
        await advance_offline(clock, child, 1)
        assert [n.dedupe_key for n in notifier.calls] == [
            "platform_offline:companion_a:fake-channel"
        ]
    finally:
        await svc.stop()


@pytest.mark.parametrize("monotonic_pauses", [False, True])
async def test_sleep_gap_restarts_timer_without_alert_after_wake(
    tmp_path, monkeypatch, monotonic_pauses
):
    svc, launcher, clock = await make(tmp_path, config={"hang_seconds": 600})
    child = launcher.children[0][1]
    notifier = attach_notifier(svc)
    try:
        await heartbeat(child, {"fake-channel": "offline"})
        await advance_offline(clock, child, 899)
        if monotonic_pauses:
            awake_now = clock.now
            monkeypatch.setattr(clock, "now", lambda: awake_now() + timedelta(hours=1))
            clock.advance(1)
        else:
            clock.advance(3600)
        await heartbeat(child, {"fake-channel": "offline"})
        assert not child.killed and not notifier.calls
        assert svc.rows["companion_0"]["channel_offline_since"] == {
            "fake-channel": clock.now().isoformat()
        }
        await advance_offline(clock, child, 899)
        assert not notifier.calls
        await advance_offline(clock, child, 1)
        assert len(notifier.calls) == 1
    finally:
        await svc.stop()


async def test_platforms_and_companions_time_independently_in_degraded_state(tmp_path):
    svc, launcher, clock = await make(tmp_path, count=2, config={"hang_seconds": 600})
    a, b = (child for _, child in launcher.children)
    notifier = attach_notifier(svc)
    try:
        await heartbeat(a, {"first": "offline", "second": "offline"}, model="auth_invalid")
        await heartbeat(b, {"first": "online"})
        clock.advance(500)
        await heartbeat(a, {"first": "online", "second": "unknown"}, model="auth_invalid")
        await heartbeat(b, {"first": "connecting"})
        clock.advance(400)
        await spin()
        assert svc.rows["companion_0"]["state"] == "degraded"
        assert [n.dedupe_key for n in notifier.calls if n.dedupe_key.startswith("platform_")] == [
            "platform_offline:companion_0:second"
        ]
        await heartbeat(a, {"first": "offline", "second": "auth_invalid"})
        await heartbeat(b, {"first": "offline"})
        clock.advance(500)
        await spin()
        assert "platform_offline:companion_0:second" not in svc.problems
        assert "auth_invalid:companion_0:second" in svc.problems
        assert [n.dedupe_key for n in notifier.calls if n.dedupe_key.startswith("platform_")] == [
            "platform_offline:companion_0:second",
            "platform_offline:companion_1:first",
        ]
    finally:
        await svc.stop()


@pytest.mark.parametrize("transition", ["auth_invalid", "stop"])
async def test_pending_offline_dispatch_dropped_when_channel_no_longer_qualifies(
    tmp_path, transition
):
    svc, launcher, clock = await make(tmp_path, config={"hang_seconds": 600})
    child = launcher.children[0][1]
    entered, release = asyncio.Event(), asyncio.Event()
    other_key = "backup_key:instance"

    class PausingNotifier(FakeNotifier):
        async def notify(self, notice, cancel):
            if notice.dedupe_key == other_key:
                entered.set()
                await release.wait()
            return await super().notify(notice, cancel)

    notifier = PausingNotifier(clock)
    svc.notifications.plugins = [(notifier, False)]
    try:
        await heartbeat(child, {"fake-channel": "offline"})
        await advance_offline(clock, child, 899)
        svc.problems[other_key] = "backup_key_missing"
        clock.advance(1)
        await spin()
        assert entered.is_set()
        queued = asyncio.create_task(svc.notifications.update(svc.problems))
        await spin()
        if transition == "auth_invalid":
            await heartbeat(child, {"fake-channel": "auth_invalid"})
        else:
            await svc.stop_companion("companion_0")
        release.set()
        await queued
        assert not any(n.dedupe_key.startswith("platform_offline:") for n in notifier.calls)
        assert other_key in svc.notifications.states
    finally:
        release.set()
        await svc.stop()


@pytest.mark.parametrize("state", ["stopped", "blocked", "backoff", "restart_stopped"])
async def test_companion_exit_clears_active_platform_problem(tmp_path, state):
    config = {
        "hang_seconds": 600,
        "restart": {"max_failures": 1 if state == "restart_stopped" else 5},
    }
    svc, launcher, clock = await make(tmp_path, config=config)
    child = launcher.children[0][1]
    try:
        await heartbeat(child, {"fake-channel": "offline"})
        await advance_offline(clock, child, 900)
        key = "platform_offline:companion_0:fake-channel"
        assert key in svc.problems
        if state == "blocked":
            await child.queue.put({"type": "fatal", "reason": "required_plugin_failed"})
        await child.exit(0 if state == "stopped" else 1)
        await spin()
        assert svc.rows["companion_0"]["state"] == state
        assert not svc.rows["companion_0"]["channel_offline_since"]
        assert key not in svc.problems
    finally:
        await svc.stop()


async def test_daemon_restart_resets_timer_but_preserves_delivered_cooldown(tmp_path):
    clock = MockClock()
    for attempt in range(2):
        launcher = FakeLauncher()
        svc = Supervisor(tmp_path, ["companion_a"], launcher, clock, config={"hang_seconds": 600})
        notifier = attach_notifier(svc)
        await svc.start()
        await spin()
        child = launcher.children[0][1]
        try:
            await child.ready()
            await spin()
            since = clock.now().isoformat()
            await heartbeat(child, {"fake-channel": "offline"})
            assert svc.rows["companion_a"]["channel_offline_since"] == {"fake-channel": since}
            await advance_offline(clock, child, 899)
            assert "platform_offline:companion_a:fake-channel" not in svc.problems
            await advance_offline(clock, child, 1)
            assert "platform_offline:companion_a:fake-channel" in svc.problems
            assert len(notifier.calls) == (1 if attempt == 0 else 0)
        finally:
            await svc.stop()
        assert svc.rows["companion_a"]["channel_offline_since"] == {}
