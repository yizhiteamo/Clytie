"""Content-free timing adapters for the approved fictional measurement harness.

Only an explicitly created temporary tree is accepted. These adapters retain
SQLite ownership/checks and delegate every syscall unchanged. They neither run a
workload nor authorize a new measurement. Observer cost is not subtracted.
"""

import asyncio
import os
import sqlite3
import threading
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter  # noqa: TID251 -- offline measurement only
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import url2pathname

_request = ContextVar("measurement_request", default=0)


@contextmanager
def request_scope(identifier):
    token = _request.set(identifier)
    try:
        yield
    finally:
        _request.reset(token)


class Observation:
    def __init__(self, root, recorder, *, origin, timer=perf_counter):
        self.root = Path(root).resolve(strict=True)
        self.recorder, self.origin, self.timer = recorder, origin, timer
        self.guard = threading.Lock()
        self.sequence = 0

    def now(self):
        return max(0, self.timer() - self.origin)

    def record(self, phase, start, finish, outcome, *, request=None):
        with self.guard:
            self.sequence += 1
            identifier = self.sequence
        self.recorder.append(
            "storage-phases.jsonl",
            dict(
                id=identifier,
                request_id=_request.get() if request is None else request,
                phase=phase,
                started_s=start,
                finished_s=finish,
                outcome=outcome,
            ),
        )

    def call(self, phase, function, *args, **kwargs):
        start, outcome = self.now(), "ok"
        try:
            return function(*args, **kwargs)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            self.record(phase, start, self.now(), outcome)

    def _check_database(self, database, *, uri):
        value = os.fsdecode(database)
        if value == ":memory:":
            return
        if uri:
            parsed = urlsplit(value)
            if parsed.scheme != "file" or parsed.netloc:
                raise ValueError("observation_database_outside_fixture")
            value = url2pathname(parsed.path)
        path = Path(value).absolute()
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("observation_database_outside_fixture")
        for candidate in (path, *path.parents):
            if candidate == self.root:
                break
            if candidate.is_symlink() or (
                candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400
            ):
                raise ValueError("observation_database_link")

    @contextmanager
    def install(self):
        from clytie.backup.coordination import CaptureCoordinator
        from clytie.plugin_host import locking

        observer = self
        original_connect, original_fsync = sqlite3.connect, os.fsync
        original_os_lock = locking._os_lock
        original_capture_lock = CaptureCoordinator._lock

        class Connection(sqlite3.Connection):
            def commit(self):
                return observer.call("commit", super().commit)

            def rollback(self):
                return observer.call("rollback", super().rollback)

            def __exit__(self, *exc):
                return observer.call("rollback" if exc[0] else "commit", super().__exit__, *exc)

        def connect(database, *args, **kwargs):
            self._check_database(
                database, uri=args[6] if len(args) > 6 else kwargs.get("uri", False)
            )
            # Do not replace an application's custom connection policy.
            positional = list(args)
            if len(positional) > 4:
                if positional[4] is not sqlite3.Connection or "factory" in kwargs:
                    raise ValueError("observation_factory_unsupported")
                positional[4] = Connection
            else:
                if kwargs.get("factory", sqlite3.Connection) is not sqlite3.Connection:
                    raise ValueError("observation_factory_unsupported")
                kwargs["factory"] = Connection
            return original_connect(database, *positional, **kwargs)

        def fsync(descriptor):
            return observer.call("fsync", original_fsync, descriptor)

        def os_lock(descriptor, acquire):
            return observer.call("lock", original_os_lock, descriptor, acquire)

        @contextmanager
        def capture_lock(coordinator):
            manager = original_capture_lock(coordinator)
            value = observer.call("lock", manager.__enter__)
            try:
                yield value
            except BaseException as exc:
                if not observer.call("lock", manager.__exit__, type(exc), exc, exc.__traceback__):
                    raise
            else:
                observer.call("lock", manager.__exit__, None, None, None)

        with ExitStack() as stack:
            stack.enter_context(patch.object(sqlite3, "connect", connect))
            stack.enter_context(patch.object(os, "fsync", fsync))
            stack.enter_context(patch.object(locking, "_os_lock", os_lock))
            stack.enter_context(patch.object(CaptureCoordinator, "_lock", capture_lock))
            yield self
