import asyncio

import pytest

from clytie.sdk.cancellation import Cancelled, CancelToken


def test_parent_cancellation_reaches_descendants_and_preserves_reason():
    parent = CancelToken()
    child = parent.child()
    grandchild = child.child()
    parent.cancel("deadline")
    parent.cancel("different reason")
    for token in (parent, child, grandchild, parent.child()):
        assert token.cancelled
        assert token.reason == "deadline"
        with pytest.raises(Cancelled):
            token.raise_if_cancelled()


def test_child_cancellation_does_not_cancel_parent_or_siblings():
    parent = CancelToken()
    child = parent.child()
    sibling = parent.child()
    child.cancel("independent")
    assert child.cancelled
    assert not parent.cancelled
    assert not sibling.cancelled
    parent.raise_if_cancelled()


@pytest.mark.asyncio
async def test_waiters_are_notified_and_late_wait_returns():
    token = CancelToken()
    started = asyncio.Event()

    async def worker():
        started.set()
        await token.wait()
        token.raise_if_cancelled()

    task = asyncio.create_task(worker())
    await started.wait()
    assert not task.done()
    token.cancel("requested")
    # Python 3.10 的 Task 边界可能把取消子类规范化为 CancelledError。
    with pytest.raises(asyncio.CancelledError):
        await task
    assert token.reason == "requested"
    await token.wait()


@pytest.mark.asyncio
async def test_asyncio_cancellation_is_not_consumed_by_wait():
    token = CancelToken()
    task = asyncio.create_task(token.wait())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not token.cancelled


def test_sdk_cancel_is_not_an_ordinary_exception():
    assert issubclass(Cancelled, asyncio.CancelledError)
    assert not issubclass(Cancelled, Exception)
