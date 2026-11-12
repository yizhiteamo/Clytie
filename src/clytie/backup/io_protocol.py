"""绑定可信执行者/操作的内部预算端点，不接受客户端指定归属。"""

import asyncio
from dataclasses import dataclass

from .io_budget import IoBudgetError, StageKey


@dataclass
class _Request:
    acquire: asyncio.Task | None = None
    closed: bool = False


class BudgetEndpoint:
    def __init__(self, budget, *, executor, operation):
        self.budget, self.executor, self.operation = budget, executor, operation
        self.requests = {}

    async def request(self, arguments):
        if not isinstance(arguments, dict):
            raise IoBudgetError("backup_io_request_invalid")
        action, stage = arguments.get("action"), arguments.get("stage")
        fields = {"action", "stage"}
        if action == "charge":
            fields |= {"sequence", "direction", "size"}
        if (
            action not in {"acquire", "charge", "release"}
            or set(arguments) != fields
            or not isinstance(stage, str)
            or not stage
            or len(stage) > 128
            or not stage.isascii()
            or not all(character.isalnum() or character == "_" for character in stage)
        ):
            raise IoBudgetError("backup_io_request_invalid")
        key = StageKey(self.executor, self.operation, stage)
        record = self.requests.setdefault(stage, _Request())
        if action == "release":
            # 调用者在同步 I/O 真正返回后声明该阶段结束；包括等待取消。
            # 先留墓碑，防止在途 acquire 迟到时重新排队。
            record.closed = True
            if record.acquire is not None:
                if not record.acquire.done():
                    record.acquire.cancel()
                await asyncio.gather(record.acquire, return_exceptions=True)
            await self.budget.finish_requested(key)
            return {}
        if record.closed:
            raise IoBudgetError("backup_io_stage_finished")
        if action == "acquire":
            if record.acquire is None:
                record.acquire = asyncio.create_task(self.budget.acquire(key))
                record.acquire.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
            try:
                await asyncio.shield(record.acquire)
            except asyncio.CancelledError:
                if record.closed:
                    raise IoBudgetError("backup_io_stage_finished") from None
                raise
            if record.closed:
                raise IoBudgetError("backup_io_stage_finished")
            return {}
        if record.acquire is None or not record.acquire.done() or record.acquire.cancelled():
            raise IoBudgetError("backup_io_stage_inactive")
        lease = record.acquire.result()
        try:
            await self.budget.charge(
                lease, arguments["sequence"], arguments["direction"], arguments["size"]
            )
        except asyncio.CancelledError:
            if record.closed:
                raise IoBudgetError("backup_io_stage_finished") from None
            raise
        return {}
