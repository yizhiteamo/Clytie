"""A4.1：MockClock、假启动器和无外部副作用的停止验收。"""

import asyncio

import pytest

from clytie.plugin_host.locking import OperationBusy
from clytie.sdk import ChannelState, ChannelUnavailable, PluginUnavailable
from clytie.sdk.channel import channel_state
from clytie.sdk.testing import MockClock
from clytie.supervisor.common import ShutdownCancelled, read
from clytie.supervisor.companion import startup_reason
from clytie.supervisor.service import FATAL, Supervisor


async def spin():
    for _ in range(40):
        await MockClock().sleep(0)


class FakeChild:
    def __init__(self, *, stop=True, ack=True):
        self.queue = asyncio.Queue()
        self.commands = []
        self.exited = asyncio.Event()
        self.code = None
        self.auto_stop, self.ack = stop, ack
        self.killed = False

    async def send(self, msg):
        self.commands.append(msg)
        if msg["type"] == "stop" and self.auto_stop:
            await self.queue.put(
                dict(
                    type="stop_result",
                    seq=1,
                    result=dict(
                        state="stopped", stopped=["fake-plugin"], not_stopped={}, reason=None
                    ),
                )
            )
            await self.exit(0)
        if msg["type"] == "silence" and self.ack:
            await self.queue.put(dict(type="silence_ack", seq=msg["seq"]))

    async def receive(self):
        return await self.queue.get()

    async def wait(self):
        await self.exited.wait()
        return self.code

    async def exit(self, code):
        self.code = code
        await self.queue.put(None)
        self.exited.set()

    async def kill(self):
        self.killed = True
        await self.exit(1)

    async def ready(self):
        await self.queue.put(dict(type="ready", seq=1))

    async def heartbeat(self, **fields):
        await self.queue.put(dict(type="heartbeat", seq=2, **fields))


class FakeLauncher:
    def __init__(self, **options):
        self.children = []
        self.options = options

    async def launch(self, cid):
        child = FakeChild(**self.options)
        self.children.append((cid, child))
        return child


async def make(tmp_path, *, config=None, count=1, **options):
    clock = MockClock()
    launcher = FakeLauncher(**options)
    svc = Supervisor(
        tmp_path, [f"companion_{i}" for i in range(count)], launcher, clock, config=config
    )
    await svc.start()
    await spin()
    for _, child in launcher.children:
        await child.ready()
    await spin()
    return svc, launcher, clock


async def test_single_instance_lock_and_status(tmp_path):
    svc, launcher, clock = await make(tmp_path)
    other = Supervisor(tmp_path, [], launcher, clock)
    with pytest.raises(OperationBusy):
        await other.start()
    await svc.stop()
    assert (tmp_path / "run/supervisor.lock").exists()
    await other.start()
    await other.stop()


async def test_backoff_failure_limit_and_stability(tmp_path):
    svc, launcher, clock = await make(tmp_path)
    for i, delay in enumerate([10, 30, 60, 120]):
        await launcher.children[-1][1].exit(1)
        await spin()
        assert svc.rows["companion_0"]["state"] == "backoff"
        clock.advance(delay - 1)
        await spin()
        assert len(launcher.children) == i + 1
        clock.advance(1)
        await spin()
        await launcher.children[-1][1].ready()
        await spin()
    await launcher.children[-1][1].exit(1)
    await spin()
    assert svc.rows["companion_0"]["state"] == "restart_stopped"
    assert "restart_stopped:companion_0" in svc.problems
    await svc.restart("companion_0")
    await spin()
    await launcher.children[-1][1].ready()
    await spin()
    svc.rows["companion_0"]["failures"] = 3
    for _ in range(60):
        clock.advance(10)
        await launcher.children[-1][1].heartbeat()
        await spin()
    assert svc.rows["companion_0"]["failures"] == 0
    await svc.stop()


@pytest.mark.parametrize("reason", sorted(FATAL))
async def test_fatal_blocked(tmp_path, reason):
    svc, launcher, clock = await make(tmp_path)
    child = launcher.children[0][1]
    await child.queue.put(dict(type="fatal", seq=2, reason=reason))
    await child.exit(1)
    await spin()
    assert svc.rows["companion_0"]["state"] == "blocked"
    clock.advance(600)
    await spin()
    assert len(launcher.children) == 1
    await svc.stop()


