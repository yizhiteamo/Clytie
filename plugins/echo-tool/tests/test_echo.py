from pathlib import Path

import pytest

from clytie.sdk import CancelToken
from clytie.sdk.testing import MockClock, assert_capability_contract
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.testing.samples import sample_call, sample_invocation
from clytie.sdk.types import Denial


@pytest.mark.parametrize("proactive", [False, True])
async def test_echo_manifest_contract_and_validation(tmp_path, proactive):
    async with plugin_from_manifest(
        Path(__file__).parents[1] / "plugin.json", clock=MockClock(), root=tmp_path
    ) as loaded:
        tool = loaded.factory("tools")
        call, inv = sample_call(), sample_invocation()
        if proactive:
            inv = inv.model_copy(update={"actor": None, "origin": "proactive"})
        await assert_capability_contract(tool, call, inv)
        plan = await tool.prepare(call, inv)
        assert (await tool.execute(plan, inv, CancelToken())).content == "fictional"
        invalid = call.model_copy(update={"arguments": {}})
        assert isinstance(await tool.prepare(invalid, inv), Denial)
