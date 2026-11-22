from unittest.mock import AsyncMock

import pytest

from clytie.plugin_host.context import Proactive
from clytie.sdk import ProactiveOutcome, ProactiveSubmission
from clytie.sdk.testing import FakeProactive, MockClock


async def test_sdk_proactive_tools_opt_in_and_paused_return():
    backend = AsyncMock()
    trace = "a" * 32
    backend.submit.return_value = {"status": "accepted", "trace_id": trace}
    api = Proactive(backend)
    assert (await api.submit("example_private", topic="topic", reason="test")).trace_id == trace
    assert not backend.submit.call_args.kwargs.get("tools", False)
    backend.submit.return_value = {"status": "paused"}
    submission = await api.submit("example_private", topic="topic", reason="test", tools=True)
    assert submission == ProactiveSubmission(status="paused")
    assert backend.submit.call_args.kwargs["tools"] is True
    with pytest.raises(ValueError):
        await api.submit("example_private", topic="topic", reason="test", tools="true")
    assert backend.submit.await_count == 2
    backend.result.return_value = {"status": "declined"}
    assert await api.result(trace) == ProactiveOutcome(status="declined")
    backend.result.return_value["content"] = "fictional"
    with pytest.raises(ValueError):
        await api.result(trace)


async def test_fake_proactive_matches_tools_and_pause_contract():
    fake = FakeProactive(MockClock())
    accepted = await fake.submit("example_private", topic="topic", reason="test", tools=True)
    assert accepted.status == "accepted"
    assert (await fake.result(accepted.trace_id)).status == "pending"
    other = FakeProactive(fake.clock)
    other_submission = await other.submit("example_private", topic="another", reason="test")
    assert (await fake.result(other_submission.trace_id)).status == "not_found"
    assert (await other.result(accepted.trace_id)).status == "not_found"
    fake.paused = True
    assert (await fake.submit("example_private", parts=[], reason="test")).status == "paused"
    assert len(fake.intents) == 1
