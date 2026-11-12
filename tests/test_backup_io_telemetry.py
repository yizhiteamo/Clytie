import asyncio
from types import SimpleNamespace

from clytie.backup.io_channel import pressure_updates
from clytie.sdk.testing import MockClock


async def test_pressure_uses_owner_cache_each_interval_and_stops_without_database():
    clock = MockClock()
    runtime = SimpleNamespace(storage_owner=None)
    emitted = []
    calls = []

    def sample():
        calls.append(clock.monotonic())
        return {"source": "owner_control_cache"}

    task = asyncio.create_task(pressure_updates(runtime, clock, emitted.append))
    try:
        await clock.advance_and_drain(0.1)
        assert emitted == []
        runtime.storage_owner = SimpleNamespace(sample_io_pressure=sample)
        await clock.advance_and_drain(0.1)
        await clock.advance_and_drain(0.1)
        assert len(calls) == 2
        assert all(row["type"] == "storage_pressure" for row in emitted)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await clock.advance_and_drain(1)
    assert len(calls) == 2
