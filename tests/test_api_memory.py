from types import SimpleNamespace

from clytie.api.operations import memory_operation
from clytie.memory.transfer import runtime_lock

from .test_memory_transfer import PASSPHRASE, export, homes, remember, revise

__all__ = ["homes"]


async def test_hosted_new_merge_and_export_reuse_owned_store(homes):
    source, source_home, _ = homes("source")
    record = remember(source)
    package = export(source, source_home)
    target, home, _ = homes("target")
    host = SimpleNamespace(memory_store=lambda cid: target)
    hosted = SimpleNamespace(cid="companion_a", host=host)
    with runtime_lock(home, "companion_a"):
        plan = await memory_operation(
            home,
            target.clock,
            "companion_a",
            "import/plan",
            {
                "source": str(package),
                "mode": "new",
                "passphrase": PASSPHRASE,
                "confirm_identity": True,
            },
            hosted,
        )
        result = await memory_operation(
            home,
            target.clock,
            "companion_a",
            "import/apply",
            {"plan_id": plan["id"], "passphrase": PASSPHRASE},
            hosted,
        )
        assert result["import_id"] and target.get_raw(record.record_id).content == record.content
        revise(source, record, "虚构的新修改")
        package = export(source, source_home, "changed.enc")
        plan = await memory_operation(
            home,
            target.clock,
            "companion_a",
            "import/plan",
            {"source": str(package), "mode": "merge", "passphrase": PASSPHRASE},
            hosted,
        )
        await memory_operation(
            home,
            target.clock,
            "companion_a",
            "import/apply",
            {"plan_id": plan["id"], "passphrase": PASSPHRASE},
            hosted,
        )
        assert target.get_raw(record.record_id).content == "虚构的新修改"
        exported = await memory_operation(
            home,
            target.clock,
            "companion_a",
            "export",
            {"out": str(home / "online.enc"), "passphrase": PASSPHRASE},
            hosted,
        )
        assert exported


async def test_undo_only_plans_then_apply(homes):
    source, source_home, _ = homes("source")
    record = remember(source)
    target, home, transfer = homes("target")
    plan = await transfer.plan(
        export(source, source_home), mode="new", passphrase=PASSPHRASE, confirm_identity=True
    )
    imported = await transfer.apply(plan["id"], passphrase=PASSPHRASE)
    plan = transfer.undo(imported)
    assert plan["kind"] == "undo" and not target.get_raw(record.record_id).retracted
    await transfer.apply(plan["id"])
    assert target.get_raw(record.record_id).retracted
