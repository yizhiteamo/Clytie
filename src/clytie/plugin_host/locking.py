"""稳定文件上的非阻塞 OS 锁；持有者诊断信息不参与存活判定。"""

import asyncio
import json
import os
import sys
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .snapshots import canonical, contained, identifier

_guard = threading.Lock()
_held = {}


class OperationBusy(ValueError):
    def __init__(self, owner):
        self.owner = owner
        result = self.result()
        super().__init__(f"{result['outcome']}: {result['operation']}")

    def result(self, *, multiple=False):
        operation = self.owner.get("operation", "unknown")
        migration = operation in {"migration", "migrating", "recovery", "restoring"}
        return {
            "outcome": "migration_in_progress"
            if migration and not multiple
            else "operation_in_progress",
            "operation": operation,
        }


def process_identity():
    """从 OS 读取进程创建时间，不用应用时钟推测进程存活。"""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(
            kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        started = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
    elif sys.platform.startswith("linux"):
        fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        boot = next(
            line.split()[1]
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        started = datetime.fromtimestamp(int(boot), timezone.utc) + timedelta(
            seconds=ticks / os.sysconf("SC_CLK_TCK")
        )
    else:
        raise RuntimeError("process identity currently supports Windows and Linux")
    return {"pid": os.getpid(), "started_at": started.isoformat()}


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return threading.get_ident()


def _os_lock(descriptor, acquire):
    if sys.platform == "win32":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


def _owner(descriptor):
    try:
        os.lseek(descriptor, 1, os.SEEK_SET)
        value = json.loads(os.read(descriptor, 4096))
        return value if isinstance(value, dict) else {"operation": "unknown"}
    except (OSError, ValueError):
        return {"operation": "unknown"}


@contextmanager
def file_lock(path, owner, log=None):
    path = Path(path)
    key = str(path.resolve()).casefold() if sys.platform == "win32" else str(path.resolve())
    with _guard:
        if key in _held:
            raise OperationBusy(_held[key][1])
        _held[key] = (_task(), owner)
    descriptor = None
    locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        os.set_inheritable(descriptor, False)
        try:
            _os_lock(descriptor, True)
        except OSError:
            raise OperationBusy(_owner(descriptor)) from None
        locked = True
        previous = _owner(descriptor)
        if previous.get("pid") and not previous.get("released") and log:
            log.info("plugin.lock_taken_over", previous=previous)
        os.lseek(descriptor, 0, os.SEEK_SET)
        value = b"\0" + canonical(owner)
        os.write(descriptor, value)
        os.ftruncate(descriptor, len(value))
        os.fsync(descriptor)
        yield
    finally:
        try:
            if locked:
                os.lseek(descriptor, 1, os.SEEK_SET)
                value = canonical({**owner, "released": True})
                os.write(descriptor, value)
                os.ftruncate(descriptor, 1 + len(value))
                _os_lock(descriptor, False)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with _guard:
                _held.pop(key, None)


class Operations:
    def __init__(self, home, log):
        self.home, self.log = home, log
        self.executor = process_identity()
        self.owners = {}

    @contextmanager
    def hold(self, cid, plugins, operation):
        with ExitStack() as stack:
            keys = [(identifier(cid), identifier(pid)) for pid in sorted(set(plugins))]
            for companion, plugin in keys:
                path = contained(
                    self.home,
                    self.home / "companions" / companion / "plugin-recovery" / (plugin + ".lock"),
                )
                stack.enter_context(
                    file_lock(path, {**self.executor, "operation": operation}, self.log)
                )
            for key in keys:
                self.owners[key] = _task()
            try:
                yield
            finally:
                for key in keys:
                    self.owners.pop(key, None)

    def require(self, cid, pid):
        if self.owners.get((cid, pid)) != _task():
            raise RuntimeError("plugin operation requires its lock")

    def metadata(self, cid):
        path = contained(
            self.home, self.home / "companions" / identifier(cid) / "plugin-versions.lock"
        )
        return file_lock(path, {**self.executor, "operation": "version_record"}, self.log)
