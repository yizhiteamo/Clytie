from pathlib import Path
from types import SimpleNamespace

from clytie.sdk.testing import (
    MockClock,
    assert_attention_contract,
    assert_before_attention_contract,
    assert_context_contributor_contract,
    assert_scheduled_handler_contract,
    assert_service_contract,
)
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.testing.samples import sample_actor, sample_event
from clytie.sdk.types import CompanionState, ContextRequest, Tick


async def test_example_behavior_all_declared_contracts(tmp_path):
    clock = MockClock()
    async with plugin_from_manifest(
        Path(__file__).parents[1] / "plugin.json", clock=clock, root=tmp_path
    ) as loaded:
        event = sample_event(clock)
        await assert_before_attention_contract(
            loaded.handler("behavior.before_attention"),
            event,
            CompanionState(companion_id="companion_a", states={}),
        )
        await assert_attention_contract(loaded.handler("behavior.attention"), event, [])
        await assert_context_contributor_contract(
            SimpleNamespace(contribute=loaded.handler("context_section")),
            ContextRequest(scene="public_group", actor=sample_actor(), budget_chars=300),
        )
        await assert_scheduled_handler_contract(
            SimpleNamespace(on_tick=loaded.handler("schedule")),
            Tick(schedule_id="tick", scheduled_at=clock.now(), now=clock.now(), missed=0),
        )
        await assert_service_contract(SimpleNamespace(call=loaded.handler("services")), "get", {})
