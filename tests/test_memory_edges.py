"""导入边界、候选可信字段、模型取消与重排契约。"""

import asyncio
import json

import pytest
from pydantic import ValidationError

from clytie.memory.crypto import decrypt
from clytie.memory.package import Exports, unpack
from clytie.memory.retrieval import Retrieval
from clytie.sdk import CancelToken
from clytie.sdk.testing import FakeReranker, assert_reranker_contract
from clytie.sdk.testing.samples import sample_import_candidate, sample_memory
from clytie.sdk.types import ImportCandidate

from .test_memory_transfer import (
    PASSPHRASE,
    FictionalImporter,
    export,
    homes,
    move,
    remember,
    revise,
)

__all__ = ["homes"]


async def test_native_fast_forward_empty_protection_and_withdrawn_identity(homes):
    source, home, _ = homes("source")
    record = remember(source)
    target, _, transfers = homes("target")
    await move(transfers, export(source, home))
    updated = revise(source, record, "后来补充的虚构经历")
    package = export(source, home, "next.enc")
    plan = await transfers.plan(package, mode="merge", passphrase=PASSPHRASE)
    await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    assert target.get_raw(record.record_id) == updated
    source.revise(
        updated.record_id,
        updated.revision,
        {"retracted": True},
        operator="owner",
        scene="owner_private",
        conversation_id="owner_chat",
    )
    empty = export(source, home, "withdrawn.enc")
    for mode in ("restore", "merge"):
        with pytest.raises(ValueError, match="空库"):
            await transfers.plan(empty, mode=mode, passphrase=PASSPHRASE)
    with pytest.raises(ValueError, match="已有身份"):
        await transfers.plan(package, mode="new", passphrase=PASSPHRASE)


async def test_snapshot_retention_and_export_of_imported_history(homes):
    source, home, _ = homes("source")
    remember(source)
    path = export(source, home)
    target, target_home, transfers = homes("target")
    await move(transfers, path)
    for index in range(6):
        target.clock.advance(1)
        plan = await transfers.plan(path, mode="restore", passphrase=PASSPHRASE)
        await transfers.apply(plan["id"], passphrase=PASSPHRASE)
    snapshots = list((target_home / "backups/memory/companion_a").iterdir())
    assert len(snapshots) == 5
    audits = [json.loads(r[0]) for r in target.db.execute("SELECT data FROM audit")]
    assert len([a for a in audits if a["kind"] == "memory_snapshot_removed"]) == 2
    new = export(target, target_home)
    _, db = unpack(decrypt(new.read_bytes(), passphrase=PASSPHRASE), target_home, "companion_a")
    db.close()
    assert list((target_home / "run/memory-temp").iterdir()) == []


async def test_importer_cannot_supply_trusted_fields_or_map_by_name(homes):
    store, home, transfers = homes("target")
    candidate = sample_import_candidate(store.clock).model_dump()
    for field, value in (
        ("subject", "owner"),
        ("origin_conversation_id", "owner_chat"),
        ("basis", "owner_confirmed"),
        ("native_package", True),
    ):
        with pytest.raises(ValidationError):
            ImportCandidate(**candidate, **{field: value})
    candidate = ImportCandidate(
        **{
            **candidate,
            "proposed_basis": "owner_confirmed",
            "subject_account": {"channel": "fake-channel", "account": "owner"},
            "origin_conversation": {
                "channel": "fake-channel",
                "kind": "private",
                "platform_id": "fictional-private",
            },
        }
    )
    mapped = transfers.candidate(candidate, True)
    assert (
        mapped.subject is None
        and mapped.origin_conversation_id is None
        and mapped.basis == "observed"
    )
    source = home / "fictional-source.txt"
    source.write_text("fake source copy", encoding="utf-8")

    class Changing(FictionalImporter):
        changed = False

        async def read(self, source, cancel):
            async for row in super().read(source, cancel):
                yield (
                    row.model_copy(update={"content": "different fictional output"})
                    if self.changed
                    else row
                )

    fake = Changing(store.clock)
    transfers.importers["fictional"] = fake
    plan = await transfers.plan(source, mode="new", importer="fictional", confirm_identity=True)
    fake.changed = True
    with pytest.raises(ValueError, match="候选"):
        await transfers.apply(plan["id"])
    assert store.uid is None


async def test_reranker_contract_and_mock_clock_embedding_timeout(homes):
    store, _, _ = homes("test")
    await assert_reranker_contract(FakeReranker(), "虚构", [sample_memory(store.clock)])
    remembered = remember(store, "虚构餐馆")
    started = asyncio.Event()

    class Hanging:
        async def embed(self, texts, cancel):
            started.set()
            await store.clock.sleep(100)

    retrieval = Retrieval(store, embedder=Hanging(), embedder_id="hanging")
    task = asyncio.create_task(retrieval.search("餐馆", "owner_private", "owner_chat"))
    await started.wait()
    store.clock.advance(30)
    assert await task == [remembered]
    assert retrieval.warnings
    cancelled = CancelToken()
    cancelled.cancel("fictional_cancel")
    with pytest.raises(asyncio.CancelledError):
        await retrieval.search("餐馆", "owner_private", "owner_chat", cancel=cancelled)


def test_recovery_only_public_key_and_bad_password_leaves_no_output(homes):
    store, home, _ = homes("test")
    remember(store)
    with pytest.raises(ValueError, match="12"):
        Exports(store, home, "companion_a").export(home / "never.enc", passphrase="short")
    assert not (home / "never.enc").exists() and not (home / "secrets").exists()
