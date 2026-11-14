"""可信本体的收尾容量交接；只包围持久命令，不包围外部插件调用。"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar("storage_finalization", default=None)
_stopping = ContextVar("storage_stop_lifecycle", default=None)


def reserve_finalizer(store):
    from .handle import StoreHandle

    return store._owner.reserve_finalizer() if isinstance(store, StoreHandle) else None


@contextmanager
def finalizer_scope(token):
    binding = _current.set(token)
    try:
        yield
    finally:
        _current.reset(binding)


def current_finalizer(owner):
    token = _current.get()
    return token if token is not None and token.owner is owner else None


def release_finalizer(token, *, executor_finished, obligations_complete):
    if token is not None:
        token.owner.release_finalizer(
            token, executor_finished=executor_finished, obligations_complete=obligations_complete
        )


class StopFinalization:
    """启动时占现有收尾槽位；只接纳一个已就绪的停止存储命令。"""

    def __init__(self, store):
        self.token = reserve_finalizer(store)
        self.owner = self.token.owner if self.token is not None else None
        self.tasks = set()
        self.busy = self.failed = self.released = False

    @contextmanager
    def scope(self):
        lease = [self, True]
        binding = _stopping.set(lease)
        try:
            yield
        finally:
            lease[1] = False
            _stopping.reset(binding)

    def track(self, task):
        self.tasks.add(task)

        def finished(value):
            self.tasks.discard(value)
            if not value.cancelled():
                value.exception()

        task.add_done_callback(finished)
        return task

    async def callback(self, operation):
        async def invoke():
            with self.scope():
                return await operation

        return await self.track(asyncio.create_task(invoke()))

    async def persist(self, store, function, *args, **kwargs):
        from clytie.backup.coordination import run_sync

        with self.scope():
            return await run_sync(store, function, *args, **kwargs)

    async def execute(self, owner, function, **kwargs):
        from .owner import StorageBusy

        if self.released or self.busy:
            raise StorageBusy()
        self.busy = True

        async def command():
            try:
                kwargs["finalizer"] = self.token
                return await owner.execute(function, **kwargs)
            except BaseException:
                self.failed = True
                raise
            finally:
                self.busy = False

        return await asyncio.shield(self.track(asyncio.create_task(command())))

    async def finish(self):
        from .owner import StorageFailed

        while self.tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self.tasks)), return_exceptions=True
            )
        if self.failed:
            raise StorageFailed()
        release_finalizer(self.token, executor_finished=True, obligations_complete=True)
        self.released = True


async def execute_storage_command(owner, function, **kwargs):
    """只对仍有效且属于同一伴侣的停止回调租约使用停止凭证。"""
    lease = _stopping.get()
    if lease is not None and lease[1] and lease[0].owner is owner:
        return await lease[0].execute(owner, function, **kwargs)
    return await owner.execute(function, **kwargs)
