import json

import pytest
from tests.test_measurement_evidence import load_tool

load_tool("measurement_recording")
load = load_tool("measurement_load")


async def test_fixed_arrivals_keep_late_failed_and_cancelled_samples():
    now = [0.0]
    recorder = load.Recorder()

    async def wait(deadline):
        now[0] = max(now[0], deadline)

    async def business(index):
        now[0] += 3
        if index == 1:
            raise ValueError("fictional detail must not be serialized")
        return index

    result = await load.requests(
        recorder,
        count=3,
        interval=1,
        start=1,
        origin=0,
        now=lambda: now[0],
        wait=wait,
        operation=business,
    )
    rows = [json.loads(row) for row in recorder.rows["requests.jsonl"].splitlines()]
    assert result is False
    assert [r["scheduled_s"] for r in rows] == [1, 2, 3]
    assert [r["started_s"] for r in rows] == [1, 4, 7]
    assert [r["finished_s"] for r in rows] == [4, 7, 10]
    assert [r["outcome"] for r in rows] == ["ok", "failed", "ok"]
    assert b"fictional" not in recorder.rows["requests.jsonl"]


async def test_cancelling_load_records_current_request_and_propagates():
    import asyncio

    recorder = load.Recorder()

    async def wait(_):
        pass

    async def business(_):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await load.requests(
            recorder,
            count=3,
            interval=1,
            start=0,
            origin=0,
            now=lambda: 0,
            wait=wait,
            operation=business,
        )
    rows = [json.loads(row) for row in recorder.rows["requests.jsonl"].splitlines()]
    assert len(rows) == 1 and rows[0]["outcome"] == "cancelled"


@pytest.mark.parametrize("count, interval", [(0, 1), (True, 1), (1, 0), (1, float("nan"))])
async def test_invalid_schedule_cannot_start_work(count, interval):
    async def forbidden(*_):
        pytest.fail("invalid schedule dispatched a request")

    with pytest.raises(ValueError):
        await load.requests(
            load.Recorder(),
            count=count,
            interval=interval,
            start=0,
            origin=0,
            now=lambda: 0,
            wait=forbidden,
            operation=forbidden,
        )
