"""只用临时实例和手写导入器验证搬家、反向修订及 Issue #30。"""

import json

import pytest

from clytie.memory.package import Exports
from clytie.memory.store import MemoryStore
from clytie.memory.transfer import Transfers, runtime_lock
from clytie.plugin_host.locking import OperationBusy
from clytie.sdk.testing import MockClock
from clytie.sdk.types import ImportCandidate

PASSPHRASE = "fictional-memory-passphrase"


@pytest.fixture
def homes(tmp_path):
    stores = []

    def make(name):
        home = tmp_path / name
        store = MemoryStore(
            home / "companions/companion_a/memory/memory.sqlite3",
            clock=MockClock(),
            config={"names": {"internal_scenes": ["owner_private"]}},
            instance={
                "people": {
                    "owner": {
                        "role": "owner",
                        "accounts": [{"channel": "fake-channel", "account": "fictional-owner"}],
                    }
                },
                "conversations": {
                    "owner_chat": {
                        "scene": "owner_private",
                        "kind": "private",
                        "channel": "fake-channel",
                        "platform_id": "fictional-private",
                    }
                },
            },
        )
        stores.append(store)
        return store, home, Transfers(store, home, "companion_a")

    yield make
    for store in stores:
        store.close()


def remember(store, content="虚构的玻璃城旅行", explicit=False):
    return store.write(
        dict(kind="event", content=content, visibility="private"),
        scene="owner_private",
        conversation_id="owner_chat",
        owner="owner",
        operator="owner",
        explicit=explicit,
    )


def revise(store, record, content):
    return store.revise(
        record.record_id,
        record.revision,
        {"content": content},
        scene="owner_private",
        conversation_id="owner_chat",
        operator="owner",
    )


def export(store, home, name="memory.enc"):
    path = home / name
    Exports(store, home, "companion_a").export(path, passphrase=PASSPHRASE)
    return path


async def move(transfers, path):
    plan = await transfers.plan(path, mode="new", passphrase=PASSPHRASE, confirm_identity=True)
    return await transfers.apply(plan["id"], passphrase=PASSPHRASE)


async def test_native_move_identity_history_and_no_content_in_plan(homes):
    source, source_home, _ = homes("source")
    record = revise(source, remember(source), "虚构旅行修正")
    path = export(source, source_home)
    target, _, transfers = homes("target")
    plan = await transfers.plan(path, mode="new", passphrase=PASSPHRASE)
    assert record.content not in json.dumps(plan, ensure_ascii=False)
    raw = transfers.plan_path(plan["id"]).read_text(encoding="utf-8")
    assert record.content not in raw and PASSPHRASE not in raw
    with pytest.raises(ValueError, match="必须确认"):
        await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert target.uid is None
    plan = await transfers.plan(
        path, mode="new", passphrase=PASSPHRASE, confirm_identity=True, show_content=True
    )
    assert plan["content"][record.record_id]["package"] == record.content
    await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert target.uid == source.uid
    assert target.get_raw(record.record_id) == record
    assert target.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
    assert len(target.all_records()) == 1
    # 导入审计不产生额外的叙事记忆。
    assert target.db.execute("SELECT count(*) FROM imports").fetchone()[0] == 1


async def test_foreign_identity_stale_source_database_and_running_lock(homes):
    source, source_home, _ = homes("source")
    remember(source)
    path = export(source, source_home)
    target, home, transfers = homes("target")
    plan = await transfers.plan(path, mode="new", passphrase=PASSPHRASE, confirm_identity=True)
    with runtime_lock(home, "companion_a"):
        with pytest.raises(OperationBusy):
            await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="源数据发生变化"):
        await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    plan = await transfers.plan(path, mode="new", passphrase=PASSPHRASE, confirm_identity=True)
    remember(target, "另一位虚构伴侣的经历")
    with pytest.raises(ValueError, match="计划失效"):
        await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    for mode in ("restore", "merge"):
        with pytest.raises(ValueError, match="不是她的记忆"):
            await transfers.plan(path, mode=mode, passphrase=PASSPHRASE)


async def test_merge_fork_conflict_and_repeat_preserves_later_changes(homes):
    source, source_home, _ = homes("source")
    original = remember(source)
    first = export(source, source_home)
    target, target_home, transfers = homes("target")
    await move(transfers, first)
    remote = revise(source, original, "包里的虚构分叉")
    path = export(source, source_home, "fork.enc")
    local = revise(target, original, "本地的虚构分叉")
    plan = await transfers.plan(path, mode="merge", passphrase=PASSPHRASE)
    assert plan["records"][0]["action"] == "conflict"
    with pytest.raises(ValueError, match="冲突尚未选择"):
        await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    plan = await transfers.plan(
        path, mode="merge", passphrase=PASSPHRASE, choices={original.record_id: "package"}
    )
    await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    merged = target.get_raw(original.record_id)
    assert merged.content == remote.content and merged.revision == local.revision + 1
    assert target.db.execute("SELECT count(*) FROM foreign_revisions").fetchone()[0] == 2
    latest = revise(target, merged, "合并后的本地经历")
    plan = await transfers.plan(path, mode="merge", passphrase=PASSPHRASE)
    assert plan["records"][0]["action"] == "skip"
    await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert target.get_raw(original.record_id) == latest
    moved, _, moving = homes("moved")
    await move(moving, export(target, target_home))
    assert moved.get_raw(original.record_id) == latest
    assert moved.db.execute("SELECT count(*) FROM foreign_revisions").fetchone()[0] == 2


