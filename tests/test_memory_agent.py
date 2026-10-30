"""经 Agent、假模型、假平台验证当场记、投影、L6 与私密出站拦截。"""

import json

import pytest

from clytie.agent.policy import text_parts
from clytie.sdk import CancelToken
from clytie.sdk.types import ToolCall

from .test_agent import event, finish, make_agent


def tool(name, **args):
    return {
        "finish": "tool_calls",
        "tool_calls": [{"call_id": "fake-call", "tool": name, "arguments": args}],
    }


def write(
    agent,
    text,
    *,
    visibility="public",
    explicit=False,
    kind="fact",
    conversation_id="owner_private",
):
    return agent.memory.store.write(
        dict(content=text, visibility=visibility, kind=kind),
        scene="owner_private" if conversation_id == "owner_private" else "public_group",
        conversation_id=conversation_id,
        owner="owner",
        operator="owner",
        explicit=explicit,
    )


@pytest.mark.parametrize("explicit,basis", [(False, "owner_stated"), (True, "owner_confirmed")])
async def test_owner_remember_through_agent_is_chat_without_task_card(tmp_path, explicit, basis):
    agent, channel, model = await make_agent(
        tmp_path,
        tool("core_remember", content="喜欢虚构的青色星星", kind="preference", explicit=explicit),
        {"finish": "stop", "text": "记得了"},
    )
    trace = await agent.receive(event(agent, "我喜欢虚构的青色星星"))
    await agent.drain()
    record = agent.memory.store.all_records()[0]
    assert record.subject == "owner" and record.source_ref == trace
    assert record.source_type == "private" and record.origin_conversation_id == "owner_private"
    assert record.basis == basis and record.write_mode == ("confirmed" if explicit else "auto")
    assert agent.memory.store.uid
    ledger = agent.ledger.get("record", trace)
    assert ledger["kind"] == "chat" and ledger["task_id"] is None
    assert len(channel.sent) == 1 and not channel.reacts
    assert any(
        text_parts(m.parts) == "记下了" for m in model.requests[-1].messages if m.role == "tool"
    )
    assert any(
        a["kind"] == "core.tool" and a["tool"] == "core_remember"
        for a in agent.ledger.rows("audit")
    )
    await finish(agent)


async def test_memory_tools_audience_proactive_and_empty_context(tmp_path):
    agent, _, model = await make_agent(tmp_path)
    incoming = event(agent, role="member", scene="public_group", mention=True)
    tools, _ = agent.tools(incoming.conversation, incoming.message.sender)
    names = {t.name for t in tools}
    assert "core_recall" in names and not names & {"core_remember", "core_forget"}
    assert agent.tools(incoming.conversation, incoming.message.sender, proactive=True)[0] == []
    await agent.receive(incoming)
    await agent.drain()
    assert any(text_parts(m.parts) == "你们之间还没有记住的事" for m in model.requests[0].messages)
    assert agent.memory.store.uid is None
    write(agent, "毫不相关的虚构事实")
    messages = await agent.memory.build("test", incoming.conversation, "完全不同的查询")
    assert all("你们之间还没有记住的事" not in text_parts(m.parts) for m in messages)
    assert "毫不相关的虚构事实" not in str(messages)
    stranger = event(agent, role="stranger", scene="stranger_private")
    assert await agent.memory.build("stranger", stranger.conversation, "事实") == []
    await finish(agent)


