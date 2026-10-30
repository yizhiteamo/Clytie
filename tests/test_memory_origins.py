"""平台身份对应仅使用虚构标识，验证修订来源不可混淆。"""

from clytie.memory import origins
from clytie.memory.fade import projected
from clytie.memory.store import content_hash

from .test_memory_transfer import PASSPHRASE, export, move
from .test_memory_transfer import homes as homes


def conversation(platform="fake-a", kind="group"):
    return dict(
        channel="fake-channel",
        kind=kind,
        platform_id=platform,
        scene="public_group" if kind == "group" else "owner_private",
    )


def write(store, cid="group_a", text="虚构纸船群经历", kind="episode"):
    return store.write(
        dict(kind=kind, content=text, visibility="relationship"),
        scene="public_group",
        conversation_id=cid,
    )


async def test_renames_kind_collision_and_same_name_different_group(homes):
    source, home, _ = homes("source")
    source.instance["conversations"].update(
        group_a=conversation(), owner_chat=conversation(kind="private")
    )
    group = write(source)
    private = write(source, "owner_chat", "虚构私聊经历")
    source.instance["conversations"]["game"] = source.instance["conversations"].pop("group_a")
    assert origins.resolve(source, group)[0] == "game"
    target, _, transfers = homes("target")
    target.instance["conversations"].update(
        group_a=conversation("fake-b"), game=conversation(), owner_chat=conversation(kind="private")
    )
    await move(transfers, export(source, home))
    local = write(target, text="本地另一个群的虚构经历")
    assert origins.resolve(target, target.get_raw(group.record_id))[0] == "game"
    assert origins.resolve(target, target.get_raw(private.record_id))[0] == "owner_chat"
    assert {r.record_id for r in target.project("public_group", "group_a")} == {
        local.record_id,
        private.record_id,
    }
    assert group.record_id in {r.record_id for r in target.project("public_group", "game")}
    assert local.record_id not in {r.record_id for r in target.project("public_group", "game")}
    assert content_hash(target.get_raw(group.record_id)) == content_hash(group)
    assert target.get_raw(group.record_id).revision == group.revision


async def test_missing_restore_config_and_reexport_preserve_registry(homes):
    source, home, _ = homes("source")
    source.instance["conversations"]["group_a"] = conversation()
    record = write(source, kind="jargon")
    target, target_home, transfers = homes("target")
    target.instance["conversations"]["group_a"] = conversation("fake-b")
    await move(transfers, export(source, home))
    imported = target.get_raw(record.record_id)
    assert not target.project("public_group", "group_a")
    assert target.project("owner_private", "owner_chat")
    target.clock.advance(120 * 86_400)
    assert projected(target, imported).decay.frozen
    target.instance["conversations"]["renamed"] = conversation()
    assert origins.resolve(target, imported)[0] == "renamed"
    assert origins.scope(target, imported) == "renamed"
    assert content_hash(imported) == content_hash(record)
    target.instance["conversations"]["renamed"]["channel"] = "other-fake-channel"
    assert origins.resolve(target, imported) is None
    moved, _, moving = homes("moved")
    moved.instance["conversations"]["third_name"] = conversation()
    await move(moving, export(target, target_home))
    assert origins.resolve(moved, moved.get_raw(record.record_id))[0] == "third_name"
    assert origins.binding(moved, moved.get_raw(record.record_id)) == origins.binding(
        source, record
    )


