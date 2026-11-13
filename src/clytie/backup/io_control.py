"""实例控制面的一份预算，跨既有伴侣/工作进程通道共享。"""

import asyncio
import math
from dataclasses import dataclass, field
from uuid import uuid4

from clytie.plugin_host.support import finish_cleanup

from .io_budget import IoBudget, IoBudgetError
from .io_protocol import BudgetEndpoint


@dataclass
class _Session:
    storage_owner: bool
    endpoints: dict = field(default_factory=dict)
    tasks: set = field(default_factory=set)
    disconnected: bool = False
    reserved: set = field(default_factory=set)
    persisted: set = field(default_factory=set)
    closed_operations: set = field(default_factory=set)


class IoControl:
    def __init__(self, clock, *, home=None):
        self.budget = IoBudget(clock)
        self.controller = uuid4().hex
        self.responsibilities = None
        if home is not None:
            from .io_responsibility import Responsibilities

            self.responsibilities = Responsibilities(home, clock)
        self.sessions = {}
        self.retiring = set()
        self._release_pending = set()
        self._metadata_lock = asyncio.Lock()
        self.admission_state = {"state": "pending", "blockers": []}
        self._owners()

    async def recheck(self):
        if self.responsibilities is None:
            return {
                "state": "ready",
                "blockers": [],
                "executions": [],
                "observed_at": self.budget.clock.now().isoformat(),
            }
        result, cancelled = await self._metadata("check", self.controller)
        self.admission_state = result
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def _metadata(self, method, *args, **kwargs):
        # This controller must not race its own short durable bookkeeping.
        # Other controllers still coordinate through the stable OS file lock.
        # Await outside Owner/transactions/latches; retain the gate until the
        # actual file worker settles, even if its awaiting task is cancelled.
        async with self._metadata_lock:
            return await finish_cleanup(
                asyncio.to_thread(getattr(self.responsibilities, method), *args, **kwargs)
            )

    async def ensure_ready(self):
        if (await self.recheck())["state"] != "ready":
            raise IoBudgetError("backup_io_recheck_required")

    def _session(self, session, operation):
        state = self.sessions[session]
        if state.disconnected or operation in state.closed_operations or operation in self.retiring:
            raise IoBudgetError("backup_io_operation_not_authorized")
        return state

    async def _registration(self, method, session, operation, *args):
        state = self._session(session, operation)
        if self.responsibilities is None:
            return state
        _, cancelled = await self._metadata(method, self.controller, session, operation, *args)
        state.persisted.add(operation)
        if cancelled or state.disconnected:
            # The caller has not received an execution authorization. Settle the
            # durable write, then revoke this unexecuted intent before returning.
            state.closed_operations.add(operation)
            await self._metadata("release", self.controller, session, operation, confirmed=True)
            if cancelled:
                raise asyncio.CancelledError()
            raise IoBudgetError("backup_io_executor_lost")
        return state

    async def reserve(self, session, operation, *, role="operation"):
        state = await self._registration("reserve", session, operation, role)
        state.reserved.add(operation)

    async def bind_execution(self, session, operation, identity):
        state = self._session(session, operation)
        if operation not in state.reserved:
            raise IoBudgetError("backup_io_operation_not_authorized")
        await self._registration("bind", session, operation, identity)
        self.authorize(session, operation)

    async def authorize_execution(self, session, operation, identity, *, role="operation"):
        await self._registration("register", session, operation, identity, role)
        self.authorize(session, operation)

    async def retire_operation(self, operation):
        ended = self.finish_operation(operation)
        if self.responsibilities is not None:
            # Tombstones/endpoints are revoked before persisting trustworthy
            # release. A failed write leaves durable responsibility intact.
            for session, key in tuple(self._release_pending):
                if key != operation:
                    continue
                _, cancelled = await self._metadata(
                    "release", self.controller, session, key, confirmed=True
                )
                self._release_pending.discard((session, key))
                if cancelled:
                    raise asyncio.CancelledError()
        return ended

    def _owners(self):
        self.budget.set_owners({key for key, value in self.sessions.items() if value.storage_owner})

    def register(self, *, storage_owner=False):
        # A new launch receives a new token. PID reuse cannot regain an old slot.
        key = uuid4().hex
        self.sessions[key] = _Session(storage_owner)
        self._owners()
        return key

    def authorize(self, session, operation):
        state = self._session(session, operation)
        if self.responsibilities is not None and operation not in state.persisted:
            raise IoBudgetError("backup_io_operation_not_authorized")
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
            for session, state in self.sessions.items():
                state.endpoints.pop(operation, None)
                state.closed_operations.add(operation)
                if operation in state.persisted:
                    self._release_pending.add((session, operation))
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
        try:
            if self.responsibilities is not None and state.persisted:
                _, cancelled = await self._metadata("ended", self.controller, session)
                self._release_pending = {
                    item for item in self._release_pending if item[0] != session
                }
                if cancelled:
                    # The persistent result is real; cancellation still propagates.
                    raise asyncio.CancelledError()
        finally:
            # The executor was actually waited above; it cannot submit work again.
            # A metadata failure still propagates and leaves durable responsibility
            # untouched for recheck, but must not retain a dead pressure source.
            self.sessions.pop(session, None)
            self._reap()
            self._owners()
