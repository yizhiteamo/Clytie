"""Startup cannot hand failure back while its owner can still target the loop."""

import asyncio
import threading

import pytest

from clytie.clock import SystemClock
from clytie.plugin_host.support import timed
from clytie.sdk.testing import MockClock
from clytie.storage.owner import StorageFailed, StorageOwner


@pytest.mark.parametrize("cancel_start", [False, True])
async def test_start_failure_or_cancel_waits_for_closed_delivery_and_real_exit(cancel_start):
    # Explicit cross-thread signals: loop-turn counts are not elapsed time.
    # The existing ten-second thread gate remains the test deadlock guard.
    loop = asyncio.get_running_loop()
    factory_entered, delivering_closed = asyncio.Event(), asyncio.Event()
    factory_release, release_delivery = threading.Event(), threading.Event()
    owners, lifecycle = [], []
    clock = MockClock()

    class Observed(StorageOwner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owners.append(self)

        def _deliver(self, future, *args, **kwargs):
            if future is self._closed:
                loop.call_soon_threadsafe(delivering_closed.set)
                assert release_delivery.wait(10)
            return super()._deliver(future, *args, **kwargs)

    class Resources:
        def close(self):
            lifecycle.append(("closed", threading.get_ident()))

    def factory():
        loop.call_soon_threadsafe(factory_entered.set)
        assert factory_release.wait(10)
        if not cancel_start:
            raise ValueError("fictional initialization failure")
        return Resources()

    task = asyncio.create_task(Observed.start(factory, companion_id="companion_a", clock=clock))
    try:
        await timed(factory_entered.wait(), SystemClock(), 10)
        if cancel_start:
            task.cancel()
        factory_release.set()
        await timed(delivering_closed.wait(), SystemClock(), 10)
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
