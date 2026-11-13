"""Observe real snapshot acquisition/close and independently verify thread exit."""

import threading
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

try:
    from .measurement_recording import Recorder
except ImportError:
    from measurement_recording import Recorder


class Views:
    def __init__(self, recorder, *, now, first_id=1):
        if type(first_id) is not int or first_id < 1:
            raise ValueError("measurement_view_id_invalid")
        self.first_id = first_id
        self.recorder, self.now = recorder, now
        self.guard = threading.Lock()
        self.workers, self.open, self.exited = {}, set(), set()

    def event(self, worker, resource, event):
        self.recorder.append(
            "release-events.jsonl",
            dict(id=self.workers[worker], at_s=self.now(), resource=resource, event=event),
        )

    @property
    def pending(self):
        with self.guard:
            return len(self.open)

    def verify_released(self):
        with self.guard:
            if self.open or any(
                worker.thread.is_alive() or worker._connections for worker in self.workers
            ):
                raise ValueError("measurement_view_release_unconfirmed")
            for worker in self.workers:
                if worker not in self.exited:
                    self.event(worker, "thread", "exit_verified")
                    self.exited.add(worker)

    @contextmanager
    def install(self):
        from clytie.backup.capture import _SnapshotWorker

        original_init = _SnapshotWorker.__init__
        original_capture, original_close = _SnapshotWorker._capture, _SnapshotWorker._close
        observer = self

        def created(worker, *args, **kwargs):
            original_init(worker, *args, **kwargs)
            with observer.guard:
                observer.workers[worker] = len(observer.workers) + observer.first_id
                observer.event(worker, "thread", "acquired")

        def capture(worker):
            try:
                result = original_capture(worker)
            except BaseException:
                # Inspect only in the snapshot's own thread. A partially pinned
                # failed capture must still remain visible until real cleanup.
                if any(db.in_transaction for db in worker._connections.values()):
                    with observer.guard:
                        observer.open.add(worker)
                        observer.event(worker, "view", "unknown")
                raise
            if result:
                with observer.guard:
                    observer.open.add(worker)
                    observer.event(worker, "view", "acquired")
            return result

        def closed(worker):
            try:
                result = original_close(worker)
            except BaseException:
                observer.event(worker, "view", "failed")
                raise
            with observer.guard:
                if worker in observer.open:
                    observer.open.remove(worker)
                    observer.event(worker, "view", "released")
            return result

        with ExitStack() as stack:
            stack.enter_context(patch.object(_SnapshotWorker, "__init__", created))
            stack.enter_context(patch.object(_SnapshotWorker, "_capture", capture))
            stack.enter_context(patch.object(_SnapshotWorker, "_close", closed))
            yield self


__all__ = ["Recorder", "Views"]
