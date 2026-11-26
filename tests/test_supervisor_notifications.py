"""B10 的通知冷却、静音与测试通知使用模拟时钟和假后端。"""

import asyncio
import json

from clytie.plugin_host.support import HostLog
from clytie.sdk.testing import MockClock
from clytie.sdk.types import Receipt
from clytie.supervisor.common import Audit
from clytie.supervisor.notifications import Notifications

from .test_supervisor_runtime import FakeNotifier


async def test_muted_failure_remains_visible_without_retry(tmp_path):
    clock, log = MockClock(), HostLog()
    audit, target = Audit(tmp_path, clock), FakeNotifier(clock, False)
    n = Notifications(tmp_path, clock, log, audit, [(target, False)])
    problem = {"backup_key:instance": "backup_key_missing"}
    await n.update(problem)
    assert n.undelivered == 1
    n = Notifications(tmp_path, clock, log, audit, [(target, False)])
    n.mute = {"backup_key"}
    assert n.undelivered == 0
    clock.advance(600)
    await n.update(problem)
    assert n.problems == problem and n.is_muted("backup_key:instance")
    assert len(target.calls) == 1 and n.undelivered == 0


async def test_platform_offline_repeat_and_recovery_survive_restart(tmp_path):
    clock, log = MockClock(), HostLog()
    audit, target = Audit(tmp_path, clock), FakeNotifier(clock)
    n = Notifications(tmp_path, clock, log, audit, [(target, False)])
    offline = "platform_offline:companion_a:fake-channel"
    blocked = "blocked:companion_a:config_invalid"
    problems = {offline: "15 分钟", blocked: "config_invalid"}
    await n.update(problems)
    n = Notifications(tmp_path, clock, log, audit, [(target, False)])
    await n.update({})  # 重启后的第一个健康汇总还没有离线问题。
    clock.advance(3 * 3600 - 1)
    await n.update(problems)
    assert len(target.calls) == 2
    clock.advance(1)
    problems[offline] = "195 分钟"
    await n.update(problems)
    assert [notice.body for notice in target.calls] == ["15 分钟", "config_invalid", "195 分钟"]
    n.platform_online(offline)
    n = Notifications(tmp_path, clock, log, audit, [(target, False)])
    clock.advance(900)
    problems[offline] = "15 分钟"
    await n.update(problems)
    assert [notice.dedupe_key for notice in target.calls] == [offline, blocked, offline, offline]


async def test_online_during_delivery_does_not_reinstate_offline_cooldown(tmp_path):
    clock = MockClock()
    ready, finish = asyncio.Event(), asyncio.Event()

    class SlowNotifier(FakeNotifier):
        async def notify(self, notice, cancel):
            ready.set()
            await finish.wait()
            return await super().notify(notice, cancel)

    fallback = FakeNotifier(clock)
    targets = [(SlowNotifier(clock, False), False), (fallback, False)]
    n = Notifications(tmp_path, clock, HostLog(), Audit(tmp_path, clock), targets)
    key = "platform_offline:companion_a:fake-channel"
    update = asyncio.create_task(n.update({key: "15 分钟"}))
    await ready.wait()
    try:
        n.platform_online(key)
    finally:
        finish.set()
        await update
    assert key not in json.loads(n.path.read_text())
    assert not fallback.calls


async def test_notify_test_reports_all_plugins_bypassing_cooldown_and_mute(tmp_path):
    clock = MockClock()
    audit = Audit(tmp_path, clock)
    sent, failed = FakeNotifier(clock), FakeNotifier(clock, False)

    class UnknownNotifier:
        async def notify(self, notice, cancel):
            return Receipt(intent_id="test-notice", status="unknown", at=clock.now())

    n = Notifications(tmp_path, clock, HostLog(), audit, [(sent, False)])
    n.by_id = {
        "fake-desktop": (sent, False),
        "fake-outward": (failed, True),
        "fake-unknown": (UnknownNotifier(), False),
        "fake-start-failure": (None, False),
    }
    await n.update({"blocked:companion_a:config_invalid": "companion_a config_invalid"})
    cooldown = n.states.copy()
    n.mute = {"blocked"}
    result = await n.notify_test()
    assert result["results"] == {
        "fake-desktop": "sent",
        "fake-outward": "failed",
        "fake-unknown": "unknown",
        "fake-start-failure": "failed",
    }
    n.silenced = True
    result = await n.notify_test()
    assert "fake-outward" not in result["results"] and len(failed.calls) == 1
    assert len(sent.calls) == 3 and sent.calls[-1].title == "Clytie"
    assert sent.calls[-1].body == "这是一条测试通知。看到它，说明桌面通知可以正常显示。"
    assert n.undelivered == 0 and n.states == cooldown
    entries = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert [e["operation"] for e in entries] == [
        "notification",
        "notification_test",
        "notification_test",
    ]


async def test_notify_test_without_plugins_is_audited(tmp_path):
    clock = MockClock()
    audit = Audit(tmp_path, clock)
    n = Notifications(tmp_path, clock, HostLog(), audit)
    assert (await n.notify_test())["status"] == "no_notifier"
    entry = json.loads(audit.path.read_text())
    assert (entry["operation"], entry["result"]) == ("notification_test", "no_notifier")
    assert n.undelivered == 0
