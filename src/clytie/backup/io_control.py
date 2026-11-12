"""实例控制面的一份预算，跨既有伴侣/工作进程通道共享。"""

import asyncio
import math
from dataclasses import dataclass, field
from uuid import uuid4

from .io_budget import IoBudget, IoBudgetError
from .io_protocol import BudgetEndpoint


@dataclass
class _Session:
    storage_owner: bool
    endpoints: dict = field(default_factory=dict)
    tasks: set = field(default_factory=set)
    disconnected: bool = False


class IoControl:
    def __init__(self, clock):
        self.budget = IoBudget(clock)
        self.sessions = {}
        self.retiring = set()
        self._owners()

    def _owners(self):
        self.budget.set_owners({key for key, value in self.sessions.items() if value.storage_owner})

    def register(self, *, storage_owner=False):
        # A new launch receives a new token. PID reuse cannot regain an old slot.
        key = uuid4().hex
        self.sessions[key] = _Session(storage_owner)
        self._owners()
        return key

    def authorize(self, session, operation):
        state = self.sessions[session]
        if state.disconnected or operation in self.retiring:
            raise IoBudgetError("backup_io_executor_lost")
        if operation not in state.endpoints:
            state.endpoints[operation] = BudgetEndpoint(
                self.budget, executor=session, operation=operation
            )

    def dispatch(self, session, message, send):
        state = self.sessions[session]
        task = asyncio.create_task(self._reply(session, message, send))
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return task

    async def _reply(self, session, message, send):
        state = self.sessions[session]
        response = {"type": "backup_io_reply", "id": message.get("id")}
        try:
            if state.disconnected:
                raise IoBudgetError("backup_io_executor_lost")
            if set(message) - {"type", "id", "seq", "operation_id", "arguments"}:
                raise IoBudgetError("backup_io_request_invalid")
            if not isinstance(message.get("id"), str) or len(message["id"]) > 128:
                raise IoBudgetError("backup_io_request_invalid")
            operation = message.get("operation_id")
            if not isinstance(operation, str) or operation not in state.endpoints:
                raise IoBudgetError("backup_io_operation_not_authorized")
            arguments = message.get("arguments")
            release = isinstance(arguments, dict) and arguments.get("action") == "release"
            if operation in self.retiring and not release:
                raise IoBudgetError("backup_io_operation_not_authorized")
            if len(state.tasks) > 64 and not release:
                raise IoBudgetError("backup_io_busy")
            response["result"] = await state.endpoints[operation].request(arguments)
        except IoBudgetError as exc:
            response["error"] = exc.code
        except Exception:
            response["error"] = "backup_io_control_failed"
        try:
            await send(response)
        except (OSError, RuntimeError):
            # Failed response delivery is not proof that the executor stopped I/O.
            self.disconnect(session)
        self._reap()

    def finish_operation(self, operation):
        # Caller has stopped dispatching this durable operation and settled its
        # capture requests. Unknown I/O still holds its slot until release/exit.
        self.retiring.add(operation)
        self._reap()
        return operation not in self.retiring

    def _reap(self):
        for operation in tuple(self.retiring):
            try:
                self.budget.forget_operation(operation)
            except IoBudgetError:
                continue
            for state in self.sessions.values():
                state.endpoints.pop(operation, None)
            self.retiring.remove(operation)

    def pressure(self, session, sample):
        state = self.sessions[session]
        if not state.storage_owner or state.disconnected:
            raise IoBudgetError("backup_io_pressure_owner_unknown")
        try:
            if sample["source"] != "owner_control_cache":
                raise ValueError()
            final = sample["finalizer_wait_seconds"]
            blocked = sample["finalizer_capacity_blocked"]
            if (
                type(final) not in (int, float)
                or not math.isfinite(final)
                or final < 0
                or type(blocked) is not bool
            ):
                raise ValueError()
            self.budget.pressure(
                session,
                waiting=sample["ready_wait_seconds"],
                executing=sample["execution_seconds"],
                finalizer_wait=blocked or final > 0,
            )
        except (KeyError, TypeError, ValueError):
            # Invalid observations invalidate the old sample immediately.
            self.budget.pressure(session, waiting=-1, executing=0, finalizer_wait=False)

    def disconnect(self, session):
        state = self.sessions.get(session)
        if state is not None:
            state.disconnected = True
            self.budget.disconnected(session)

    async def ended(self, session, *, confirmed):
        if confirmed is not True:
            raise IoBudgetError("backup_io_exit_unconfirmed")
        state = self.sessions.get(session)
        if state is None:
            return
        self.disconnect(session)
        self.budget.executor_ended(session, confirmed=True)
        for task in tuple(state.tasks):
            task.cancel()
        await asyncio.gather(*tuple(state.tasks), return_exceptions=True)
        self.sessions.pop(session, None)
        self._reap()
        self._owners()