async def test_replaces_forget_conflict_and_shared_write_limit(tmp_path):
    agent, _, _ = await make_agent(tmp_path, changes={"memory": {"max_writes_per_record": 2}})
    record = write(agent, "原本喜欢虚构雪山", explicit=True)
    incoming = event(agent)
    memory = agent.memory
    await memory.build("trace", incoming.conversation, "雪山")
    result = await memory.execute(
        "trace",
        incoming.conversation,
        incoming.message.sender,
        ToolCall(
            call_id="one",
            tool="core_remember",
            arguments={"kind": "preference", "content": "现在喜欢虚构海岛", "replaces": "[记忆1]"},
        ),
        CancelToken(),
    )
    assert result.content == "记下了"
    updated = memory.store.get_raw(record.record_id)
    assert updated.revision == 2 and updated.content == "现在喜欢虚构海岛"
    result = await memory.execute(
        "trace",
        incoming.conversation,
        incoming.message.sender,
        ToolCall(
            call_id="two", tool="core_forget", arguments={"target": "[记忆1]", "reason": "虚构要求"}
        ),
        CancelToken(),
    )
    assert result.error.code == "revision_conflict"
    await memory.build("trace", incoming.conversation, "海岛")
    result = await memory.execute(
        "trace",
        incoming.conversation,
        incoming.message.sender,
        ToolCall(
            call_id="three",
            tool="core_forget",
            arguments={"target": "[记忆1]", "reason": "虚构要求"},
        ),
        CancelToken(),
    )
    assert "次数已用完" in result.content
    await memory.build("next", incoming.conversation, "海岛")
    result = await memory.execute(
        "next",
        incoming.conversation,
        incoming.message.sender,
        ToolCall(
            call_id="four",
            tool="core_forget",
            arguments={"target": "[记忆1]", "reason": "虚构要求"},
        ),
        CancelToken(),
    )
    assert result.content == "已经不会再提了"
    assert memory.store.get_raw(record.record_id).retracted
    assert not memory.store.project("owner_private", "owner_private")
    assert not await memory.retrieval.search("海岛", "public_group", "test_group")
    await finish(agent)


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("case", ["absent", "found", "private", "retracted"])
async def test_l6_evidence_from_agent_tool_result(tmp_path, case, semantic):
    agent, _, model = await make_agent(
        tmp_path,
        tool("core_recall", query="你上次答应给我画猫"),
        {"finish": "stop", "text": "照回想结果回复"},
    )
    write(agent, "我喜欢虚构的蓝色雨伞")
    if case != "absent":
        record = write(
            agent, "答应在虚构画板上画猫", visibility="private" if case == "private" else "public"
        )
        if case == "retracted":
            agent.memory.store.revise(
                record.record_id,
                1,
                {"retracted": True},
                operator="owner",
                scene="owner_private",
                conversation_id="owner_private",
            )
    if semantic:

        class Embedder:
            async def embed(self, texts, cancel):
                return [[1, 0] if "画猫" in t else [0, 1] for t in texts]

        write(agent, "晴空")
        agent.memory.retrieval.embedder = Embedder()
        agent.memory.retrieval.embedder_id = "fictional-semantic"
        await agent.memory.retrieval.rebuild()
    await agent.receive(event(agent, "你上次答应给我画猫", scene="public_group", mention=True))
    await agent.drain()
    results = [text_parts(m.parts) for m in model.requests[-1].messages if m.role == "tool"]
    assert len(results) == 1
    if case == "found":
        assert "[记忆" in results[0] and "画猫" in results[0]
        assert record.record_id not in results[0]
    else:
        assert results[0] == "记忆和任务记录里都没有找到相关的事"
    await finish(agent)


async def test_private_guard_redacts_rewrite_prompt_and_blocks_second_copy(tmp_path):
    private_text = "虚构私密岛上的青色玻璃城堡"
    agent, channel, model = await make_agent(
        tmp_path, {"finish": "stop", "text": private_text}, {"finish": "stop", "text": private_text}
    )
    write(agent, private_text, visibility="private", explicit=True)
    trace = await agent.receive(event(agent, "聊聊虚构天气", scene="public_group", mention=True))
    await agent.drain()
    assert len(model.requests) == 2
    for request in model.requests:
        assert private_text not in request.model_dump_json()
    assert "包含了不能在这里说的私密内容" in model.requests[-1].model_dump_json()
    record = agent.ledger.get("record", trace)
    assert record["exec_status"] == "error" and record["error"]["code"] == "outbound_blocked"
    assert not channel.sent
    assert private_text not in json.dumps(agent.log.entries, ensure_ascii=False)
    assert private_text not in json.dumps(agent.ledger.rows("audit"), ensure_ascii=False)
    assert agent.memory.guard("虚构私密岛上，的青色\u200b玻璃城堡", "public_group")
    await finish(agent)


async def test_core_budget_whole_records_and_current_call_labels(tmp_path):
    agent, _, _ = await make_agent(
        tmp_path, changes={"memory": {"core_chars": 45, "recall_chars": 45}}
    )
    first = write(agent, "虚构记录甲" * 3, explicit=True)
    second = write(agent, "虚构记录乙" * 3, explicit=True)
    incoming = event(agent)
    rows = await agent.memory.build("trace", incoming.conversation, "无关")
    text = "".join(text_parts(m.parts) for m in rows)
    assert agent.memory.core_overflow == 1
    assert (first.content in text) != (second.content in text)
    assert first.record_id not in text and second.record_id not in text
    assert set(agent.memory.session("trace").references) == {"[记忆1]"}
    await agent.memory.build("trace", incoming.conversation, "无关")
    assert set(agent.memory.session("trace").references) == {"[记忆1]"}
    await finish(agent)


async def test_write_failure_does_not_break_dialogue_or_retry(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path,
        tool("core_remember", content="虚构写入失败", kind="fact"),
        {"finish": "stop", "text": "稍后再记"},
    )
    agent.memory.store.db.execute(
        "CREATE TRIGGER fail_write BEFORE INSERT ON records BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
    )
    trace = await agent.receive(event(agent))
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "ok"
    assert agent.memory.store.uid is None and not agent.memory.store.all_records()
    assert len(channel.sent) == 1 and len(model.requests) == 2
    assert any(
        "这次没有记住" in text_parts(m.parts)
        for m in model.requests[-1].messages
        if m.role == "tool"
    )
    await finish(agent)


