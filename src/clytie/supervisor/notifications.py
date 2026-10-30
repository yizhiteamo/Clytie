"""固定结构通知与跨重启冷却；没有正文持久化。"""

import asyncio
from datetime import datetime
from pathlib import Path

from clytie.plugin_host.support import timed
from clytie.sdk import CancelToken
from clytie.sdk.types import Notice

from .common import read, write


class Notifications:
    def __init__(self, home, clock, log, audit, plugins=(), cooldown_hours=6):
        self.path = Path(home) / "supervisor/notify-state.json"
        self.clock, self.log, self.audit = clock, log, audit
        self.plugins = list(plugins)
        self.cooldown = cooldown_hours * 3600
        self.states = read(self.path, {})
        self.problems = {}
        self.silenced = False
        self._lock = asyncio.Lock()

    @property
    def undelivered(self):
        return sum(v["state"] == "failed" for v in self.states.values())

    async def update(self, problems, *, pending=()):
        async with self._lock:
            await self._update(dict(problems), set(pending))

    async def _update(self, problems, pending):
        self.problems = dict(problems)
        for key in tuple(self.states):
            if (
                (self.states[key]["state"] == "failed" or key.startswith("startup_stalled:"))
                and key not in problems
                and key.split(":")[1] not in pending
            ):
                self.states.pop(key)
        for key, body in problems.items():
            previous = self.states.get(key)
            if previous:
                if key.startswith("startup_stalled:") and previous["state"] == "cooldown":
                    continue
                elapsed = (
                    self.clock.now() - datetime.fromisoformat(previous["at"])
                ).total_seconds()
                if elapsed < (self.cooldown if previous["state"] == "cooldown" else 600):
                    continue
            sent = False
            notice = Notice(level="warn", title="Clytie", body=body, dedupe_key=key)
            for notifier, outward in self.plugins:
                if outward and self.silenced:
                    continue
                try:
                    result = await timed(notifier.notify(notice, CancelToken()), self.clock, 10)
                    if result.status == "sent":
                        sent = True
                        break
                except Exception:
                    pass
            self.states[key] = {
                "state": "cooldown" if sent else "failed",
                "at": self.clock.now().isoformat(),
            }
            if not sent:
                self.log.warning("supervisor.notification_failed", key=key)
            self.audit.append("notification", result="sent" if sent else "failed", reason=key)
        write(self.path, self.states)
