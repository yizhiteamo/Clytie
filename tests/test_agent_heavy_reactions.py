import pytest

from clytie.agent.service import Capability
from clytie.sdk.types import ToolResult

from .test_agent import event, finish, make_agent
from .test_agent_acceptance import ECHO


@pytest.mark.parametrize(
    "status,code,retry",
    [
        ("error", "content_refused", False),
        ("denied", "permission", False),
        ("error", "timeout", False),
        ("error", "content_refused", True),
    ],
)
async def test_heavy_failure_changes_reaction_not_record_or_reply(
    tmp_path, monkeypatch, status, code, retry
):
    turns = [ECHO]
    if retry:
        turns.append(
            {
                "finish": "tool_calls",
                "tool_calls": [
                    {"call_id": "call_b", "tool": "echo", "arguments": {"text": "fictional retry"}}
                ],
            }
        )
    turns.append({"finish": "stop", "text": "fictional final reply"})
    agent, channel, _ = await make_agent(tmp_path, *turns)
    declarations = agent.declarations

    def heavy():
        values = declarations()
        pid, declaration = values["echo"]
        values["echo"] = (pid, {**declaration, "heavy": True})
        return values

    agent.declarations = heavy
    calls = 0

    async def execute(self, plan, inv, cancel):
        nonlocal calls
        calls += 1
        return ToolResult(
            call_id=plan.data["call_id"],
            status="ok" if calls > 1 else status,
            data={},
            content="fictional tool result",
            error=None if calls > 1 else {"code": code, "message": ""},
        )

    monkeypatch.setattr(Capability, "execute", execute)
    try:
        trace = await agent.receive(event(agent))
        await agent.drain()
        assert agent.ledger.get("record", trace)["exec_status"] == "ok"
        assert trace not in agent.kernel.reactions.heavy_results
        assert channel.reacts[-1].emoji == ("✅" if retry else "❌")
        assert channel.sent[-1].parts[0].text == "fictional final reply"
        assert any(a["kind"] == "tool.heavy_result" for a in agent.ledger.rows("audit"))
    finally:
        await finish(agent)