async def test_restore_downgrade_and_undo_only_its_changes_with_conflicts(homes):
    source, source_home, _ = homes("source")
    original = remember(source)
    path = export(source, source_home)
    target, _, transfers = homes("target")
    await move(transfers, path)
    remember(target, explicit=True)
    added = remember(target, "导入后本地新增")
    plan = await transfers.plan(path, mode="restore", passphrase=PASSPHRASE)
    assert plan["downgrade_count"] == 1
    imported = await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert target.get_raw(original.record_id).basis == "owner_stated"
    assert target.get_raw(added.record_id).retracted
    unrelated = remember(target, "恢复之后新记住的事")
    current = target.get_raw(original.record_id)
    changed = revise(target, current, "恢复之后又改过的事")
    undo = transfers.undo(imported)
    assert (
        next(r for r in undo["records"] if r["record_id"] == original.record_id)["action"]
        == "conflict"
    )
    with pytest.raises(ValueError, match="冲突"):
        await transfers.apply(undo["id"])
    undo = transfers.undo(imported, choices={original.record_id: "current"})
    undo_id = await transfers.apply(undo["id"])
    assert target.get_raw(unrelated.record_id) == unrelated
    assert target.get_raw(original.record_id) == changed
    assert not target.get_raw(added.record_id).retracted
    assert target.get_raw(original.record_id).basis == "owner_stated"
    redo = transfers.undo(undo_id)
    await transfers.apply(redo["id"])
    assert target.get_raw(added.record_id).retracted


class FictionalImporter:
    def __init__(self, clock):
        self.clock = clock

    def detect(self, source):
        return source.name == "fictional-source.txt"

    async def read(self, source, cancel):
        cancel.raise_if_cancelled()
        yield ImportCandidate(
            record_id="e" * 32,
            kind="preference",
            content="虚构的旧偏好",
            visibility="public",
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            subject_account={"channel": "fake-channel", "account": "fictional-owner"},
            origin_conversation={
                "channel": "fake-channel",
                "kind": "private",
                "platform_id": "fictional-private",
            },
            proposed_basis="owner_confirmed",
        )


async def test_issue_30_reimport_latest_approval_downgrades_in_one_plan(homes):
    store, home, transfers = homes("target")
    source = home / "fictional-source.txt"
    source.write_text("handwritten fictional source", encoding="utf-8")
    transfers.importers["fictional"] = FictionalImporter(store.clock)

    async def plan(approve):
        return await transfers.plan(
            source,
            mode="new",
            importer="fictional",
            approve_proposals=approve,
            confirm_identity=True,
        )

    first = await plan(True)
    await transfers.apply(first["id"])
    uid = store.uid
    assert store.get_raw("e" * 32).basis == "owner_confirmed"
    second = await plan(False)
    assert second["reimport"] and second["downgrade_count"] == 1
    assert second["records"][0]["action"] == "update"
    assert second["records"][0]["choice"] is None
    await transfers.apply(second["id"])
    downgraded = store.get_raw("e" * 32)
    assert downgraded.basis == "observed" and downgraded.write_mode == "auto"
    assert store.uid == uid
    third = await plan(True)
    imported = await transfers.apply(third["id"])
    undo = transfers.undo(imported)
    await transfers.apply(undo["id"])
    assert store.get_raw("e" * 32).basis == "observed"
    fourth = await plan(True)
    await transfers.apply(fourth["id"])
    revise(store, store.get_raw("e" * 32), "聊天后的新偏好")
    with pytest.raises(ValueError, match="已有其他修订"):
        await plan(False)


async def test_missing_conversation_projects_internally_and_snapshot_failure_is_atomic(
    homes, monkeypatch
):
    source, source_home, _ = homes("source")
    record = remember(source)
    path = export(source, source_home)
    target, _, transfers = homes("target")
    target.instance["conversations"] = {}
    plan = await transfers.plan(path, mode="new", passphrase=PASSPHRASE, confirm_identity=True)

    def failed_snapshot():
        raise ValueError("fictional snapshot failure")

    with monkeypatch.context() as context:
        context.setattr(transfers, "_snapshot", failed_snapshot)
        with pytest.raises(ValueError, match="snapshot failure"):
            await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert target.uid is None and not target.all_records()
    await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert target.get_raw(record.record_id).origin_conversation_id is None
    assert target.meta("missing_origins") == [record.record_id]
    assert target.project("public_group", "unrelated") == []
    assert target.project("owner_private", "owner_chat")


async def test_new_and_fast_forward_preserve_revision_provenance(homes):
    source, home, _ = homes("source")
    remember(source, "另一条保留的记录")
    original = remember(source)
    source.clock.advance(70)
    updated = source.revise(
        original.record_id,
        1,
        {"content": "修订内容"},
        scene="owner_private",
        conversation_id="owner_chat",
        operator="owner",
        trace="fictional-trace",
        reason="fictional-reason",
    )
    target, _, transfers = homes("target")
    target.clock.advance(300)
    await move(transfers, export(source, home))

    def provenance(store):
        return [
            tuple(r)
            for r in store.db.execute(
                "SELECT record_id,revision,data,hash,parent,at,operator,trace,reason FROM revisions ORDER BY record_id,revision"
            )
        ]

    assert provenance(target) == provenance(source)
    source.clock.advance(70)
    source.revise(
        updated.record_id,
        2,
        {"retracted": True},
        scene="owner_private",
        conversation_id="owner_chat",
        operator="owner",
        trace="forget-trace",
        reason="forget-reason",
    )
    plan = await transfers.plan(
        export(source, home, "next.enc"), mode="merge", passphrase=PASSPHRASE
    )
    await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert provenance(target) == provenance(source)
