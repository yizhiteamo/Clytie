"""本体绑定在场身份；异步子调用离开作用域后不能保留读取租约。"""

from contextlib import contextmanager
from contextvars import ContextVar

_present = ContextVar("memory_presence", default=None)


@contextmanager
def bind_presence(conversation_id, actors):
    lease = [conversation_id, frozenset(actors), True]
    token = _present.set(lease)
    try:
        yield
    finally:
        lease[2] = False
        _present.reset(token)


def present_actors(conversation_id):
    lease = _present.get()
    return lease[1] if lease and lease[2] and lease[0] == conversation_id else frozenset()


def capture_presence_binding():
    return _present.get()


@contextmanager
def restore_presence_binding(lease):
    """重新绑定同一租约而非延长租约；退出不修改原调用方的有效标记。"""
    token = _present.set(lease)
    try:
        yield
    finally:
        _present.reset(token)
