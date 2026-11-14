"""Exercise the bridge/cleanup dependency independently of real process timing."""

import asyncio
import json

import pytest
from tests.measurement_cancel import cancel_at_capture


async def test_capture_gate_opens_before_trial_waits_for_bridge_cleanup(tmp_path):
    started, proceed = asyncio.Event(), asyncio.Event()
    events = []

    async def bridge():
        started.set()
        await proceed.wait()
        events.append("bridge_finished")

    async def trial():
        child = asyncio.create_task(bridge())
        try:
            await asyncio.Event().wait()
        finally:
            assert proceed.is_set()
            await child
            events.append("cleanup_finished")

    task = asyncio.create_task(trial())
    with pytest.raises(asyncio.CancelledError):
        await cancel_at_capture(task, started, proceed, tmp_path)
    assert task.cancelled()
    assert events == ["bridge_finished", "cleanup_finished"]


@pytest.mark.parametrize("entered", [False, True])
@pytest.mark.parametrize("outcome", ["fail", "exception"])
async def test_early_trial_end_remains_failure_with_safe_diagnosis(tmp_path, entered, outcome):
    started, proceed = asyncio.Event(), asyncio.Event()
    (tmp_path / "trial-status.json").write_text(
        json.dumps({"codes": ["measurement_sampler_failed", "fictional_hidden_message"]})
    )

    async def trial():
        if entered:
            started.set()
        if outcome == "exception":
            raise ValueError("fictional_hidden_message")
        return {"result": "fail"}

    task = asyncio.create_task(trial())
    with pytest.raises(AssertionError, match="trial_not_active_at_cancel") as error:
        await cancel_at_capture(task, started, proceed, tmp_path)
    report = json.loads(str(error.value).split(": ", 1)[1])
    assert report["capture_entered"] == entered
    assert report["task"] == ("exception" if outcome == "exception" else "returned")
    assert report["codes"] == ["measurement_sampler_failed", "unknown_code"]
    assert "fictional_hidden_message" not in str(error.value)
    assert proceed.is_set() and task.done()
