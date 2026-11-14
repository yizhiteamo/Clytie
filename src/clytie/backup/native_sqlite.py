"""原生 SQLite 校验的阶段和协作取消。"""

from contextlib import contextmanager, nullcontext

from .io_client import _stage, checkpoint, current_client


@contextmanager
def validation_stage():
    """调用方必须在本作用域内创建、使用并实际关闭连接。

    必要提交可能关闭字节收费作用域，但仍须复用已持有的共享阶段。
    不修改收费作用域，也不估算或预扣 SQLite 内部读页。
    """
    client = current_client()
    held = _stage.get()
    if client is None and held is not None:
        client = held["client"]
    with client.stage() if client is not None else nullcontext():
        yield


@contextmanager
def native_checks(db):
    """包围一次原生校验调用；连接仍由调用方在原线程关闭。

    SQLite 会把回调异常转换为 interrupted；回调只记录原异常并请求
    中断，返回 Python 后恢复原异常。VM 指令间隔不代表读取的字节数。
    """
    checkpoint()
    interrupted = None

    def progress():
        nonlocal interrupted
        try:
            checkpoint()
        except BaseException as error:
            interrupted = error
            return 1
        return 0

    db.set_progress_handler(progress, 4096)
    try:
        try:
            yield
        except BaseException:
            if interrupted is not None:
                raise interrupted
            raise
        if interrupted is not None:
            raise interrupted
        checkpoint()
    finally:
        db.set_progress_handler(None, 0)
