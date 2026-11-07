"""受限插件备份输出与保持迟到资源跟踪的分阶段执行器。"""

import asyncio
import hashlib
import os
import unicodedata
from pathlib import Path

from clytie.plugin_host.snapshots import contained
from clytie.sdk.backup import BackupEntry, BackupTicket, BackupView

_FAILED = object()


def _relative(value):
    if (
        not isinstance(value, str)
        or not value
        or any(c in value for c in '\\:\x00<>"|?*')
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("sink_path_invalid")
    parts = value.split("/")
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if any(
        not p
        or p in {".", ".."}
        or p.endswith((".", " "))
        or p.split(".")[0].casefold() in reserved
        for p in parts
    ):
        raise ValueError("sink_path_invalid")
    return unicodedata.normalize("NFC", value).casefold()


class NamespaceSink:
    def __init__(self, root, *, credentials=(), max_chunk_bytes=1024 * 1024, check=None):
        self.root = Path(root)
        contained(self.root.parent, self.root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.credentials = {_relative(p) for p in credentials}
        self.max_chunk_bytes = max_chunk_bytes
        self.check = check
        self.closed = False
        self.failed = False
        self.entries = {}
        self._reserved = set()
        self._spellings = {}

    def close(self):
        self.closed = True

    def _check(self):
        if self.closed:
            raise ValueError("sink_closed")
        if self.check:
            self.check()

    async def write(self, relative_path, chunks):
        try:
            return await self._write(relative_path, chunks)
        except BaseException:
            self.failed = True
            self.close()
            raise

    async def _write(self, relative_path, chunks):
        self._check()
        key = _relative(relative_path)
        if any(key == p or key.startswith(p + "/") for p in self.credentials):
            raise ValueError("sink_credential_forbidden")
        if key in self._reserved:
            raise ValueError("sink_duplicate")
        parts = relative_path.split("/")
        for length in range(1, len(parts) + 1):
            spelling = "/".join(parts[:length])
            folded = _relative(spelling)
            if folded in self._spellings and self._spellings[folded] != spelling:
                raise ValueError("sink_case_conflict")
        for length in range(1, len(parts) + 1):
            spelling = "/".join(parts[:length])
            self._spellings[_relative(spelling)] = spelling
        # 保留大小写折叠后的全部父级拼写，拒绝跨平台含义不一致的目录名。
        for old in self._reserved:
            old_parts, new_parts = old.split("/"), key.split("/")
            if old_parts == new_parts[: len(old_parts)] or new_parts == old_parts[: len(new_parts)]:
                raise ValueError("sink_path_conflict")
        self._reserved.add(key)
        path = contained(self.root, self.root / relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        contained(self.root, path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        size, digest = 0, hashlib.sha256()
        try:
            async for chunk in chunks:
                self._check()
                if not isinstance(chunk, bytes) or len(chunk) > self.max_chunk_bytes:
                    raise ValueError("sink_chunk_invalid")

                def append(data):
                    self._check()
                    offset = 0
                    while offset < len(data):
                        offset += os.write(descriptor, data[offset:])
                    digest.update(data)

                # 单次后台写执行者必须收回后再关 descriptor。
                task = asyncio.create_task(asyncio.to_thread(append, chunk))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    self.close()
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                    raise
                self._check()
                size += len(chunk)
            self._check()
            entry = BackupEntry(relative_path=relative_path, size=size, sha256=digest.hexdigest())
            self.entries[relative_path] = entry
            return entry
        finally:
            os.close(descriptor)


class ProviderCapture:
    def __init__(self, provider, request, clock, sink, *, cancel):
        self.provider, self.request, self.clock, self.sink = provider, request, clock, sink
        self.cancel = cancel.child()
        self.ticket = self.view = None
        self.errors = []
        self.tasks = set()
        self._release_requested = False
        self._release_deferred = False
        self._release_task = None
        self.released = self.materialized = False

    def _failed(self, stage, reason):
        self.errors.append({"stage": stage, "reason": reason})
        self.sink.close()
        self.cancel.cancel(reason)
        self._release_requested = True

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()
        self._schedule_release()

    def _schedule_release(self):
        if (
            self._release_requested
            and not self._release_deferred
            and not self.tasks
            and self._release_task is None
        ):
            if self.ticket is None:
                self.released = True
            else:
                self._release_task = asyncio.create_task(self._release_once())
                self._release_task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )

    async def _call(self, stage, operation, seconds=None, *, watch_cancel=True):
        started = self.clock.monotonic()

        async def invoke():
            result = await operation()
            if stage == "prepare" and isinstance(result, BackupTicket):
                self.ticket = result
            return result

        task = asyncio.create_task(invoke())
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        timer = asyncio.create_task(self.clock.sleep(seconds)) if seconds is not None else None
        cancellation = asyncio.create_task(self.cancel.wait()) if watch_cancel else None
        waiters = {task, *(t for t in (timer, cancellation) if t is not None)}
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                if task.cancelled():
                    self._failed(stage, "provider_cancelled")
                    return _FAILED
                if seconds is not None and self.clock.monotonic() - started >= seconds:
                    self._failed(stage, "provider_timeout")
                    return _FAILED
                if watch_cancel and self.cancel.cancelled:
                    self._failed(stage, "cancelled")
                    return _FAILED
                return task.result()
            self._failed(stage, "provider_timeout" if timer in done else "cancelled")
            task.cancel()
            return _FAILED
        except asyncio.CancelledError:
            self._failed(stage, "cancelled")
            task.cancel()
            raise
        except Exception:
            self._failed(stage, "provider_error")
            return _FAILED
        finally:
            for waiter in (timer, cancellation):
                if waiter:
                    waiter.cancel()
            await asyncio.gather(*(t for t in (timer, cancellation) if t), return_exceptions=True)
            self._schedule_release()

    async def prepare(self):
        if self.ticket is not None or self.errors or self.released:
            return False
        seconds = max(0, min(10, (self.request.deadline - self.clock.now()).total_seconds()))
        if seconds == 0 or self.cancel.cancelled:
            self._failed("prepare", "provider_timeout" if seconds == 0 else "cancelled")
            await self.release()
            return False
        result = await self._call(
            "prepare", lambda: self.provider.prepare(self.request, self.cancel), seconds
        )
        if result is _FAILED:
            return False
        if not isinstance(result, BackupTicket):
            self._failed("prepare", "provider_contract")
            await self.release()
            return False
        return True

    async def capture(self, epoch):
        if self.ticket is None or self.errors or self.view is not None or self._release_requested:
            return False
        # 捕获可能处于共同门闩内；释放只能由调用方离开门闩后触发。
        self._release_deferred = True
        result = await self._call(
            "capture", lambda: self.provider.capture(self.ticket, epoch, self.cancel), 0.25
        )
        if result is _FAILED:
            return False
        if (
            not isinstance(result, BackupView)
            or result.epoch != epoch
            or result.data_version != self.request.data_version
        ):
            self._failed("capture", "provider_contract")
            return False
        self.view = BackupView(
            result.epoch, result.data_version, result.revision, result.handle, result.assets
        )
        self._asset_snapshot = tuple(asset.model_dump_json() for asset in self.view.assets)
        return True

    async def materialize(self, *, timeout=None):
        if self.view is None or self.errors or self.materialized or self._release_requested:
            return False
        try:
            result = await self._call(
                "materialize",
                lambda: self.provider.materialize(self.view, self.sink, self.cancel),
                timeout,
            )
            if result is _FAILED:
                return False
            if result is not None:
                self._failed("materialize", "provider_contract")
                return False
            try:
                validated = BackupView(
                    self.view.epoch,
                    self.view.data_version,
                    self.view.revision,
                    self.view.handle,
                    self.view.assets,
                )
                assets = tuple(asset.model_dump_json() for asset in validated.assets)
            except (TypeError, ValueError, AttributeError):
                self._failed("materialize", "provider_contract")
                return False
            if assets != self._asset_snapshot:
                self._failed("materialize", "provider_contract")
                return False
            if self.sink.failed:
                self._failed("materialize", "sink_rejected")
                return False
            self.materialized = True
        finally:
            self.sink.close()
            await self.release()
        return self.released and not self.errors

    async def _release_once(self):
        result = await self._call(
            "release", lambda: self.provider.release(self.ticket), 2, watch_cancel=False
        )
        if result is not _FAILED and result is not None:
            self._failed("release", "provider_contract")
        self.released = result is None
        return self.released

    async def release(self):
        self.sink.close()
        self._release_requested = True
        self._release_deferred = False
        self._schedule_release()
        if self._release_task is not None:
            return await asyncio.shield(self._release_task)
        return self.released

    def result(self):
        pending = (
            bool(self.tasks) or self._release_task is not None and not self._release_task.done()
        )
        return {
            "status": "complete"
            if self.materialized and self.released and not self.errors and not pending
            else "partial",
            "resource_pending": pending,
            "errors": list(self.errors),
            "entries": [entry.model_dump() for entry in self.sink.entries.values()],
        }
