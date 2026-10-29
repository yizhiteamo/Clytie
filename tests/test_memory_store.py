"""手写虚构数据：身份、权限、来源、追加历史与去重。"""

import pytest

from clytie.memory.store import MemoryStore
from clytie.plugin_host.memory import bind_scene, memory_view
from clytie.sdk.context import RevisionConflict
from clytie.sdk.testing import MockClock
from clytie.sdk.types import MemoryDraft, MemoryRecord


@pytest.fixture
def store(tmp_path):
    config = {
        "names": {"internal_scenes": ["owner_private", "family"]},
        "memory": {"private_scenes": ["owner_private"]},
    }
    instance = {
        "conversations": {
            "owner_chat": {"scene": "owner_private", "kind": "private"},
            "group_a": {"scene": "public_group", "kind": "group"},
            "group_b": {"scene": "public_group", "kind": "group"},
            "family_chat": {"scene": "family", "kind": "group"},
        }
    }
    result = MemoryStore(
        tmp_path / "memory.sqlite3", clock=MockClock(), config=config, instance=instance
    )
    yield result
    result.close()


def write(store, content="喜欢香菜", **options):
    return store.write(
        dict(kind="preference", content=content, visibility="public"),
        scene="owner_private",
        conversation_id="owner_chat",
        operator="owner",
        owner="owner",
        **options,
    )


def test_identity_begins_with_first_record_and_survives_retraction(store):
    assert store.uid is None
    with store.transaction():
        store.audit("initialized")
    assert store.uid is None
    record = write(store)
    uid = store.uid
    assert len(uid) == 32
    store.revise(
        record.record_id,
        1,
        {"retracted": True},
        operator="owner",
        scene="owner_private",
        conversation_id="owner_chat",
    )
    assert store.uid == uid
    assert not store.project("owner_private", "owner_chat")
    assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2


def test_dedupe_upgrade_preserves_identity_and_history(store):
    record = write(store)
    duplicate = write(store, "  喜欢香菜  ")
    assert duplicate.record_id == record.record_id
    confirmed = write(store, explicit=True)
    assert confirmed.basis == "owner_confirmed" and confirmed.revision == 2
    assert confirmed.write_mode == "confirmed"
    again = write(store)
    assert again == confirmed
    changed = store.revise(
        confirmed.record_id,
        2,
        {"content": "现在喜欢萝卜"},
        operator="owner",
        scene="owner_private",
        conversation_id="owner_chat",
    )
    assert changed.basis == "owner_confirmed"
    with pytest.raises(RevisionConflict):
        store.revise(
            changed.record_id,
            2,
            {"content": "旧输入"},
            operator="owner",
            scene="owner_private",
            conversation_id="owner_chat",
        )
    assert store.get_raw(changed.record_id) == changed


async def test_plugin_cannot_modify_owner_records_or_smuggle_trusted_fields(store):
    stated = write(store)
    confirmed = write(store, "明年想去虚构岛", explicit=True)
    view = memory_view(store, ["memory.read.private", "memory.write"])
    with bind_scene("owner_private", "owner_chat"):
        for record in (stated, confirmed):
            with pytest.raises(KeyError, match="unavailable"):
                await view.revise(record.record_id, record.revision, {"content": "插件替换"})
            with pytest.raises(KeyError, match="unavailable"):
                await view.retract(record.record_id, record.revision, "插件撤销")
        own = await view.write(
            MemoryDraft(
                kind="fact",
                content="虚构观察",
                visibility="public",
                source_type="group",
                scope="group_b",
            )
        )
        changed = await view.revise(
            own.record_id,
            1,
            {
                "content": "观察修正",
                "basis": "owner_confirmed",
                "origin_conversation_id": "group_b",
            },
        )
        assert changed.basis == "observed" and changed.origin_conversation_id == "owner_chat"


async def test_write_only_does_not_dedupe_unreadable_record(store):
    record = write(store)
    view = memory_view(store, ["memory.write"])
    with bind_scene("owner_private", "owner_chat"):
        new = await view.write(
            MemoryDraft(
                kind="preference",
                content=record.content,
                subject="owner",
                visibility="public",
                source_type="private",
            )
        )
    assert new.record_id != record.record_id
    assert new.basis == "observed"


async def test_origin_projection_cannot_be_changed_by_plugin_description(store):
    view = memory_view(store, ["memory.read.private", "memory.write"])
    with bind_scene("public_group", "group_a"):
        group = await view.write(
            MemoryDraft(
                kind="fact",
                content="虚构考试安排",
                visibility="public",
                source_type="private",
                scope="group_b",
            )
        )
    assert group.origin_conversation_id == "group_a" and group.source_type == "group"
    for scene, conversation, visible in [
        ("public_group", "group_a", True),
        ("public_group", "group_b", False),
        ("owner_private", "owner_chat", True),
        ("family", "family_chat", True),
        ("stranger_private", "stranger_chat", False),
    ]:
        with bind_scene(scene, conversation):
            assert (await view.get(group.record_id) is not None) == visible
    unbound = await view.write(
        MemoryDraft(
            kind="fact",
            content="无绑定观察",
            visibility="public",
            source_type="private",
            scope="group_a",
        )
    )
    assert unbound.origin_conversation_id is None
    assert await view.get(unbound.record_id) is None


def test_legacy_record_basis_is_derived_only_on_read(store):
    data = write(store, explicit=True).model_dump(mode="json")
    data.pop("basis")
    data.pop("origin_conversation_id")
    legacy = MemoryRecord.model_validate(data)
    assert legacy.basis == "owner_confirmed" and legacy.origin_conversation_id is None


def test_failed_first_write_does_not_leave_identity(store):
    store.db.execute("""CREATE TRIGGER reject_record BEFORE INSERT ON records
        BEGIN SELECT RAISE(ABORT,'fictional failure'); END""")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        write(store)
    assert store.uid is None and store.all_records() == []


async def test_plugin_dedup_returns_readable_confirmed_record(store):
    existing = write(store, explicit=True)
    view = memory_view(store, ["memory.read.public", "memory.write"])
    with bind_scene("owner_private", "owner_chat"):
        returned = await view.write(
            MemoryDraft(
                kind=existing.kind,
                content=existing.content,
                subject="owner",
                visibility="public",
                source_type="group",
            )
        )
    assert returned == existing and len(store.all_records()) == 1
    assert returned.basis == "owner_confirmed"


async def test_core_backed_group_view_cannot_read_or_dedup_private_record(store):
    private = store.write(
        dict(kind="fact", content="虚构私密事实", visibility="private"),
        scene="owner_private",
        conversation_id="owner_chat",
        owner="owner",
        operator="owner",
    )
    view = memory_view(store, ["memory.read.private", "memory.write"])
    with bind_scene("public_group", "group_a"):
        assert await view.get(private.record_id) is None
        assert await view.search("虚构私密事实") == []
        written = await view.write(
            MemoryDraft(
                kind="fact",
                content=private.content,
                subject="owner",
                visibility="private",
                source_type="private",
            )
        )
    assert written.record_id != private.record_id
    assert written.origin_conversation_id == "group_a" and written.basis == "observed"
    assert len(store.all_records()) == 2
