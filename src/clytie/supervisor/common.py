"""状态持久化与可取消关闭结果。"""

import errno
import json
from pathlib import Path
from sys import platform
from threading import Event

from clytie.clock import SystemClock
from clytie.plugin_host.snapshots import atomic_write, canonical
from clytie.plugin_host.support import finish_cleanup


class ShutdownCancelled(BaseException):
    def __init__(self, result):
        self.result = result
        super().__init__("shutdown completed after cancellation")


async def finish_shutdown(operation):
    result, cancelled = await finish_cleanup(operation)
    if cancelled:
        raise ShutdownCancelled(result)
    return result


def read(path, default=None):
    path = Path(path)
    deadline = None
    while True:
        try:
            return json.loads(path.read_bytes())
        except FileNotFoundError:
            return default
        except PermissionError as exc:
            winerror = getattr(exc, "winerror", None)
            if platform != "win32" or not (
                winerror in (5, 32) or (winerror is None and exc.errno == errno.EACCES)
            ):
                raise
            now = SystemClock().monotonic()
            if deadline is None:
                deadline = now + 0.2
            if now >= deadline:
                raise
            Event().wait(min(0.01, deadline - now))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, canonical(value))


class Audit:
    def __init__(self, home, clock, *, rotate_bytes=10 * 1024**2):
        from clytie.backup.logs import RotatingLogWriter

        self.path = Path(home) / "supervisor/audit.jsonl"
        self.writer = RotatingLogWriter(
            home, "supervisor/audit.jsonl", rotate_bytes=rotate_bytes, audit=True
        )
        self.clock = clock
        self.on_entry = None

    def append(self, operation, cid=None, result="ok", reason=None, source="internal", **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = dict(
            at=self.clock.now().isoformat(),
            operation=operation,
            companion_id=cid,
            result=result,
            reason=reason,
            source=source,
            **fields,
        )
        self.writer.write(canonical(value) + b"\n")
        if self.on_entry:
            self.on_entry(value)
