"""既有管道上的同步预算请求；接收线程只交付结果，不执行重 I/O。"""

import asyncio
import threading
from functools import partial
from uuid import uuid4

from .io_budget import IoBudgetError
from .io_client import IoClient, checkpoint


async def pressure_updates(runtime, clock, emit):
    while True:
        owner = runtime.storage_owner
        if owner is not None:
            emit(dict(type="storage_pressure", sample=owner.sample_io_pressure()))
        await clock.sleep(0.1)


class IoChannel:
    def __init__(self, emit):
        self.emit = emit
        self._condition = threading.Condition()
        self._pending = {}
        self._disconnected = False

    @property
    def pending_count(self):
        with self._condition:
            return len(self._pending)

    def client(self, operation_id, *, check):
        return IoClient(partial(self.call, operation_id, check=check))

    def receive(self, value):
        if not isinstance(value, dict) or value.get("type") != "backup_io_reply":
            return False
        key = value.get("id")
        if not isinstance(key, str):
            return True
        with self._condition:
            if key in self._pending:
                self._pending[key] = value
                self._condition.notify_all()
        return True

    def disconnect(self):
        with self._condition:
            self._disconnected = True
            self._condition.notify_all()

    def call(self, operation_id, arguments, *, check, cleanup=False):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise IoBudgetError("backup_io_on_event_loop")
        if not cleanup:
            checkpoint()
            check()
        key = uuid4().hex
        with self._condition:
            if self._disconnected:
                raise IoBudgetError("backup_io_disconnected")
            # Pipeline producer is trusted host code; bound transport occupancy too.
            if len(self._pending) >= 64 and not cleanup:
                raise IoBudgetError("backup_io_busy")
            self._pending[key] = None
        try:
            self.emit(
                dict(type="backup_io", id=key, operation_id=operation_id, arguments=arguments)
            )
            while True:
                if not cleanup:
                    checkpoint()
                    check()
                with self._condition:
                    if self._disconnected:
                        raise IoBudgetError("backup_io_disconnected")
                    response = self._pending[key]
                    if response is not None:
                        if "error" in response or response.get("result") != {}:
                            raise IoBudgetError("backup_io_remote_failure")
                        return response["result"]
                    # No time budget here: the controller's injected clock governs
                    # tokens. Poll only to observe cooperative cancellation/view limits.
                    self._condition.wait(0.05)
        finally:
            with self._condition:
                self._pending.pop(key, None)
