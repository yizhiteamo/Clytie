"""固定频率与本地日历时刻调度；重启没有持久化待执行队列。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from clytie.sdk import CancelToken
from clytie.sdk.schedule import duration_seconds
from clytie.sdk.types import ScheduleDeclaration, Tick

from .events import background


@dataclass
class Job:
    plugin_id: str
    declaration: ScheduleDeclaration
    handler: object
    anchor: datetime
    due: datetime
    period: Decimal | None
    missed: int = 0
    task: object = None
    cancel: object = None


class Scheduler:
    def __init__(self, clock, log, timezone_name="UTC", *, grace=5):
        self.clock, self.log = clock, log
        self.timezone, self.grace = ZoneInfo(timezone_name), grace
        self.jobs, self.worker = {}, None
        self.changed = asyncio.Event()

    def _calendar_next(self, declaration, after):
        day = after.astimezone(self.timezone).date()
        hour, minute = map(int, declaration.at.split(":"))
        while True:
            if not declaration.days or day.isoweekday() in declaration.days:
                local = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
                # 不存在的墙上时刻按分钟向前找到第一个有效时刻；重复时刻只取 fold=0。
                while True:
                    candidate = local.replace(tzinfo=self.timezone, fold=0).astimezone(timezone.utc)
                    if candidate.astimezone(self.timezone).replace(tzinfo=None) == local:
                        break
                    local += timedelta(minutes=1)
                if candidate > after:
                    return candidate
            day += timedelta(days=1)

    def add(self, plugin_id, declaration, handler):
        declaration = ScheduleDeclaration.model_validate(declaration)
        key = (plugin_id, declaration.id)
        if key in self.jobs:
            raise ValueError("duplicate schedule")
        anchor = self.clock.now()
        period = duration_seconds(declaration.every) if declaration.every else None
        due = (
            anchor + timedelta(seconds=float(period))
            if period
            else self._calendar_next(declaration, anchor)
        )
        self.jobs[key] = Job(plugin_id, declaration, handler, anchor, due, period)
        self.changed.set()
        if self.worker is None:
            self.worker = background(self._run())

    def _due(self, job, now):
        if job.period is not None:
            elapsed = Decimal(str((now - job.anchor).total_seconds()))
            latest = int(elapsed // job.period)
            prior = int(Decimal(str((job.due - job.anchor).total_seconds())) / job.period)
            count = max(1, latest - prior + 1)
            scheduled = job.anchor + timedelta(seconds=float(latest * job.period))
            job.due = job.anchor + timedelta(seconds=float((latest + 1) * job.period))
            # datetime 的精度为微秒；下次唤醒必须是可表示的未来时刻。
            if job.due <= now:
                job.due = now + timedelta(microseconds=1)
            return scheduled, count
        count, scheduled = 0, job.due
        while job.due <= now:
            scheduled, count = job.due, count + 1
            job.due = self._calendar_next(job.declaration, job.due)
        return scheduled, count

    async def _invoke(self, job, tick):
        try:
            await job.handler(tick, job.cancel)
        except (Exception, asyncio.CancelledError) as exc:
            self.log.warning(
                "schedule.failed",
                plugin_id=job.plugin_id,
                schedule_id=job.declaration.id,
                error=type(exc).__name__,
            )

    async def _run(self):
        try:
            while self.jobs:
                self.changed.clear()
                now = self.clock.now()
                for key, job in sorted(self.jobs.items()):
                    if job.due > now:
                        continue
                    scheduled, count = self._due(job, now)
                    if job.task and not job.task.done():
                        job.missed += count
                        continue
                    tick = Tick(
                        schedule_id=job.declaration.id,
                        scheduled_at=scheduled,
                        now=now,
                        missed=job.missed + count - 1,
                    )
                    job.missed, job.cancel = 0, CancelToken()
                    job.task = background(self._invoke(job, tick))
                if not self.jobs:
                    break
                delay = max(
                    0, (min(j.due for j in self.jobs.values()) - self.clock.now()).total_seconds()
                )
                timer, changed = (
                    asyncio.create_task(self.clock.sleep(delay)),
                    asyncio.create_task(self.changed.wait()),
                )
                try:
                    await asyncio.wait({timer, changed}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    timer.cancel()
                    changed.cancel()
                    await asyncio.gather(timer, changed, return_exceptions=True)
        finally:
            self.worker = None

    async def remove(self, plugin_id):
        removed = [self.jobs.pop(k) for k in list(self.jobs) if k[0] == plugin_id]
        self.changed.set()
        for job in removed:
            if not job.task or job.task.done():
                continue
            job.cancel.cancel("plugin_disabled")
            timer = asyncio.create_task(self.clock.sleep(self.grace))
            try:
                done, _ = await asyncio.wait({job.task, timer}, return_when=asyncio.FIRST_COMPLETED)
                if job.task not in done:
                    job.task.cancel()
            finally:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
        if not self.jobs and self.worker:
            await self.worker

    async def close(self):
        for plugin_id in sorted({p for p, _ in self.jobs}):
            await self.remove(plugin_id)
        if self.worker:
            await self.worker
