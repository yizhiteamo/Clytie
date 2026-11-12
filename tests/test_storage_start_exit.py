"""Startup cannot hand failure back while its owner can still target the loop."""

import asyncio
import threading

import pytest

from clytie.sdk.testing import MockClock
from clytie.storage.owner import StorageFailed, StorageOwner


async def signalled(event, clock):
    for _ in range(10_000):
        if event.is_set():
            return
        await clock.sleep(0)
    raise AssertionError("owner synchronization signal missing")


@pytest.mark.parametrize("cancel_start", [False, True])
async def test_start_failure_or_cancel_waits_for_closed_delivery_and_real_exit(cancel_start):
    factory_entered, factory_release = threading.Event(), threading.Event()
    delivering_closed, release_delivery = threading.Event(), threading.Event()
    owners, lifecycle = [], []
    clock = MockClock()

    class Observed(StorageOwner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owners.append(self)

        def _deliver(self, future, *args, **kwargs):
            if future is self._closed:
                delivering_closed.set()
                assert release_delivery.wait(10)
            return super()._deliver(future, *args, **kwargs)

    class Resources:
        def close(self):
            lifecycle.append(("closed", threading.get_ident()))

    def factory():
        factory_entered.set()
        assert factory_release.wait(10)
        if not cancel_start:
            raise ValueError("fictional initialization failure")
        return Resources()

    task = asyncio.create_task(Observed.start(factory, companion_id="companion_a", clock=clock))
    try:
        await signalled(factory_entered, clock)
        if cancel_start:
            task.cancel()
        factory_release.set()
        await signalled(delivering_closed, clock)
        for _ in range(20):
            await clock.sleep(0)
        assert not task.done(), "startup returned while owner still delivers into loop"
        if cancel_start:
            task.cancel()  # A second waiter cancellation must not abandon the owner.
            await clock.sleep(0)
            assert not task.done()
        release_delivery.set()
        with pytest.raises(asyncio.CancelledError if cancel_start else StorageFailed):
            await task
        assert owners[0]._closed.done()
        assert not owners[0]._thread.is_alive()
        assert lifecycle == ([("closed", owners[0]._thread.ident)] if cancel_start else [])
    finally:
        factory_release.set()
        release_delivery.set()
        await asyncio.gather(task, return_exceptions=True)
        if owners:
            await asyncio.gather(owners[0].close(), return_exceptions=True)
            while owners[0]._thread.is_alive():
                await clock.sleep(0)
