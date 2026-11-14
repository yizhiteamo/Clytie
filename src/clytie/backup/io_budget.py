"""控制面拥有的实例预算；只管理门闩外重 I/O，不执行任何磁盘写入。"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from dataclasses import dataclass

CHUNK = 256 * 1024
BURST = 512 * 1024
RATE_NORMAL = 16 * 1024 * 1024
RATE_BUSY = 4 * 1024 * 1024
WAITING = 8
SIGNAL_INTERVAL = 0.1
SIGNAL_MAX_AGE = 1.0


class IoBudgetError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StageKey:
    # executor 由控制端依据进程身份生成，不能由 HTTP/插件选择。
    executor: str
    operation: str
    stage: str


class _Stage:
    def __init__(self, key):
        self.key = key
        self.ready = asyncio.get_running_loop().create_future()
        self.finished = False
        self.disconnected = False
        self.sequence = 0
        self.charge_request = None
        self.charge_task = None


class IoBudget:
    """仅在同一控制事件循环调用。离线测试使用注入 Clock。

    等待者断线不代表执行者结束；占用释放须由真实执行阶段 finally 或
    控制端核验进程身份及退出后发起。没有逐批锁文件、fsync 或重试线程。
    """

    def __init__(self, clock):
        self.clock = clock
        self._stages = {}
        self._waiting = deque()
        self._active = None
        self._lost = set()
        self._changed = asyncio.Event()
        self._owners = None  # 缺少信号不能冒充实例没有运行伴侣。
        self._signals = {}
        self._normal = False
        self._low_since = None
        self._tokens = float(BURST)
        self._updated = clock.monotonic()
        self._charged = {"read": 0, "write": 0}

    def _wake(self):
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _fresh_until(self):
        if self._owners is None or any(key not in self._signals for key in self._owners):
            return float("-inf")
        return min(
            (self._signals[key][0] + SIGNAL_MAX_AGE for key in self._owners),
            default=float("inf"),
        )

    def _refill(self):
        now, fresh_until = self.clock.monotonic(), self._fresh_until()
        start, normal_at = self._updated, float("inf")
        if self._normal:
            normal_at = start
        elif self._low_since is not None:
            normal_at = self._low_since + 1.0
        # 精确分割正常/过时时间段；不能将陈旧时段按上一次正常速率补满。
        normal_time = max(0.0, min(now, fresh_until) - max(start, normal_at))
        elapsed = max(0.0, now - start)
        self._tokens = min(
            BURST,
            self._tokens + normal_time * RATE_NORMAL + (elapsed - normal_time) * RATE_BUSY,
        )
        self._updated = now
        if now > fresh_until:
            self._normal, self._low_since = False, None
        elif now >= normal_at:
            self._normal = True

    def set_owners(self, owners):
        """只能由管理运行进程的控制端确认；包括明确的空集合。"""
        self._refill()
        owners = frozenset(owners)
        if owners == self._owners:
            return
        self._owners = owners
        self._signals = {key: value for key, value in self._signals.items() if key in owners}
        self._normal, self._low_since = False, None
        self._evaluate_pressure()

    def pressure(self, owner, *, waiting, executing, finalizer_wait):
        self._refill()
        if self._owners is None or owner not in self._owners:
            raise IoBudgetError("backup_io_pressure_owner_unknown")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value < 0
            for value in (waiting, executing)
        ) or not isinstance(finalizer_wait, bool):
            self._signals.pop(owner, None)
            self._normal, self._low_since = False, None
            self._wake()
            raise IoBudgetError("backup_io_pressure_invalid")
        self._signals[owner] = (self.clock.monotonic(), waiting, executing, finalizer_wait)
        self._evaluate_pressure()

    def _evaluate_pressure(self):
        now = self.clock.monotonic()
        if now > self._fresh_until():
            self._normal, self._low_since = False, None
        else:
            samples = [self._signals[key] for key in self._owners]
            busy = any(wait >= 0.020 or run >= 0.020 or final for _, wait, run, final in samples)
            low = all(wait < 0.005 and run < 0.005 and not final for _, wait, run, final in samples)
            if busy:
                self._normal, self._low_since = False, None
            elif low:
                if self._low_since is None:
                    self._low_since = now
            else:
                self._low_since = None
        self._wake()

    async def acquire(self, key):
        if not isinstance(key, StageKey) or any(
            not isinstance(value, str) or not value
            for value in (key.executor, key.operation, key.stage)
        ):
            raise IoBudgetError("backup_io_request_invalid")
        if key.executor in self._lost:
            raise IoBudgetError("backup_io_executor_lost")
        stage = self._stages.get(key)
        if stage is not None:
            if stage.finished:
                raise IoBudgetError("backup_io_stage_finished")
        else:
            # 同执行者的独立就绪调用链也可排队；嵌套同步调用由客户端共用阶段。
            if self._active is not None and len(self._waiting) >= WAITING:
                raise IoBudgetError("backup_io_busy")
            stage = self._stages[key] = _Stage(key)
            self._waiting.append(stage)
            self._grant()
        try:
            return await asyncio.shield(stage.ready)
        except asyncio.CancelledError:
            # 尚未授予的等待可取消；已交付或不确定的槽位继续保留。
            if stage in self._waiting:
                self._waiting.remove(stage)
                stage.finished = True
                stage.ready.cancel()
            raise

    def _grant(self):
        if self._active is None and self._waiting:
            self._active = self._waiting.popleft()
            self._active.ready.set_result(self._active)

    def _check(self, stage):
        if stage.key.executor in self._lost or stage.disconnected:
            raise IoBudgetError("backup_io_executor_lost")
        if self._active is not stage or stage.finished:
            raise IoBudgetError("backup_io_stage_inactive")

    async def charge(self, stage, sequence, direction, size):
        self._check(stage)
        if (
            type(sequence) is not int
            or sequence < 1
            or direction not in {"read", "write"}
            or type(size) is not int
            or not 0 <= size <= CHUNK
        ):
            raise IoBudgetError("backup_io_request_invalid")
        request = (sequence, direction, size)
        if sequence == stage.sequence:
            if stage.charge_request != request:
                raise IoBudgetError("backup_io_sequence_mismatch")
        else:
            if sequence != stage.sequence + 1 or (
                stage.charge_task is not None and not stage.charge_task.done()
            ):
                raise IoBudgetError("backup_io_sequence_mismatch")
            stage.sequence, stage.charge_request = sequence, request
            stage.charge_task = asyncio.create_task(self._charge(stage, direction, size))
            stage.charge_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return await asyncio.shield(stage.charge_task)

    async def _charge(self, stage, direction, size):
        while True:
            self._check(stage)
            self._refill()
            if self._tokens >= size:
                self._tokens -= size
                self._charged[direction] += size
                return
            rate = RATE_NORMAL if self._normal else RATE_BUSY
            timer = asyncio.create_task(
                self.clock.sleep(min(SIGNAL_INTERVAL, (size - self._tokens) / rate))
            )
            change = asyncio.create_task(self._changed.wait())
            try:
                await asyncio.wait((timer, change), return_when=asyncio.FIRST_COMPLETED)
            finally:
                timer.cancel()
                change.cancel()
                await asyncio.gather(timer, change, return_exceptions=True)

    def release(self, stage):
        if stage.finished:
            return
        if stage.disconnected:
            raise IoBudgetError("backup_io_exit_unconfirmed")
        if stage.charge_task is not None and not stage.charge_task.done():
            raise IoBudgetError("backup_io_charge_pending")
        self._finish(stage)

    def _finish(self, stage):
        stage.finished = True
        if stage in self._waiting:
            self._waiting.remove(stage)
            stage.ready.cancel()
        if self._active is stage:
            self._active = None
        self._grant()
        self._wake()

    async def finish_requested(self, key):
        """绑定控制通道确认阶段已无 I/O；包括 acquire 回复未到达的情况。"""
        stage = self._stages.get(key)
        if stage is None or stage.finished:
            return
        if stage.disconnected:
            raise IoBudgetError("backup_io_exit_unconfirmed")
        task = stage.charge_task
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._finish(stage)

    def disconnected(self, executor):
        self._lost.add(executor)
        for stage in self._stages.values():
            if stage.key.executor == executor and not stage.finished:
                stage.disconnected = True
        self._wake()

    def executor_ended(self, executor, *, confirmed):
        if confirmed is not True:
            raise IoBudgetError("backup_io_exit_unconfirmed")
        self.disconnected(executor)
        for stage in self._stages.values():
            if stage.key.executor == executor and not stage.finished:
                self._finish(stage)

    def forget_operation(self, operation):
        """仅在持久操作已终结且控制面不再接受该编号的新转交后调用。"""
        stages = [value for value in self._stages.values() if value.key.operation == operation]
        if any(not value.finished for value in stages):
            raise IoBudgetError("backup_io_exit_unconfirmed")
        for value in stages:
            del self._stages[value.key]
        remaining = {value.key.executor for value in self._stages.values()}
        self._lost.intersection_update(remaining)

    def status(self):
        self._refill()
        active = self._active
        return {
            "active": None
            if active is None
            else {"operation": active.key.operation, "disconnected": active.disconnected},
            "waiting": len(self._waiting),
            "rate": RATE_NORMAL if self._normal else RATE_BUSY,
            "available": self._tokens,
            "charged": dict(self._charged),
        }
