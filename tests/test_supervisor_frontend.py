"""前端控制、关闭编排和通知集成；不注册真实信号或发送通知。"""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from clytie.supervisor.entry import FrontendCommands, FrontendSignals
from clytie.supervisor.service import FATAL

from .test_supervisor import make, spin
from .test_supervisor_runtime import FakeNotifier


def test_frontend_signal_adapters():
    stream = io.BytesIO()
    signals = FrontendSignals(stream)
    signals.interrupt()
    signals.interrupt()
    assert signals.windows(2)
    assert signals.windows(5)
    assert signals.windows(6)
    assert not signals.windows(99)
    messages = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [m["type"] for m in messages] == [
        "stop",
        "escalate",
        "stop",
        "session_ending",
        "session_ending",
    ]
    assert [m["source"] for m in messages] == [
        "interrupt",
        "interrupt",
        "close",
        "session_ending",
        "session_ending",
    ]


@pytest.mark.parametrize("ending", ["escalate", "close", "eof", "session_ending"])
async def test_frontend_interrupt_wait_then_cancel(tmp_path, ending):
    svc, launcher, clock = await make(
        tmp_path, stop=False, config={"stop": {"task_mode": "wait", "timeout_seconds": 10}}
    )
    control = FrontendCommands(svc)
    await control.receive(dict(type="stop", source="interrupt"))
    await spin()
    child = launcher.children[0][1]
    assert child.commands[-1]["task_mode"] == "wait"
    clock.advance(20)
    await spin()
    assert not control.stopping.done() and not child.killed
    msg = (
        None
        if ending == "eof"
        else dict(type="stop" if ending == "close" else ending, source=ending)
    )
    pending = asyncio.create_task(control.receive(msg))
    await spin()
    assert svc.escalation.is_set()
    clock.advance(10)
    await spin()
    await pending
    assert (await control.stopping)["companion_0"]["reason"] == "timeout_killed"
    assert child.killed


@pytest.mark.parametrize("source", ["close", "eof"])
async def test_frontend_close_ignores_configured_wait(tmp_path, source):
    svc, launcher, _ = await make(tmp_path, config={"stop": {"task_mode": "wait"}})
    control = FrontendCommands(svc)
    await control.receive(None if source == "eof" else dict(type="stop", source=source))
    await control.stopping
    assert launcher.children[0][1].commands[0]["task_mode"] == "cancel"


@pytest.mark.parametrize("stopping", [False, True])
def test_frontend_duplicate_start_waits_only_for_stopping(tmp_path, monkeypatch, capsys, stopping):
    import clytie.supervisor.entry as entry

    states = iter(
        [
            {"running": True, "health": {"state": "stopping" if stopping else "running"}},
            {"running": True, "health": {"state": "stopping" if stopping else "running"}},
            {"running": False, "health": {}},
        ]
    )
    waits, launched, registered = [], [], {}

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(entry, "SystemClock", lambda: SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(entry, "status", lambda home: next(states))
    monkeypatch.setattr(entry.sys, "platform", "linux")
    monkeypatch.setattr(entry.signal, "signal", lambda key, fn: registered.update({key: fn}))
    monkeypatch.setattr(entry, "python_process", lambda: ("fictional-python", {}))

    def popen(*args, **kwargs):
        launched.append((args, kwargs))
        return SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), wait=lambda: 0)

    monkeypatch.setattr(entry.subprocess, "Popen", popen)
    assert entry.frontend(tmp_path) == (0 if stopping else 1)
    assert len(launched) == int(stopping)
    assert waits == ([1] if stopping else [])
    assert ("请稍候" if stopping else "已在运行") in capsys.readouterr().out


@pytest.mark.parametrize(
    "reason", sorted(FATAL) + ["restart_stopped", "auth_runtime", "stop_killed"]
)
async def test_supervisor_notifications_each_problem_once(tmp_path, reason):
    svc, launcher, clock = await make(
        tmp_path, stop=reason != "stop_killed", config={"restart": {"max_failures": 1}}
    )
    notifier = FakeNotifier(clock)
    svc.notifications.plugins = [(notifier, False)]
    child = launcher.children[0][1]
    closing = None
    if reason in FATAL:
        await child.queue.put(dict(type="fatal", seq=3, reason=reason))
        await child.exit(1)
        expected = {f"blocked:companion_0:{reason}"}
    elif reason == "restart_stopped":
        await child.exit(1)
        expected = {"restart_stopped:companion_0"}
    elif reason == "auth_runtime":
        await child.heartbeat(
            phase="running",
            model="auth_invalid",
            model_plugin="fake-model",
            channels={"fake-channel": "auth_invalid"},
            quota_exhausted=True,
            content="fictional-secret-do-not-notify",
        )
        expected = {
            "auth_invalid:companion_0:fake-model",
            "auth_invalid:companion_0:fake-channel",
            "quota_exhausted:companion_0",
        }
    else:
        closing = asyncio.create_task(svc.stop(source="terminal"))
        await spin()
        clock.advance(120)
        expected = {"stop_killed:companion_0"}
    await spin()
    clock.advance(1)
    await spin()
    assert {n.dedupe_key for n in notifier.calls} == expected
    assert len(notifier.calls) == len(expected)
    assert "fictional-secret-do-not-notify" not in str(notifier.calls)
    await svc.notifications.update(svc.problems)
    assert len(notifier.calls) == len(expected)
    if closing:
        await closing
    else:
        await svc.stop()