async def test_hang_and_silence_ack_timeout(tmp_path):
    svc, launcher, clock = await make(tmp_path, ack=False)
    task = asyncio.create_task(svc.silence(True))
    await spin()
    clock.advance(5)
    await spin()
    assert not task.done()
    await launcher.children[-1][1].ready()
    await spin()
    await task
    assert launcher.children[0][1].killed
    assert read(tmp_path / "supervisor/silence.json")["on"]
    await spin()
    clock.advance(30)
    await spin()
    assert launcher.children[-1][1].killed
    await svc.stop()


async def test_health_auth_quota_unknown_protocol(tmp_path):
    svc, launcher, clock = await make(tmp_path)
    child = launcher.children[0][1]
    await child.queue.put(dict(type="fictional_unknown", seq=1))
    await child.heartbeat(
        model="auth_invalid",
        channels={"fake": "auth_invalid"},
        quota_exhausted=True,
        sending=2,
        inflight_tasks=1,
    )
    await spin()
    clock.advance(5)
    await spin()
    value = read(tmp_path / "run/health.json")
    assert value["companions"]["companion_0"]["state"] == "degraded"
    assert value["companions"]["companion_0"]["sending"] == 2
    assert len(launcher.children) == 1
    assert any(entry[1] == "supervisor.unknown_type" for entry in svc.log.entries)
    await svc.stop()


async def test_shutdown_cancel_preserves_all_results(tmp_path):
    svc, launcher, clock = await make(tmp_path, count=2, stop=False)
    task = asyncio.create_task(svc.stop())
    await spin()
    task.cancel()
    await spin()
    assert not task.done()
    for _, child in launcher.children:
        await child.queue.put(
            dict(
                type="stop_result",
                seq=2,
                result=dict(state="stopped", stopped=["fake"], not_stopped={}, reason=None),
            )
        )
        await child.exit(0)
    with pytest.raises(ShutdownCancelled) as info:
        await task
    assert set(info.value.result) == {"companion_0", "companion_1"}
    assert all(r["stopped"] == ["fake"] for r in info.value.result.values())


async def test_stop_timeout_and_session_ending_order(tmp_path):
    svc, launcher, clock = await make(tmp_path, stop=False)
    task = asyncio.create_task(svc.session_ending())
    await spin()
    assert [m["type"] for m in launcher.children[0][1].commands] == ["session_ending", "stop"]
    assert "session_ending" in (tmp_path / "supervisor/audit.jsonl").read_text()
    clock.advance(120)
    await spin()
    result = await task
    assert result["companion_0"]["reason"] == "timeout_killed"


async def test_wait_escalation_timeout(tmp_path):
    svc, launcher, clock = await make(tmp_path, stop=False)
    task = asyncio.create_task(svc.stop("wait"))
    await spin()
    for _ in range(12):
        clock.advance(10)
        await launcher.children[0][1].heartbeat()
        await spin()
    assert not task.done()
    await svc.escalate()
    await spin()
    for _ in range(12):
        clock.advance(10)
        await launcher.children[0][1].heartbeat()
        await spin()
    assert (await task)["companion_0"]["reason"] == "timeout_killed"


def test_sdk_optional_state_and_startup_codes():
    assert channel_state(object()) == ChannelState()
    assert isinstance(ChannelUnavailable("auth_invalid"), PluginUnavailable)
    assert startup_reason("auth_invalid") == "auth_invalid"
    assert startup_reason("quota_exhausted") == "quota_exhausted"
    assert startup_reason("transient") is None
    assert startup_reason(None) is None
    assert startup_reason("fictional_code") == "required_plugin_failed"


async def test_status_stopping_uses_lock_metadata_without_health_rewrite(tmp_path):
    from clytie.supervisor.entry import status

    svc, launcher, clock = await make(tmp_path, stop=False)
    await svc.flush_health()
    before = (tmp_path / "run/health.json").read_bytes()
    closing = asyncio.create_task(svc.stop())
    await spin()
    assert status(tmp_path)["health"]["state"] == "stopping"
    assert (tmp_path / "run/health.json").read_bytes() == before
    clock.advance(120)
    await spin()
    await closing
    assert status(tmp_path)["stale"] and not status(tmp_path)["running"]


def test_process_creation_and_session_adapter(monkeypatch):
    import subprocess

    from clytie.supervisor import processes, signals

    monkeypatch.setattr(processes.sys, "platform", "linux")
    assert processes.creation(detached=True) == {"start_new_session": True}
    assert processes.creation() == {"start_new_session": True}
    monkeypatch.setattr(processes.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 1024, raising=False)
    assert processes.creation(detached=True)["creationflags"] == 8
    assert processes.creation()["creationflags"] == 512 | 1024
    handler = signals.SessionEvents.__new__(signals.SessionEvents)
    called = []
    handler.callback = lambda: called.append("session_ending")
    handler.notify()
    assert called == ["session_ending"]