async def test_revision_keeps_source_and_undo_restores_binding(homes):
    source, home, _ = homes("source")
    source.instance["conversations"]["group_a"] = conversation()
    record = write(source)
    target, _, transfers = homes("target")
    target.instance["conversations"]["group_a"] = conversation("fake-b")
    await move(transfers, export(source, home))
    current = target.get_raw(record.record_id)
    changed = target.revise(
        current.record_id,
        current.revision,
        {"content": "主人修订后的虚构经历"},
        scene="owner_private",
        conversation_id="owner_chat",
        operator="owner",
        explicit=True,
    )
    assert origins.binding(target, changed) == origins.binding(source, record)
    plan = await transfers.plan(
        export(source, home, "restore.enc"), mode="restore", passphrase=PASSPHRASE
    )
    imported = await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    undo = transfers.undo(imported)
    await transfers.apply(undo["id"])
    assert target.get_raw(record.record_id).content == changed.content
    assert origins.binding(target, target.get_raw(record.record_id)) == origins.binding(
        source, record
    )


async def test_identity_conflict_with_equal_content_requires_choice(homes):
    source, home, _ = homes("source")
    source.instance["conversations"]["group_a"] = conversation()
    record = write(source)
    target, _, transfers = homes("target")
    target.instance["conversations"]["group_a"] = conversation("fake-b")
    with target.transaction():
        target.set_meta("companion_uid", source.uid)
        target._save(record, operator="core:test")
    plan = await transfers.plan(export(source, home), mode="merge", passphrase=PASSPHRASE)
    assert plan["records"][0]["action"] == "conflict"
    plan = await transfers.plan(
        export(source, home, "choice.enc"),
        mode="merge",
        choices={record.record_id: "package"},
        passphrase=PASSPHRASE,
    )
    await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert origins.binding(target, target.get_raw(record.record_id)) == origins.binding(
        source, record
    )
    assert not target.project("public_group", "group_a")


async def test_legacy_package_cannot_infer_identity_from_matching_local_id(homes, tmp_path):
    import hashlib
    import json
    import sqlite3

    from clytie.memory.crypto import b64, decrypt, encrypt, new_recovery, un64
    from clytie.memory.store import canonical

    source, home, _ = homes("source")
    source.instance["conversations"]["group_a"] = conversation()
    record = write(source)
    path = export(source, home)
    package = json.loads(decrypt(path.read_bytes(), passphrase=PASSPHRASE))
    dbfile = tmp_path / "legacy-fixture.sqlite3"
    dbfile.write_bytes(un64(package["files"]["memory.sqlite3"]["data"]))
    with sqlite3.connect(dbfile) as db:
        for table in ("fade", "revision_origins", "conversation_registry"):
            db.execute(f"DROP TABLE {table}")
    data = dbfile.read_bytes()
    package["manifest"]["version"] = 2
    package["files"]["memory.sqlite3"] = {
        "data": b64(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    _, pub = new_recovery()
    path.write_bytes(encrypt(canonical(package).encode(), PASSPHRASE, pub))
    target, _, transfers = homes("target")
    target.instance["conversations"]["group_a"] = conversation()
    await move(transfers, path)
    imported = target.get_raw(record.record_id)
    assert origins.binding(target, imported) is None
    assert not target.project("public_group", "group_a")
    target.clock.advance(120 * 86_400)
    assert projected(target, imported).decay.weight == 1


async def test_status_reports_unmatched_and_resolved_without_content(homes):
    from types import SimpleNamespace

    from clytie.memory.status import notices

    source, home, _ = homes("source")
    source.instance["conversations"]["group_a"] = conversation()
    write(source)
    target, target_home, transfers = homes("target")
    await move(transfers, export(source, home))
    report = SimpleNamespace(
        home=target_home,
        companions=[{"id": "companion_a"}],
        plugin_configs={"companion_a": {"config": target.config}},
        instance=target.instance,
    )
    before = notices(report, clock=target.clock)
    assert any(n["code"] == "memory_missing_origins" and n["count"] == 1 for n in before)
    target.instance["conversations"]["renamed"] = conversation()
    after = notices(report, clock=target.clock)
    assert not any(n["code"] == "memory_missing_origins" for n in after)
    assert any(n["code"] == "memory_origins_resolved" and n["count"] == 1 for n in after)
    assert "虚构纸船" not in str(before) + str(after)
