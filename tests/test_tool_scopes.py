"""B4b uses fictional actors, configuration and tools only."""

from types import SimpleNamespace

import pytest

from clytie.agent.service import Agent
from clytie.config import load_home

from .test_config import make_home, write_yaml


@pytest.mark.parametrize(
    "kind,role,actor,scope,allowed",
    [
        ("private", "owner", "owner", {"private": ["owner"]}, True),
        ("private", "member", "member", {"private": ["owner"]}, False),
        *[
            ("group", role, role, {"group": ["owner", "peer", "member", "stranger"]}, True)
            for role in ("owner", "peer", "member", "stranger")
        ],
        ("group", "member", "excluded", {"exclude": ["excluded"]}, False),
        ("group", "owner", "owner", {"exclude": ["owner"]}, True),
        ("group", "member", "member", {"group": []}, False),
        ("group", "member", "member", {"private": []}, True),
        ("future", "owner", "owner", {}, False),
        ("future", "member", "member", None, True),
    ],
)
def test_tool_scope_table(kind, role, actor, scope, allowed):
    agent = SimpleNamespace(config={"tool_scopes": {} if scope is None else {"draw_image": scope}})
    assert (
        Agent.scope_allows(
            agent,
            SimpleNamespace(role=role, actor_id=actor),
            "draw_image",
            SimpleNamespace(kind=kind),
        )
        == allowed
    )


@pytest.mark.parametrize(
    "scope",
    [
        [],
        {"draw_image": []},
        {"draw_image": {"unexpected": []}},
        {"draw_image": {"private": ["admin"]}},
        {"draw_image": {"exclude": ["missing"]}},
        {"draw_image": {"exclude": ["owner"]}},
    ],
)
def test_invalid_tool_scope_configuration(tmp_path, scope):
    home, root, instance, companion = make_home(tmp_path)
    companion["tool_scopes"] = scope
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=root)
    assert not report.valid
    assert any("tool_scopes" in error.pointer for error in report.errors)


async def test_excluded_tool_is_hidden_and_denied_even_if_model_calls_it(tmp_path, monkeypatch):
    from clytie.sdk import CancelToken
    from clytie.sdk.types import ToolCall

    from .test_agent import event, finish, make_agent
    from .test_agent_acceptance import ECHO

    agent, _, model = await make_agent(
        tmp_path, ECHO, {"finish": "stop", "text": "fictional reply"}
    )
    agent.config["tool_scopes"] = {"echo": {"group": []}}
    try:
        incoming = event(agent, scene="public_group", mention=True)
        specs, status = await agent.tools(incoming.conversation, incoming.message.sender)
        assert "echo" not in [spec.name for spec in specs]
        assert "echo: permission" in status
        trace = await agent.receive(incoming)
        await agent.drain()
        assert "echo" not in [spec.name for spec in model.requests[0].tools]
        assert not agent.ledger.rows("invocation")
        result = await agent._execute_tool(
            trace,
            incoming.conversation,
            incoming.message.sender,
            ToolCall(call_id="fictional_call", tool="echo", arguments={}),
            specs,
            CancelToken(),
        )
        assert result.status == "denied" and result.error.code == "permission"
        assert trace not in agent.kernel.reactions.heavy_results
    finally:
        await finish(agent)


def test_unknown_tool_is_notice_and_owner_present_config_is_valid(tmp_path):
    home, root, _, companion = make_home(tmp_path)
    companion["tool_scopes"] = {"inactive_tool": {"private": ["owner"]}}
    companion.setdefault("boundary", {})["admission"] = "owner_present"
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=root)
    assert report.valid, report.errors
    codes = {item["code"] for item in report.dialogue_notices}
    assert "tool_scope_unavailable" in codes
    assert "owner_present_requires_membership" in codes