async def test_backoff_reuses_final_delay_and_finalizes_before_unlock(tmp_path):
    from clytie.supervisor.entry import status

    svc, launcher, clock = await make(tmp_path, config={"restart": {"max_failures": 7}})
    for delay in (10, 30, 60, 120, 300, 300):
        count = len(launcher.children)
        await launcher.children[-1][1].exit(1)
        await spin()
        clock.advance(delay - 1)
        await spin()
        assert len(launcher.children) == count
        clock.advance(1)
        await spin()
        await launcher.children[-1][1].ready()
        await spin()
    finalized = []

    async def finalize():
        assert status(tmp_path)["running"]
        finalized.append(True)

    svc.finalize = finalize
    await svc.stop()
    assert finalized == [True]
    assert not status(tmp_path)["running"]


async def test_external_fake_summary_and_no_message_content(tmp_path):
    class External:
        async def status(self):
            return {"id": "fake-external", "state": "offline"}

        async def start(self):
            pass

        async def stop(self):
            pass

    svc, launcher, clock = await make(tmp_path)
    svc.external = [External()]
    await launcher.children[-1][1].heartbeat(model="unknown", content="fictional-sensitive-content")
    await spin()
    clock.advance(5)
    await svc.flush_health()
    value = read(tmp_path / "run/health.json")
    assert value["external"] == [{"id": "fake-external", "state": "offline"}]
    assert "fictional-sensitive-content" not in str(value)
    assert "fictional-sensitive-content" not in str(svc.problems)
    await svc.stop()


async def test_stop_uses_total_budget_not_heartbeat_budget(tmp_path):
    svc, launcher, clock = await make(tmp_path, stop=False)
    closing = asyncio.create_task(svc.stop())
    await spin()
    clock.advance(30)
    await spin()
    assert not launcher.children[-1][1].killed
    assert not closing.done()
    clock.advance(90)
    await spin()
    assert (await closing)["companion_0"]["reason"] == "timeout_killed"


async def test_health_file_write_failure_does_not_end_monitoring(tmp_path, monkeypatch):
    import clytie.supervisor.service as service

    svc, _, clock = await make(tmp_path)
    original = service.write
    attempts = []

    def busy_once(path, value):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError("fictional sharing violation")
        original(path, value)

    monkeypatch.setattr(service, "write", busy_once)
    try:
        clock.advance(5)
        await spin()
        assert len(attempts) == 1
        assert not svc.ticker.done()
        clock.advance(1)
        await spin()
        assert len(attempts) == 2
        assert read(tmp_path / "run/health.json")["companions"]["companion_0"]["state"] == "running"
    finally:
        await svc.stop()


@pytest.mark.parametrize("session", [False, True])
async def test_terminal_stop_audit_source(tmp_path, session):
    import json

    svc, _, _ = await make(tmp_path)
    if session:
        await svc.session_ending(source="terminal")
    else:
        await svc.stop(source="terminal")
    rows = [
        json.loads(line) for line in (tmp_path / "supervisor/audit.jsonl").read_text().splitlines()
    ]
    actions = {"stop", "session_ending"} if session else {"stop"}
    selected = [r for r in rows if r["operation"] in actions]
    assert len(selected) == len(actions)
    assert all(r["source"] == "terminal" for r in selected)


def test_status_chinese_counts_and_pending_migration(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    import clytie.api
    from clytie.supervisor.entry import run

    monkeypatch.setattr(
        clytie.api,
        "local_supervisor_status",
        lambda home: {
            "running": True,
            "health": {
                "undelivered": 2,
                "companions": {
                    "companion_a": {
                        "state": "stopping",
                        "sending": 3,
                        "inflight_tasks": 1,
                        "silence_status": "pending_migrating",
                        "migration_stalled": True,
                    }
                },
            },
        },
    )
    assert run(Namespace(action="status", home=tmp_path, json=False)) == 0
    output = capsys.readouterr().out
    assert "2 条通知未送达" in output
    assert "还有 3 条消息正在发送 / 1 个任务在执行" in output
    assert "静默待确认（迁移中，尚未连接平台）" in output
    assert "startup_stalled" in output
