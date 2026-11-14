"""捕获调用链的重入检查；ContextVar 随 await 和子任务传播。"""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_captures = ContextVar("storage_capture_scopes", default=frozenset())


def capture_key(path):
    return os.path.normcase(str(Path(path).resolve()))


@contextmanager
def capture_scope(path):
    binding = _captures.set(_captures.get() | {capture_key(path)})
    try:
        yield
    finally:
        _captures.reset(binding)


def reject_capture_reentry(key):
    if key is not None and key in _captures.get():
        raise RuntimeError("storage_capture_reentry")
