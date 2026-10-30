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
