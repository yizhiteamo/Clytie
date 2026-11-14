"""本体持有调用场景；按方法和记录可见性裁剪注入的记忆后端。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from clytie.sdk.types import MemoryRecord

from .surface import surface


@dataclass
class _Scope:
    scene: str
    conversation_id: str | None = None
    active: bool = True


_current: ContextVar[_Scope | None] = ContextVar("plugin_call_scene", default=None)


@contextmanager
def bind_scene(scene="public_group", conversation_id=None):
    scope = _Scope(scene, conversation_id)
    token = _current.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _current.reset(token)


def current_scene():
    scope = _current.get()
    return scope.scene if scope is not None and scope.active else "public_group"


def current_conversation():
    scope = _current.get()
    return scope.conversation_id if scope is not None and scope.active else None


def capture_scene_binding():
    """本体存储交接保留原租约，不能把已失效场景重新激活。"""
    return _current.get()


@contextmanager
def restore_scene_binding(scope):
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


class _MemoryBase:
    __slots__ = ("_backend", "_permissions", "_private_scenes")

    def __init__(self, backend, permissions, private_scenes):
        self._backend = (
            backend.for_permissions(permissions) if hasattr(backend, "for_permissions") else backend
        )
        self._permissions = frozenset(permissions)
        self._private_scenes = frozenset(private_scenes)

    def _visible(self, record):
        if record is None:
            return False
        record = MemoryRecord.model_validate(record)
        return not record.retracted and (
            record.visibility != "private"
            or (
                "memory.read.private" in self._permissions
                and current_scene() not in {"public_group", "stranger_private"}
                and current_scene() in self._private_scenes
            )
        )


class ReadMemory(_MemoryBase):
    __slots__ = ()

    async def search(self, text, *, limit=10):
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        records = [MemoryRecord.model_validate(r) for r in await self._backend.search(text)]
        return [r for r in records if self._visible(r)][:limit]

    async def get(self, record_id):
        record = await self._backend.get(record_id)
        return MemoryRecord.model_validate(record) if self._visible(record) else None


class WriteMemory(_MemoryBase):
    __slots__ = ()

    async def write(self, draft):
        # 本体负责新建时 auto；可读重复项可以是已有的 confirmed，不能据此拒绝去重。
        return MemoryRecord.model_validate(await self._backend.write(draft))


class ReadWriteMemory(ReadMemory, WriteMemory):
    __slots__ = ()

    async def revise(self, record_id, expected_revision, changes):
        record = await self.get(record_id)
        if record is None or record.basis != "observed":
            raise KeyError("record unavailable")
        return await self._backend.revise(record_id, expected_revision, changes)

    async def retract(self, record_id, expected_revision, reason):
        record = await self.get(record_id)
        if record is None or record.basis != "observed":
            raise KeyError("record unavailable")
        return await self._backend.retract(record_id, expected_revision, reason)


def memory_view(backend, permissions, private_scenes=("owner_private",)):
    readable = any(p in permissions for p in ("memory.read.public", "memory.read.private"))
    writable = "memory.write" in permissions
    cls = ReadWriteMemory if readable and writable else ReadMemory if readable else WriteMemory
    methods = (["search", "get"] if readable else []) + (["write"] if writable else [])
    if readable and writable:
        methods += ["revise", "retract"]
    return surface(cls(backend, permissions, private_scenes), methods)
