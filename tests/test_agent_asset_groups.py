"""工具产物从模型回合到渠道和账本的离线交付验收。"""

import pytest

from clytie.agent.policy import text_parts
from clytie.sdk.types import AssetRef, Receipt

from .test_agent import event, finish, make_agent
from .test_kernel_service import new_record, reply, setup


def asset(number, size=10, media_type="image/png"):
    return AssetRef(asset_id=f"{number:064x}", media_type=media_type, size=size, origin="fictional")


async def asset_agent(tmp_path, results, *, text="fictional reply", max_images=4):
    calls = [{"call_id": key, "tool": "echo", "arguments": {"text": key}} for key in results]
    agent, channel, _ = await make_agent(
        tmp_path,
        {"finish": "tool_calls", "tool_calls": calls},
        {"finish": "stop", "text": text},
        changes={"proactive": {"tools": {"echo": {"daily": 3}}}},
    )
    tool = agent.host.registrations(agent.cid, "tool")[0].implementation
    execute = tool.execute

    async def with_assets(plan, inv, cancel):
        result = await execute(plan, inv, cancel)
        return result.model_copy(update={"assets": results[result.call_id]})

    tool.execute = with_assets
    features = channel.features()
    limits = {
        "image": {"max_bytes": 100, "formats": ["image/png"]},
        "file": {"max_bytes": 100, "formats": ["text/plain"]},
    }
    overrides = {"media_limits": limits}
    if max_images is not None:
        overrides["max_images_per_message"] = max_images
    features = features.model_copy(update=overrides)
    channel.features = lambda: features
    return agent, channel


@pytest.mark.parametrize(
    "sizes,max_images,group_lengths",
    [
        ([10] * 3, 4, [3]),
        ([10] * 6, 4, [4, 2]),
        ([60, 40, 30, 80], 4, [2, 1, 1]),
        ([10] * 3, None, [1, 1, 1]),
    ],
)
async def test_image_intents_respect_channel_count_bytes_and_page_order(
    tmp_path, sizes, max_images, group_lengths
):
    pages = [asset(n, size) for n, size in enumerate(sizes)]
    agent, channel = await asset_agent(tmp_path, {"pages": pages}, max_images=max_images)
    try:
        trace = await agent.receive(event(agent))
        await agent.drain()
        text, *images = channel.sent
        task_id = agent.ledger.get("record", trace)["task_id"]
        assert text.parts[0].text == "fictional reply" and task_id in text.parts[-1].text
        assert [len(intent.parts) for intent in images] == group_lengths
        assert [part.asset for intent in images for part in intent.parts] == pages
        image_ids = {intent.intent_id for intent in images}
        receipts = [
            a["receipt"] for a in agent.ledger.rows("attempt") if a["intent_id"] in image_ids
        ]
        assert [(r["intent_id"], r["status"]) for r in receipts] == [
            (intent.intent_id, "sent") for intent in images
        ]
    finally:
        await finish(agent)


@pytest.mark.parametrize("proactive", [False, True])
async def test_grouping_preserves_result_boundaries_files_and_task_card(tmp_path, proactive):
    a, b, c, d, e = [asset(n) for n in range(5)]
    files = [asset(n, media_type="text/plain") for n in [5, 6]]
    results = {"first": [a, b, *files, c], "second": [d, e]}
    agent, channel = await asset_agent(
        tmp_path, results, text="fictional reply" if proactive else ""
    )
    try:
        if proactive:
            trace = (
                await agent.submit("owner_private", topic="sample", reason="test", tools=True)
            ).trace_id
        else:
            trace = await agent.receive(event(agent))
        await agent.drain()
        task_id = agent.ledger.get("record", trace)["task_id"]
        if proactive:
            text, *media = channel.sent
            assert all(intent.kind == "proactive" for intent in channel.sent)
            assert text_parts(text.parts) == "fictional reply"
        else:
            *media, card = channel.sent
            assert task_id in text_parts(card.parts)
        assert [[part.asset for part in intent.parts] for intent in media] == [
            [a, b],
            [files[0]],
            [files[1]],
            [c],
            [d, e],
        ]
    finally:
        await finish(agent)


@pytest.mark.parametrize("max_images,max_bytes", [(1, 100), (4, 19)])
async def test_delivery_rejects_a_sealed_group_exceeding_current_limits(
    tmp_path, max_images, max_bytes
):
    kernel, channel, _ = setup(tmp_path)
    try:
        record, _, _ = await new_record(kernel)
        parts = [{"type": "image", "asset": asset(n)} for n in range(2)]
        intent = reply(record).model_copy(update={"parts": parts})
        await kernel.seal(record["trace_id"], "ok", [intent])
        features = channel.features().model_copy(
            update={
                "max_images_per_message": max_images,
                "media_limits": {"image": {"max_bytes": max_bytes, "formats": ["image/png"]}},
            }
        )
        channel.features = lambda: features
        await kernel.delivery.deliver(record["trace_id"])
        row = kernel.ledger.get("intent", intent.intent_id)
        assert row["status"] == "failed" and row["error"]["code"] == "asset_rejected"
        assert not channel.text and not kernel.ledger.rows("attempt")
    finally:
        await kernel.bus.drain()
        kernel.ledger.close()


async def test_unknown_image_group_has_one_receipt_and_no_resend(tmp_path):
    pages = [asset(n) for n in range(3)]
    agent, channel = await asset_agent(tmp_path, {"pages": pages})
    send = channel.send

    async def unknown_images(intent, cancel):
        if intent.parts[0].type == "image":
            channel.sent.append(intent)
            return Receipt(intent_id=intent.intent_id, status="unknown", at=agent.clock.now())
        return await send(intent, cancel)

    channel.send = unknown_images
    try:
        trace = await agent.receive(event(agent))
        await agent.drain()
        await agent.kernel.delivery.deliver(trace)
        _, image_group = channel.sent
        assert [p.asset for p in image_group.parts] == pages
        attempts = [
            a for a in agent.ledger.rows("attempt") if a["intent_id"] == image_group.intent_id
        ]
        assert [attempt["receipt"]["status"] for attempt in attempts] == ["unknown"]
        assert agent.ledger.get("intent", image_group.intent_id)["status"] == "unknown"
    finally:
        await finish(agent)
