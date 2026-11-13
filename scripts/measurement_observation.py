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
    def __init__(self, root, recorder, *, origin, timer=perf_counter, process_sequence=None):
        self.root = Path(root).resolve(strict=True)
        self.recorder, self.origin, self.timer = recorder, origin, timer
        self.guard = threading.Lock()
        self.sequence = 0
        self.process_sequence = process_sequence
        self.io = self.pressure = None
        if recorder.format_version in {5, 6}:
            try:
                from .measurement_io_observation import IoObservation
            except ImportError:
                from measurement_io_observation import IoObservation
            self.io = IoObservation(self, process_sequence)
            self.sequence = (process_sequence + 1) << 40
        if recorder.format_version == 6:
            try:
                from .measurement_pressure_observation import PressureObservation
            except ImportError:
                from measurement_pressure_observation import PressureObservation
            self.pressure = PressureObservation(self.io)
            self.io.pressure = self.pressure

    def now(self):
        return max(0, self.timer() - self.origin)

    def record(self, phase, start, finish, outcome, *, request=None):
        # Once explicitly invalidated, no evidence can be sealed. Do not turn
        # the cleanup of already accepted business into another observer error.
        if self.recorder.failed:
            return
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
    def observe_owner(self, owner):
        """Observe the real command boundary; do not replace queues or execution."""
        original = owner.submit
        pending = {}

        def submit(function, **kwargs):
            queued, request = self.now(), _request.get()
            entered = False
            recorded = False
            phase = "finalizer" if kwargs.get("finalizer") is not None else "queue"

            def measured(resources, command):
                nonlocal entered
                entered = True
                self.record(phase, queued, self.now(), "ok", request=request)
                with request_scope(request):
                    return self.call("execute", function, resources, command)

            try:
                command = original(measured, **kwargs)
            except BaseException as exc:
                outcome = "busy" if getattr(exc, "code", None) == "storage_busy" else "failed"
                self.record(phase, queued, self.now(), outcome, request=request)
                raise

            def completed(future):
                nonlocal recorded
                if recorded:
                    return
                recorded = True
                try:
                    if self.recorder.failed:
                        return
                    if entered:
                        return
                    # A queued cancellation may never enter the function. Keep its
                    # queue residence instead of silently losing that slow sample.
                    error = None if future.cancelled() else future.exception()
                    outcome = (
                        "cancelled"
                        if future.cancelled() or getattr(error, "code", None) == "cancelled"
                        else "failed"
                        if error
                        else "ok"
                    )
                    self.record(phase, queued, self.now(), outcome, request=request)
                finally:
                    # Retain only unsettled commands, not every Future and callback
                    # from the full window. Invalidated recordings also release them.
                    pending.pop(future, None)

            pending[command._future] = completed
            command._future.add_done_callback(completed)
            return command

        with patch.object(owner, "submit", submit):
            try:
                yield
            finally:
                # An already-done Future can return before its scheduled callbacks
                # run. Settle evidence synchronously; no artificial loop sleep or
                # deadline extension. Pending execution cannot become a complete log.
                for future, completed in tuple(pending.items()):
                    if not future.done():
                        self.recorder.failed = True
                        raise ValueError("measurement_owner_unsettled")
                    completed(future)

    @contextmanager
    def install(self):
        from clytie.backup.coordination import CaptureCoordinator
        from clytie.plugin_host import locking

        observer = self
        original_connect, original_fsync = sqlite3.connect, os.fsync
        original_os_lock = locking._os_lock
        original_capture_lock = CaptureCoordinator._lock
        native = None
        if self.recorder.format_version in {4, 5, 6}:
            try:
                from .measurement_native_observation import NativeObservation, _validation
            except ImportError:
                from measurement_native_observation import NativeObservation, _validation
            native = NativeObservation(self)

        class Connection(sqlite3.Connection):
            def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
                if observer.io is None:
                    return super().backup(
                        target, pages=pages, progress=progress, name=name, sleep=sleep
                    )
                return observer.io.backup(
                    super().backup, target, pages=pages, progress=progress, name=name, sleep=sleep
                )

            def execute(self, sql, *args, **kwargs):
                state = native.before_sql(self, sql, _request.get()) if native is not None else None
                state = state or getattr(self, "_measurement_implicit", None)
                try:
                    cursor = super().execute(sql, *args, **kwargs)
                except BaseException as exc:
                    if state is not None:
                        native.end(state, exc)
                    raise
                finally:
                    if state is not None:
                        state["last_call"] = observer.now()
                cursor = native.cursor(cursor, state) if native is not None else cursor
                if observer.pressure is not None:
                    cursor = observer.pressure.cursor(self, sql, cursor)
                return cursor

            def close(self):
                state = getattr(self, "_measurement_implicit", None)
                if state is not None:
                    native.end(state)
                result = super().close()
                if native is not None:
                    native.closed(self)
                return result

            def set_progress_handler(self, callback, instructions):
                state = _validation.get() if native is not None else None
                if callback is not None and state is not None:
                    original_callback = callback

                    def callback():
                        result = original_callback()
                        if result:
                            native.interrupted(state)
                        return result

                return super().set_progress_handler(callback, instructions)

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
            connection = original_connect(database, *positional, **kwargs)
            if native is not None and native.legacy:
                native.bind(connection)
            return connection

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
            if self.io is not None:
                self.io.install(stack)
            if self.pressure is not None:
                self.pressure.install(stack)
            stack.enter_context(patch.object(sqlite3, "connect", connect))
            stack.enter_context(patch.object(os, "fsync", fsync))
            stack.enter_context(patch.object(locking, "_os_lock", os_lock))
            stack.enter_context(patch.object(CaptureCoordinator, "_lock", capture_lock))
            if native is not None:
                native.install(stack, _request.get)
            try:
                yield self
            finally:
                if native is not None:
                    native.check_unsettled()
