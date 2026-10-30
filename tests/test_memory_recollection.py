"""回想证据、合并、取消及事务崩溃的离线回归。"""

import pytest

from clytie.memory.recollection import RecollectionCommit, SnapshotReader
from clytie.sdk import Cancelled, CancelToken
from clytie.sdk.types import RecollectItem, RecollectMessage, RecollectWindow

from .test_memory_store import store as store


def window():
    return RecollectWindow(
        scene="public_group",
        conversation_kind="group",
        messages=[
            RecollectMessage(number=1, speaker="speaker_a", role="owner", text="一起折纸船"),
            RecollectMessage(number=2, speaker="speaker_b", role="member", text="我也喜欢纸船"),
        ],
    )


def commit(store, items, **kwargs):
    return RecollectionCommit(store).commit(
        items,
        window(),
        kwargs.pop("reader", None) or SnapshotReader(store.project("public_group", "group_a")),
        cid="group_a",
        actors={"speaker_a": "owner", "speaker_b": "member_a"},
        trace="fake-window",
        progress={"inbound": 2, "intent": 0},
        cancel=kwargs.pop("cancel", CancelToken()),
        **kwargs,
    )


def test_evidence_kinds_owner_tag_and_atomic_progress(store):
    items = [
        RecollectItem(kind="fact", content="无效事实", evidence=[1]),
        RecollectItem(kind="preference", content="无效偏好", evidence=[1]),
        RecollectItem(kind="episode", content="没有出处", evidence=[]),
        RecollectItem(kind="episode", content="错误出处", evidence=[3]),
        RecollectItem(kind="episode", content="错误主人标签", evidence=[2], with_owner=True),
        RecollectItem(kind="episode", content="一起折纸船", evidence=[1]),
    ]
    result = commit(store, items)
    assert (result["written"], result["dropped"]) == (1, 5)
    import json

    audits = [json.loads(r[0]) for r in store.db.execute("SELECT data FROM audit")]
    assert any(a["kind"] == "memory.recollect" and a["dropped"] == 5 for a in audits)
    record = store.all_records()[0]
    assert record.basis == "observed"
    assert list(record.tags) == ["core:with_owner"]
    assert store.db.execute("SELECT count(*) FROM recollect_progress").fetchone()[0] == 1
    commit(store, items)
    assert len(store.all_records()) == 1


def test_cancel_before_commit_writes_nothing(store):
    cancel = CancelToken()
    cancel.cancel("closed")
    with pytest.raises(Cancelled):
        commit(store, [], cancel=cancel)
    assert not store.all_records()
    assert store.db.execute("SELECT count(*) FROM recollect_progress").fetchone()[0] == 0


def test_failure_between_save_and_progress_rolls_back(store, monkeypatch):
    save = store._save

    def crash(*args, **kwargs):
        save(*args, **kwargs)
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(store, "_save", crash)
    item = RecollectItem(kind="episode", content="一起折纸船", evidence=[1])
    with pytest.raises(RuntimeError):
        commit(store, [item])
    assert not store.all_records()
    assert store.uid is None
    assert store.db.execute("SELECT count(*) FROM recollect_progress").fetchone()[0] == 0
    monkeypatch.setattr(store, "_save", save)
    assert commit(store, [item])["written"] == 1


def test_profile_requires_actual_evidence_speaker(store):
    items = [
        RecollectItem(kind="profile", content="喜欢纸船", about="speaker_b", evidence=[1]),
        RecollectItem(kind="profile", content="喜欢纸船", about="speaker_b", evidence=[2]),
    ]
    assert commit(store, items)["written"] == 1
    record = store.all_records()[0]
    assert record.subject == "member_a"
    assert record.origin_conversation_id is None
    assert list(record.tags) == ["core:profile"]


def test_merge_observed_preserves_oldest_and_revisions_but_rejects_owner(store):
    for text in ("第一只纸船", "第二只纸船"):
        commit(store, [RecollectItem(kind="episode", content=text, evidence=[1])])
        store.clock.advance(1)
    before = sorted(store.all_records(), key=lambda r: r.created_at)
    reader = SnapshotReader(store.project("public_group", "group_a"))
    item = RecollectItem(
        kind="episode", content="一起折两只纸船", evidence=[1], merges=list(reader.labels)
    )
    assert commit(store, [item])["written"] == 1
    assert store.get_raw(before[0].record_id).content == item.content
    assert store.get_raw(before[1].record_id).retracted
    assert all(store.get_raw(r.record_id).revision == 2 for r in before)
    first = store.get_raw(before[0].record_id)
    store.revise(
        first.record_id,
        first.revision,
        {"content": first.content},
        operator="owner",
        explicit=True,
        scene="owner_private",
        conversation_id="owner_chat",
    )
    reader = SnapshotReader(store.project("public_group", "group_a"))
    label, confirmed = next(
        (label, r) for label, r in reader.labels.items() if r.record_id == first.record_id
    )
    assert confirmed.basis == "owner_confirmed"
    assert confirmed.revision == store.get_raw(first.record_id).revision
    revisions = store.db.execute("SELECT count(*) FROM revisions").fetchone()[0]
    item = item.model_copy(update={"merges": [label]})
    assert commit(store, [item], reader=reader)["dropped"] == 1
    assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == revisions
    assert store.get_raw(first.record_id) == confirmed


def test_owner_jargon_fades_but_owner_episode_does_not(store):
    from clytie.memory.fade import projected

    commit(
        store,
        [
            RecollectItem(kind="jargon", content="纸船：虚构暗号", evidence=[1]),
            RecollectItem(kind="episode", content="一起折纸船", evidence=[1]),
        ],
    )
    store.clock.advance(120 * 86_400)
    rows = {r.kind: r for r in store.all_records()}
    assert "core:with_owner" not in rows["jargon"].tags
    assert projected(store, rows["jargon"]).decay.frozen
    assert "core:with_owner" in rows["episode"].tags
    assert projected(store, rows["episode"]).decay.weight == 1


async def test_recollection_snapshot_excludes_private_and_other_group(store):
    store.write(
        dict(kind="episode", content="虚构私密原文", visibility="private"),
        scene="owner_private",
        conversation_id="owner_chat",
    )
    store.write(
        dict(kind="episode", content="另一群的虚构经历", visibility="relationship"),
        scene="public_group",
        conversation_id="group_b",
    )
    reader = SnapshotReader(store.project("public_group", "group_a"))
    assert not reader.labels
    assert "虚构私密原文" not in str(await reader.get([]))
