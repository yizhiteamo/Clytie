"""普通、主动和工具轮数用尽时均按本次请求可用的工具挑选段落。"""

import pytest

from clytie.agent.appearance import Appearance

from .test_agent import event, finish, make_agent


@pytest.mark.parametrize(
    ("proactive", "permitted", "rounds", "description", "visible"),
    [
        (False, True, 4, "", True),
        (False, False, 4, "fictional appearance", False),
        (False, True, 0, "", False),
        (True, True, 4, "fictional appearance", True),
        (True, False, 4, "", False),
    ],
)
async def test_request_passes_final_capability_names_and_appearance_flag(
    tmp_path, monkeypatch, proactive, permitted, rounds, description, visible
):
    agent, _, model = await make_agent(
        tmp_path,
        changes={
            "permissions": {"owner": ["*"] if permitted or proactive else []},
            "proactive": {"tools": {"echo": {"daily": 1}} if permitted else {}},
            "agent": {"max_tool_rounds": rounds},
        },
    )
    observations = []

    async def capture(cid, request, *, conversation_id, available_tools):
        observations.append(("echo" in available_tools, request.has_appearance))
        return []

    monkeypatch.setattr(agent.host, "context_sections", capture)
    agent.appearance = Appearance(description=description)
    try:
        if proactive:
            await agent.submit("owner_private", topic="fictional", reason="fictional", tools=True)
        else:
            await agent.receive(event(agent))
        await agent.drain()
        (request,) = model.requests
        assert ("echo" in {tool.name for tool in request.tools}) is visible
        assert observations == [(visible, bool(description))]
    finally:
        await finish(agent)
