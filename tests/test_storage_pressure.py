import threading

import pytest

from clytie.sdk.testing import MockClock
from clytie.storage.owner import StorageBusy, StorageOwner


class Resources:
    def close(self):
        pass


async def test_pressure_contains_live_and_between_sample_peaks_without_storage_read():
    clock = MockClock()
    owner = await StorageOwner.start(Resources, companion_id="companion_a", clock=clock)
    entered, release = threading.Event(), threading.Event()
    finalizer = owner.reserve_finalizer()

    def slow(*_):
        entered.set()
        assert release.wait(10)

    active = owner.submit(slow)
    try:
        for _ in range(10_000):
            if entered.is_set():
                break
            await clock.sleep(0)
        assert entered.is_set()
        waiting = owner.submit(lambda *_: None, finalizer=finalizer)
        clock.advance(0.025)
        pressure = owner.sample_io_pressure()
        assert pressure["ready_wait_seconds"] == pytest.approx(0.025)
        assert pressure["execution_seconds"] == pytest.approx(0.025)
        assert pressure["finalizer_wait_seconds"] == pytest.approx(0.025)
        assert pressure["ordinary"] == 1
        assert pressure["finalizers"] == 1
        clock.advance(0.010)
        release.set()
        await active.result()
        await waiting.result()
        # A command completing between two control ticks must not vanish from pressure.
        peak = owner.sample_io_pressure()
        assert peak["execution_seconds"] >= 0.035
        assert peak["ready_wait_seconds"] >= 0.035
        assert peak["finalizer_wait_seconds"] >= 0.035
        idle = owner.sample_io_pressure()
        assert idle["execution_seconds"] == idle["ready_wait_seconds"] == 0
        assert idle["finalizer_wait_seconds"] == 0
        assert not idle["finalizer_capacity_blocked"]
        assert not any(key in idle for key in ("context", "scene", "conversation_id", "sql"))
    finally:
        release.set()
        await active.result()
        owner.release_finalizer(finalizer, executor_finished=True, obligations_complete=True)
        await owner.close()


async def test_finalizer_capacity_refusal_is_separate_from_measured_wait():
    clock = MockClock()
    owner = await StorageOwner.start(
        Resources, companion_id="companion_a", clock=clock, finalizer_capacity=1
    )
    token = owner.reserve_finalizer()
    try:
        with pytest.raises(StorageBusy):
            owner.reserve_finalizer()
        blocked = owner.sample_io_pressure()
        assert blocked["finalizer_capacity_blocked"]
        assert blocked["finalizer_wait_seconds"] == 0
        assert not owner.sample_io_pressure()["finalizer_capacity_blocked"]
    finally:
        owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        await owner.close()