async def test_recall_uses_old_visible_tasks_and_limits_calls(tmp_path):
    agent, _, model = await make_agent(
        tmp_path, tool("echo", text="fictional remembered task"), {"finish": "stop", "text": "done"}
    )
    task_trace = await agent.receive(event(agent))
    await agent.drain()
    task_id = agent.ledger.get("record", task_trace)["task_id"]
    agent.clock.advance(2 * 24 * 3600)
    model.turns.extend(
        [tool("core_recall", query="remembered"), {"finish": "stop", "text": "found"}]
    )
    await agent.receive(event(agent, "那件旧任务怎么样了"))
    await agent.drain()
    results = [text_parts(m.parts) for m in model.requests[-1].messages if m.role == "tool"]
    assert any(task_id in text and "remembered" in text for text in results)
    incoming = event(agent)
    for query in ("status", "2000", "exec_status"):
        assert agent.memory.task_recall(query, incoming.conversation) == []
    for _ in range(4):
        result = await agent.memory.execute(
            "limited",
            incoming.conversation,
            incoming.message.sender,
            ToolCall(call_id="recall", tool="core_recall", arguments={"query": "remembered"}),
            CancelToken(),
        )
    assert "次数已用完" in result.content
    await finish(agent)


async def test_proactive_memory_is_projected_to_target_and_has_no_tools(tmp_path):
    agent, _, model = await make_agent(tmp_path)
    private_text = "虚构的只有私聊可读的记忆"
    write(agent, private_text, visibility="private", explicit=True)
    await agent.submit("owner_private", topic="随便聊聊", reason="fictional")
    await agent.drain()
    assert private_text in model.requests[-1].model_dump_json()
    assert not model.requests[-1].tools
    await agent.submit("test_group", topic="随便聊聊", reason="fictional")
    await agent.drain()
    assert private_text not in model.requests[-1].model_dump_json()
    assert not model.requests[-1].tools
    await finish(agent)


@pytest.mark.parametrize("operation", ["core_forget", "core_remember"])
async def test_recall_reference_survives_agent_round_and_core_reordering(tmp_path, operation):
    agent, _, model = await make_agent(
        tmp_path,
        {
            "finish": "tool_calls",
            "tool_calls": [
                {
                    "call_id": "remember",
                    "tool": "core_remember",
                    "arguments": {"kind": "fact", "content": "新的核心", "explicit": True},
                },
                {"call_id": "recall", "tool": "core_recall", "arguments": {"query": "琥珀"}},
            ],
        },
        tool(
            operation,
            **(
                {"target": "[记忆2]", "reason": "虚构撤销要求"}
                if operation == "core_forget"
                else {"replaces": "[记忆2]", "kind": "fact", "content": "修正琥珀"}
            ),
        ),
        {"finish": "stop", "text": "完成"},
    )
    core = write(agent, "旧核心", explicit=True)
    recalled = write(agent, "琥珀")
    agent.clock.advance(1)
    await agent.receive(event(agent, "请处理"))
    await agent.drain()
    assert not agent.memory.store.get_raw(core.record_id).retracted
    result = agent.memory.store.get_raw(recalled.record_id)
    assert result.retracted if operation == "core_forget" else result.content == "修正琥珀"
    context = "\n".join(text_parts(m.parts) for m in model.requests[1].messages)
    assert "[记忆1] 旧核心" in context and "[记忆3] 新的核心" in context
    assert "[记忆2] 琥珀" in context
    assert len(model.requests) == 3
    await finish(agent)


@pytest.mark.parametrize(
    "role,scene",
    [
        ("owner", "owner_private"),
        ("member", "public_group"),
        ("stranger", "stranger_private"),
        ("peer", "public_group"),
    ],
)
async def test_do_not_remember_rule_in_request_without_granting_non_owner_tools(
    tmp_path, role, scene
):
    agent, _, model = await make_agent(tmp_path)
    await agent.receive(event(agent, "这个别记", role=role, scene=scene, mention=True))
    await agent.drain()
    request = model.requests[0]
    rule = "主人说不用记、别记这个时不记；已经在本次记下的，用 core_forget 撤销。"
    assert any(rule in text_parts(m.parts) for m in request.messages if m.role == "system")
    names = {t.name for t in request.tools}
    if role == "owner":
        assert {"core_remember", "core_forget"} <= names
        for spec in request.tools:
            if spec.name in {"core_remember", "core_forget"}:
                assert rule in spec.description + spec.when_not_to_use
    else:
        assert not names & {"core_remember", "core_forget"}
    assert not agent.memory.store.all_records()
    await finish(agent)
