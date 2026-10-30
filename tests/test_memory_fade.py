"""仅手写虚构记忆，时间全部由 MockClock 推进。"""

from clytie.memory.fade import reinforce
from clytie.memory.retrieval import Retrieval
from clytie.memory.store import content_hash

from .test_memory_store import store as store


def episode(store, conversation="group_a", kind="episode"):
    return store.write(
        dict(kind=kind, content="虚构纸船活动", visibility="relationship"),
        scene="public_group",
        conversation_id=conversation,
    )


async def test_fade_freeze_reinforce_without_revisions_or_deletion(store):
    record = episode(store)
    before = content_hash(record)
    store.clock.advance(120 * 86_400)
    projected = store.project("public_group", "group_a")[0]
    assert projected.decay.weight == 0.0625
    assert projected.decay.frozen
    assert await Retrieval(store).search("纸船", "public_group", "group_a") == []
    reinforce(store, [projected])
    assert store.project("public_group", "group_a")[0].decay.weight == 1
    row = store.db.execute("SELECT * FROM fade").fetchone()
    assert row["half_life_days"] == 45
    assert content_hash(store.get_raw(record.record_id)) == before
    assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
    store.clock.advance(1000 * 86_400)
    assert store.get_raw(record.record_id) is not None


def test_family_episode_and_jargon_never_fade(store):
    for kind in ("episode", "jargon"):
        episode(store, "family_chat", kind)
    store.clock.advance(1000 * 86_400)
    assert all(r.decay.weight == 1 for r in store.project("family", "family_chat"))


def test_plugin_reserved_tags_and_jargon_scope(store):
    record = store.write(
        dict(
            kind="jargon",
            content="纸船：虚构活动",
            visibility="relationship",
            tags=["core:with_owner"],
            scope="group_b",
        ),
        scene="public_group",
        conversation_id="group_a",
    )
    assert not record.tags
    assert record.scope == "group_a"
    assert not store.project("public_group", "group_b")


async def test_frozen_recall_requires_lexical_and_semantic_match(store):
    from clytie.memory.retrieval import CandidateReader

    class FakeEmbedding:
        async def embed(self, texts, cancel):
            return [[1, 0] for _ in texts]

    record = episode(store)
    retrieval = Retrieval(store, embedder=FakeEmbedding(), embedder_id="fake")
    await retrieval.rebuild()
    store.clock.advance(120 * 86_400)
    assert not await CandidateReader(store, "public_group", "group_a").get([record.record_id])
    assert not await retrieval.search(
        "没有命中的文字", "public_group", "group_a", include_frozen=True
    )
    found = await retrieval.search("纸船", "public_group", "group_a", include_frozen=True)
    assert len(found) == 1 and found[0].decay.frozen


def test_profile_presence_cross_group_and_lease_expiry(store):
    from clytie.memory.presence import bind_presence

    record = episode(store, kind="profile")
    with store.transaction():
        record = record.model_copy(
            update={"subject": "member_a", "origin_conversation_id": None, "tags": ["core:profile"]}
        )
        store._save(record.model_copy(update={"revision": 2}), operator="core:recollect")
    assert not store.project("public_group", "group_b")
    with bind_presence("group_b", {"member_a"}):
        assert store.project("public_group", "group_b")[0].record_id == record.record_id
    assert not store.project("public_group", "group_b")
    assert store.project("owner_private", "owner_chat")
    assert not store.project("stranger_private", "stranger")
    store.clock.advance(120 * 86_400)
    assert store.project("owner_private", "owner_chat")[0].decay.frozen


async def test_context_injection_reinforces_and_labels_jargon(store):
    from types import SimpleNamespace

    from clytie.memory.context import MemoryContext
    from clytie.sdk.types import Conversation

    record = episode(store, kind="jargon")
    context = MemoryContext(store, log=SimpleNamespace(warning=lambda *args: None))
    conversation = Conversation(
        conversation_id="group_a", kind="group", scene="public_group", channel_id="fake"
    )
    result = await context.build("fake", conversation, "纸船")
    assert "这个群里的说法" in result[0].parts[0].text
    assert (
        store.db.execute(
            "SELECT half_life_days FROM fade WHERE record_id=?", (record.record_id,)
        ).fetchone()[0]
        == 45
    )
    private = conversation.model_copy(
        update={"conversation_id": "owner_chat", "kind": "private", "scene": "owner_private"}
    )
    result = await context.build("private", private, "纸船")
    assert "听懂就好，不主动用" in result[0].parts[0].text


async def test_confirmed_profiles_and_jargon_use_retrieval_not_core(store):
    from types import SimpleNamespace

    from clytie.memory.context import MemoryContext
    from clytie.sdk.types import Conversation, RecollectItem

    from .test_memory_recollection import commit

    commit(
        store,
        [
            RecollectItem(kind="profile", content="喜欢虚构纸船", about="speaker_b", evidence=[2]),
            RecollectItem(kind="jargon", content="纸船：虚构暗号", evidence=[1]),
        ],
    )
    for record in store.all_records():
        store.revise(
            record.record_id,
            record.revision,
            {"content": record.content},
            operator="owner",
            explicit=True,
            scene="owner_private",
            conversation_id="owner_chat",
        )
    store.clock.advance(120 * 86_400)
    assert all(r.decay.weight == 1 for r in store.project("owner_private", "owner_chat"))
    context = MemoryContext(store, log=SimpleNamespace(warning=lambda *args: None))
    conversation = Conversation(
        conversation_id="owner_chat", kind="private", scene="owner_private", channel_id="fake"
    )
    unrelated = await context.build("unrelated", conversation, "完全不相干的雨滴")
    assert "纸船" not in str(unrelated)
    recalled = await context.build("related", conversation, "纸船")
    assert "喜欢虚构纸船" in str(recalled) and "听懂就好" in str(recalled)


def test_unknown_non_profile_origin_never_fades(store):
    from clytie.memory.fade import projected

    record = store.write(
        dict(kind="episode", content="虚构往事", visibility="relationship"),
        scene="owner_private",
        conversation_id=None,
    )
    store.clock.advance(120 * 86_400)
    assert projected(store, record).decay.weight == 1


async def test_unrelated_profiles_and_jargon_do_not_consume_recall_budget(store):
    from types import SimpleNamespace

    from clytie.memory.context import MemoryContext
    from clytie.sdk.types import Conversation

    for kind in ("profile", "jargon"):
        for i in range(11):
            record = store.write(
                dict(kind=kind, content=f"虚构无关细节{i}", visibility="relationship"),
                scene="public_group",
                conversation_id="group_a",
            )
            if kind == "profile":
                with store.transaction():
                    store._save(
                        record.model_copy(
                            update={
                                "revision": 2,
                                "tags": ["core:profile"],
                                "subject": f"member_{i}",
                                "origin_conversation_id": None,
                            }
                        ),
                        operator="core:recollect",
                    )
    wanted = episode(store)
    context = MemoryContext(store, log=SimpleNamespace(warning=lambda *args: None))
    for scene, cid, kind in [
        ("owner_private", "owner_chat", "private"),
        ("public_group", "group_a", "group"),
    ]:
        conv = Conversation(conversation_id=cid, kind=kind, scene=scene, channel_id="fake")
        output = await context.build(cid, conv, "纸船")
        assert "虚构纸船活动" in str(output)
        assert "无关细节" not in str(output)
    assert [r[0] for r in store.db.execute("SELECT record_id FROM fade")] == [wanted.record_id]
