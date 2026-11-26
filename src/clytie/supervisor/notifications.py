"""固定结构通知与跨重启冷却；没有正文持久化。"""

import asyncio
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from clytie.plugin_host.support import timed
from clytie.sdk import CancelToken
from clytie.sdk.types import Notice

from .common import read, write


class Notifications:
    def __init__(self, home, clock, log, audit, plugins=(), cooldown_hours=6):
        self.path = Path(home) / "supervisor/notify-state.json"
        self.clock, self.log, self.audit = clock, log, audit
        self.plugins = list(plugins)
        self.by_id = {}
        self.cooldown = cooldown_hours * 3600
        self.offline_repeat = 3 * 3600
        self.mute = set()
        self.zone = ZoneInfo("UTC")
        self.states = read(self.path, {})
        self.problems = {}
        self.silenced = False
        self._lock = asyncio.Lock()

    @property
    def undelivered(self):
        return sum(
            state["state"] == "failed" and not self.is_muted(key)
            for key, state in self.states.items()
        )

    def is_muted(self, key):
        return key.partition(":")[0] in self.mute

    def platform_online(self, key):
        # Discard an in-flight snapshot as well as a previous delivery's cooldown.
        self.problems.pop(key, None)
        if self.states.pop(key, None) is not None:
            try:
                write(self.path, self.states)
            except OSError as exc:
                # The next notification tick retries persistence.
                self.log.warning(
                    "supervisor.notification_state_write_failed", error=type(exc).__name__
                )

    async def update(self, problems, *, pending=()):
        async with self._lock:
            await self._update(problems, set(pending))

    async def _deliver(self, notifier, notice):
        token = CancelToken()
        try:
            receipt = await timed(notifier.notify(notice, token), self.clock, 10)
            return receipt.status if receipt.status in ("sent", "failed", "unknown") else "unknown"
        except Exception:
            return "unknown"
        finally:
            token.cancel("notification_finished")

    async def _send(self, notice, *, current_problems=None):
        for notifier, outward in self.plugins:
            if current_problems is not None and (
                notice.dedupe_key not in self.problems or notice.dedupe_key not in current_problems
            ):
                break
            if outward and self.silenced:
                continue
            if await self._deliver(notifier, notice) == "sent":
                return True
        return False

    async def notify_test(self):
        if not self.by_id:
            self.audit.append("notification_test", result="no_notifier")
            return {"status": "no_notifier", "results": {}}
        notice = Notice(
            level="info",
            title="Clytie",
            body="这是一条测试通知。看到它，说明桌面通知可以正常显示。",
            dedupe_key=f"notification_test:{uuid4().hex}",
        )
        results = {}
        for pid, (notifier, outward) in self.by_id.items():
            if outward and self.silenced:
                continue
            results[pid] = "failed" if notifier is None else await self._deliver(notifier, notice)
        self.audit.append("notification_test", results=results)
        return {"results": results}

    async def safety_trip(self, cid, group, count, until):
        from clytie.agent.safety_belt import notice_body, recovery_time

        notice = Notice(
            level="warn",
            title="Clytie",
            body=notice_body(group, count, recovery_time(until, self.clock.now(), self.zone)),
            dedupe_key=f"safety_belt:{cid}:{uuid4().hex}",
        )
        if not await self._send(notice):
            self.log.warning("safety_belt.notifier_unavailable", conversation_id=group)

    async def _update(self, problems, pending):
        self.problems = dict(problems)
        for key in tuple(self.states):
            if (
                (self.states[key]["state"] == "failed" or key.startswith("startup_stalled:"))
                and key not in problems
                and key.split(":")[1] not in pending
            ):
                self.states.pop(key)
        for key, body in tuple(problems.items()):
            if self.is_muted(key) or key not in self.problems or key not in problems:
                continue
            previous = self.states.get(key)
            if previous:
                if key.startswith("startup_stalled:") and previous["state"] == "cooldown":
                    continue
                elapsed = (
                    self.clock.now() - datetime.fromisoformat(previous["at"])
                ).total_seconds()
                cooldown = (
                    self.offline_repeat if key.startswith("platform_offline:") else self.cooldown
                )
                if elapsed < (cooldown if previous["state"] == "cooldown" else 600):
                    continue
            notice = Notice(level="warn", title="Clytie", body=body, dedupe_key=key)
            sent = await self._send(notice, current_problems=problems)
            if key in self.problems and (sent or key in problems):
                self.states[key] = {
                    "state": "cooldown" if sent else "failed",
                    "at": self.clock.now().isoformat(),
                }
            if not sent:
                self.log.warning("supervisor.notification_failed", key=key)
            self.audit.append("notification", result="sent" if sent else "failed", reason=key)
        write(self.path, self.states)
