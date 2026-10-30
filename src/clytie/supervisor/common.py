"""状态持久化与可取消关闭结果。"""

import json
from pathlib import Path

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
    try:
        return json.loads(Path(path).read_bytes())
    except FileNotFoundError:
        return default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, canonical(value))


class Audit:
    def __init__(self, home, clock):
        self.path = Path(home) / "supervisor/audit.jsonl"
        self.clock = clock
        self.on_entry = None

    def append(self, operation, cid=None, result="ok", reason=None, source="internal"):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = dict(
            at=self.clock.now().isoformat(),
            operation=operation,
            companion_id=cid,
            result=result,
            reason=reason,
            source=source,
        )
        with self.path.open("ab") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
        if self.on_entry:
            self.on_entry(value)
