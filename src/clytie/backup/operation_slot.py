"""可信管理协调者的限定操作槽位；不是数据权限或通用线程锁。"""

import os
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .io_budget import IoBudgetError
from .io_client import _checks, _stage, checkpoint, io_checks, io_scope

_binding = ContextVar("management_operation_slot", default=None)


@dataclass(frozen=True)
class SlotBinding:
    slot: object
    home: Path
    companion: str
    operation: str
    execution: str


def current_slot():
    return _binding.get()


def verify_operation_binding(binding, journal, client=None):
    if binding is None:
        return
    if client is not None and client is not binding.slot.client:
        raise IoBudgetError("management_slot_context_mismatch")
    if journal is not None and (
        binding.operation != journal.value["id"]
        or binding.companion != journal.value["companion_id"]
        or binding.home != journal.path.parents[4].resolve()
    ):
        raise IoBudgetError("management_slot_context_mismatch")


@contextmanager
def operation_scope(binding):
    token = _binding.set(binding)
    try:
        yield
    finally:
        _binding.reset(token)


def release_operation_slot(slot, journal):
    """只处理失联的释放回执；真实执行结束由协调者先行核验。"""
    try:
        slot.release()
    except IoBudgetError as error:
        if error.code not in {
            "backup_io_disconnected",
            "backup_io_exit_unconfirmed",
            "backup_io_executor_lost",
        }:
            raise
        journal.value["io_release_unconfirmed"] = True
        journal.value["io_release_error"] = error.code
        journal.save()
        return False
    return True


class OperationSlot:
    def __init__(self, client, home, companion, operation):
        self.client = client
        self.binding = SlotBinding(self, Path(home).resolve(), companion, operation, uuid4().hex)
        self._pid = os.getpid()
        self._guard = threading.Lock()
        self._status = "new"
        self._borrower = None
        self._state = {"client": client, "id": uuid4().hex, "thread": None, "sequence": 0}

    @property
    def held(self):
        with self._guard:
            return self._status == "held"

    def acquire(self):
        # Only the queue-external file executor calls this; no Owner command/latch.
        with self._guard:
            self._check_process()
            if self._status != "new":
                raise IoBudgetError("management_slot_already_acquired")
            self._status = "acquiring"
        try:
            self.client._request({"action": "acquire", "stage": self._state["id"]})
        except BaseException as original:
            try:
                self.client.request({"action": "release", "stage": self._state["id"]}, cleanup=True)
            except IoBudgetError as error:
                raise original from error
            finally:
                self._status = "failed"
            raise
        self._status = "held"

    def _check_process(self):
        if self._pid != os.getpid():
            raise IoBudgetError("management_slot_execution_mismatch")

    @contextmanager
    def borrow(self, binding, *, cleanup=False):
        with self._guard:
            self._check_process()
            if binding is not self.binding:
                raise IoBudgetError("management_slot_execution_mismatch")
            if self._status != "held":
                raise IoBudgetError("management_slot_released")
            if self._borrower is not None or _stage.get() is not None:
                raise IoBudgetError("management_slot_overlap")
            borrower = object()
            self._borrower = borrower
            self._state["thread"] = threading.get_ident()
        token = _stage.set(self._state)
        checks = _checks.set(()) if cleanup else None
        try:
            with (
                operation_scope(binding),
                io_scope(None if cleanup else self.client),
                io_checks(self.client.check_available) if not cleanup else nullcontext(),
            ):
                if not cleanup:
                    checkpoint()
                    self.client.check_available()
                yield
        finally:
            if checks is not None:
                _checks.reset(checks)
            _stage.reset(token)
            with self._guard:
                if self._borrower is not borrower:
                    raise IoBudgetError("management_slot_execution_mismatch")
                self._borrower = None
                self._state["thread"] = None

    def release(self):
        with self._guard:
            self._check_process()
            if self._status != "held":
                raise IoBudgetError("management_slot_released")
            if self._borrower is not None:
                raise IoBudgetError("management_slot_borrowed")
            self._status = "releasing"
        # A lost receipt leaves occupancy unknown at both ends, never reusable.
        self.client.request({"action": "release", "stage": self._state["id"]}, cleanup=True)
        with self._guard:
            self._status = "released"
