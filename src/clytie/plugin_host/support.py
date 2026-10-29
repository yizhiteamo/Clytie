"""宿主内部的持久化、日志和注入时钟超时工具。"""

import asyncio
import json
import logging
from pathlib import Path

from clytie.sdk.immutable import thaw

from .snapshots import atomic_write, contained


def json_copy(value):
    return json.loads(json.dumps(thaw(value), ensure_ascii=False, allow_nan=False))


class HostLog:
    def __init__(self):
        self.entries = []
        self._logger = logging.getLogger("clytie.plugin_host")

    def _write(self, level, msg, fields):
        fields = json_copy(fields)
        self.entries.append((level, msg, fields))
        getattr(self._logger, level)("%s %s", msg, fields)

    def debug(self, msg, **fields):
        self._write("debug", msg, fields)

    def info(self, msg, **fields):
        self._write("info", msg, fields)

    def warning(self, msg, **fields):
        self._write("warning", msg, fields)

    def error(self, msg, **fields):
        self._write("error", msg, fields)


def _consume(task):
    if not task.cancelled():
        task.exception()


async def finish_cleanup(operation):
    """即使调用方再次取消也先完成清理，调用方随后传播取消。"""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def timed(operation, clock, seconds):
    task = asyncio.ensure_future(operation)
    timer = asyncio.create_task(clock.sleep(seconds))
    try:
        done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return await task
        raise TimeoutError("plugin clock budget exceeded")
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume)
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)


class FileStorage:
    def __init__(self, path, clock):
        self.path, self.clock = Path(path), clock
        self.path.mkdir(parents=True, exist_ok=True)
        self._file = contained(self.path, self.path / ".values.json")
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or len(key) > 200:
            raise ValueError("storage key must be a string of at most 200 characters")

    def _load(self):
        if not self._file.exists():
            return {}
        value = json.loads(contained(self.path, self._file).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid plugin key-value file")
        return value

    async def get(self, key):
        self._key(key)
        await self.clock.sleep(0)
        async with self._lock:
            return json_copy(self._load().get(key))

    async def set(self, key, value):
        self._key(key)
        value = json_copy(value)
        if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > 1024 * 1024:
            raise ValueError("storage value exceeds one MiB")
        await self.clock.sleep(0)
        async with self._lock:
            values = self._load()
            values[key] = value
            atomic_write(self._file, json.dumps(values, ensure_ascii=False).encode())

    async def delete(self, key):
        self._key(key)
        await self.clock.sleep(0)
        async with self._lock:
            values = self._load()
            values.pop(key, None)
            atomic_write(self._file, json.dumps(values, ensure_ascii=False).encode())

    async def keys(self, prefix=""):
        self._key(prefix)
        await self.clock.sleep(0)
        return sorted(key for key in self._load() if key.startswith(prefix))
