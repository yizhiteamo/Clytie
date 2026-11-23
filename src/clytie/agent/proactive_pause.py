"""每个伴侣的主动聊天开关。状态文件同时供本体和管理接口读取。"""

import asyncio
import json
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from clytie.backup.coordination import companion_coordinator
from clytie.backup.old_instances import startup_gate
from clytie.plugin_host.locking import file_lock
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained, identifier
from clytie.plugin_host.support import finish_cleanup


class ProactivePause:
    def __init__(self, home, cid, clock):
        self.home, self.cid, self.clock = Path(home), identifier(cid), clock
        self.base = self.home / "companions" / self.cid

    def _read(self, path):
        try:
            value = json.loads(atomic_read(path))
        except FileNotFoundError:
            return {"paused": False, "since": None}
        if not isinstance(value, dict) or type(value.get("paused")) is not bool:
            raise ValueError("invalid_proactive_state")
        paused, since = value["paused"], value["since"]
        if paused:
            if not isinstance(since, str) or datetime.fromisoformat(since).utcoffset() is None:
                raise ValueError("invalid_proactive_state")
        elif since is not None:
            raise ValueError("invalid_proactive_state")
        return {"paused": paused, "since": since}

    def _status(self):
        return self._read(contained(self.home, self.base / "state/proactive.json"))

    async def status(self):
        return await asyncio.to_thread(self._status)

    def _set(self, paused, stopped):
        with ExitStack() as guards:
            if stopped:
                # Check actual runtime ownership while excluding a new startup.
                guards.enter_context(startup_gate(self.home))
                path = contained(self.home, self.base / "ledger/.lock")
                guards.enter_context(file_lock(path, {"operation": "proactive_pause"}))
            guards.enter_context(companion_coordinator(self.home, self.cid, self.clock).commit())
            path = contained(self.home, self.base / "state/proactive.json")
            previous = self._read(path)
            if previous["paused"] == paused:
                return previous
            value = {"paused": paused, "since": self.clock.now().isoformat() if paused else None}
            atomic_write(path, canonical(value), retry_sharing=True)
            return value

    async def set(self, paused, *, stopped=False):
        if type(paused) is not bool:
            raise ValueError("invalid_request")
        value, cancelled = await finish_cleanup(asyncio.to_thread(self._set, paused, stopped))
        if cancelled:
            raise asyncio.CancelledError
        return value
