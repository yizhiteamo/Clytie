"""Management commit evidence survives process loss without replay."""

import json

import pytest

from clytie.api.management_journal import Journal, ManagementCancelled
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock


@pytest.mark.parametrize("corruption", ["invalid_json", "missing_fields", "wrong_identity"])
def test_corrupt_journal_persists_companion_block_without_replay(tmp_path, corruption):
    from clytie.api.management_journal import ManagementBusy, recover_pending

    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "thaw", clock)
    if corruption == "invalid_json":
        journal.path.write_text("{", encoding="utf-8")
    elif corruption == "missing_fields":
        journal.path.write_text('{"version": 1}', encoding="utf-8")
    else:
        journal.value["companion_id"] = "companion_b"
        journal.save()
    original = journal.path.read_bytes()
    with pytest.raises(ManagementBusy):
        recover_pending(tmp_path, "companion_a", clock)
    assert journal.block_path.is_file()
    assert journal.path.read_bytes() == original
    # Even loss of the damaged journal must not silently lift the durable block.
    journal.path.rename(journal.path.with_suffix(".evidence"))
    with pytest.raises(ManagementBusy):
        recover_pending(tmp_path, "companion_a", clock)
    recover_pending(tmp_path, "companion_b", clock)


def test_cancel_before_commit_and_committed_result_is_not_cancelled(tmp_path):
    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "thaw", clock)
    journal.cancel()
    with pytest.raises(ManagementCancelled):
        journal.checkpoint()
    committed = Journal.create(tmp_path, "companion_b", "thaw", clock)
    committed.committed({"thawed": True})
    committed.cancel()
    committed.checkpoint()
    assert committed.public()["committed"] is True
    assert committed.public()["state"] == "committed"


def test_plan_file_commit_retains_queryable_plan_id_after_response_loss(tmp_path):
    journal = Journal.create(tmp_path, "companion_a", "import/plan", MockClock())
    plan = tmp_path / "run/memory-plans/fictional.json"
    journal.prepare_file(plan, b"{}", {"plan_id": "a" * 32})
    plan.parent.mkdir(parents=True)
    plan.write_bytes(b"{}")
    result = journal.recover()
    assert result["committed"] and result["result"] == {"plan_id": "a" * 32}
    journal.committed({"id": "a" * 32})
    assert journal.public()["result"] == result["result"]


def test_interrupted_temporary_export_is_removed_without_publishing_or_bulk_cleanup(tmp_path):
    journal = Journal.create(tmp_path, "companion_a", "export", MockClock())
    journal.phase("preparing")
    pending = tmp_path / "fictional.enc.pending"
    unrelated = tmp_path / "unrelated.pending"
    journal.temporary(pending)
    pending.write_bytes(b"fictional partial encrypted output")
    unrelated.write_bytes(b"unrelated evidence")
    assert journal.recover()["state"] == "interrupted"
    assert not pending.exists()
    assert unrelated.read_bytes() == b"unrelated evidence"


def test_unsafe_temporary_artifact_blocks_without_deleting_it(tmp_path):
    journal = Journal.create(tmp_path, "companion_a", "export", MockClock())
    pending = tmp_path / "fictional.pending"
    journal.temporary(pending)
    pending.mkdir()
    assert journal.recover()["state"] == "recovery_blocked"
    assert pending.is_dir()


def test_transaction_marker_is_atomic_and_recovery_does_not_replay(tmp_path):
    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "thaw", clock)
    path = tmp_path / "companions/companion_a/memory/memory.sqlite3"
    store = MemoryStore(path, clock=clock, config={}, instance={})
    journal.database(path)
    journal.phase("transaction")
    with store.transaction():
        journal.mark_transaction(store, {"thawed": True})
    store.close()
    recovered = Journal.load(journal.path, clock)
    assert recovered.recover()["state"] == "committed"
    assert recovered.public()["delivery"] == "unconfirmed"
    assert recovered.recover()["state"] == "committed"


def test_rollback_no_marker_is_interrupted_and_bad_database_blocks_only_target(tmp_path):
    clock = MockClock()
    path = tmp_path / "companions/companion_a/memory/memory.sqlite3"
    store = MemoryStore(path, clock=clock, config={}, instance={})
    journal = Journal.create(tmp_path, "companion_a", "thaw", clock)
    journal.database(path)
    journal.phase("transaction")
    with pytest.raises(RuntimeError):
        with store.transaction():
            journal.mark_transaction(store, {"thawed": True})
            raise RuntimeError("fictional failure")
    store.close()
    assert journal.recover()["state"] == "interrupted"
    broken = Journal.create(tmp_path, "companion_b", "thaw", clock)
    bad = tmp_path / "companions/companion_b/memory/memory.sqlite3"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"fictional incomplete database")
    broken.database(bad)
    broken.phase("transaction")
    assert broken.recover()["state"] == "recovery_blocked"
    assert broken.block_path.exists()
    assert not journal.block_path.exists()


def test_file_commit_checked_without_repeating_original_operation(tmp_path):
    journal = Journal.create(tmp_path, "companion_a", "export", MockClock())
    output = tmp_path / "exports/fictional.enc"
    output.parent.mkdir()
    data = b"fictional encrypted artifact"
    journal.prepare_file(output, data)
    output.write_bytes(data)
    assert journal.recover()["state"] == "committed"
    assert output.read_bytes() == data


def test_public_state_and_durable_marker_never_store_result_body(tmp_path):
    journal = Journal.create(tmp_path, "companion_a", "recovery-code", MockClock())
    journal.committed({"recovery_code": "fictional sensitive value", "content": "fictional text"})
    serialized = json.dumps(journal.public()) + journal.path.read_text(encoding="utf-8")
    assert "fictional sensitive value" not in serialized
    assert "fictional text" not in serialized
    assert journal.public()["delivery"] == "unconfirmed"


def test_management_gate_rejects_existing_operation_and_block(tmp_path):
    from clytie.api.management_journal import ManagementBusy, gate, pending

    clock = MockClock()
    with gate(tmp_path, "companion_a"):
        with pytest.raises(ManagementBusy):
            with gate(tmp_path, "companion_a"):
                pass
        Journal.create(tmp_path, "companion_a", "export", clock)
    assert len(pending(tmp_path, "companion_a", clock)) == 1
    assert pending(tmp_path, "companion_b", clock) == []


@pytest.mark.parametrize("snapshot_fault", [None, "missing", "corrupt"])
async def test_import_apply_uses_real_commit_evidence(tmp_path, snapshot_fault):
    from clytie.api.management_journal import current
    from clytie.api.memory import MemoryAPI
    from clytie.memory.package import Exports

    from .test_config import make_home

    home, repo, _, _ = make_home(tmp_path)
    clock = MockClock()
    source_home = tmp_path / "source"
    source = MemoryStore(source_home / "memory.sqlite3", clock=clock, config={}, instance={})
    source.write(
        {"kind": "fact", "content": "虚构搬家测试", "visibility": "public"},
        scene="owner_private",
        conversation_id="test_group",
    )
    package = source_home / "package.enc"
    phrase = "fictional-long-passphrase"
    Exports(source, source_home, "companion_a").export(package, passphrase=phrase)
    source.close()
    api = MemoryAPI(home, repo_root=repo, clock=clock)
    plan = await api.plan(
        "companion_a", package, mode="new", passphrase=phrase, confirm_identity=True
    )
    journal = Journal.create(home, "companion_a", "import/apply", clock)
    token = current.set(journal)
    try:
        imported = await api.apply(plan["id"], passphrase=phrase)
    finally:
        current.reset(token)
    assert journal.value["committed"]
    assert journal.public()["result"] == {"import_id": imported}
    # Simulate journal update lost after the actual SQLite commit.
    journal.value.update(state="transaction", committed=False)
    journal.save()
    backup = next((home / "backups/memory/companion_a").glob("*.enc"))
    import hashlib

    assert journal.value["snapshot"] == {
        "path": str(backup.resolve()),
        "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    }
    if snapshot_fault == "missing":
        backup.unlink()
    elif snapshot_fault == "corrupt":
        backup.write_bytes(b"fictional broken snapshot")
    assert journal.recover()["committed"]
    if snapshot_fault:
        assert journal.value["state"] == "recovery_blocked"
        assert journal.block_path.exists()
        from clytie.api.management_journal import pending

        assert pending(home, "companion_b", clock) == []
    else:
        assert journal.value["state"] == "committed"
        # Completed operations are not revalidated against normally rotated snapshots.
        backup.unlink()
        assert journal.recover()["state"] == "committed"
    with api.open("companion_a") as store:
        assert store.db.execute("SELECT count(*) FROM imports").fetchone()[0] == 1


async def test_real_worker_process_produces_plan_without_daemon_memory_access(tmp_path):
    import asyncio

    from clytie.clock import SystemClock
    from clytie.memory.package import Exports
    from clytie.plugin_host.support import timed
    from clytie.supervisor.processes import python_process

    from .test_config import make_home

    home, _, _, _ = make_home(tmp_path)
    clock = MockClock()
    source_home = tmp_path / "source"
    source = MemoryStore(source_home / "memory.sqlite3", clock=clock, config={}, instance={})
    source.write(
        {"kind": "fact", "content": "虚构进程测试", "visibility": "public"},
        scene="owner_private",
        conversation_id="test_group",
    )
    package = source_home / "package.enc"
    phrase = "fictional-long-passphrase"
    Exports(source, source_home, "companion_a").export(package, passphrase=phrase)
    source.close()
    journal = Journal.create(home, "companion_a", "import/plan", clock)
    executable, env = python_process()
    process = await asyncio.create_subprocess_exec(
        executable,
        "-m",
        "clytie.api.management_worker",
        str(home),
        "companion_a",
        journal.value["id"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        process.stdin.write(
            json.dumps({"source": str(package), "mode": "new", "passphrase": phrase}).encode()
            + b"\n"
        )
        await process.stdin.drain()
        response = json.loads(await timed(process.stdout.readline(), SystemClock(), 30))
        assert response["ok"], response
        assert response["result"]["target_exists"] is False
        assert await timed(process.wait(), SystemClock(), 30) == 0
        persisted = Journal.load(journal.path, clock)
        assert persisted.value["executor"]["pid"] == process.pid
        assert persisted.value["executor"]["started_at"]
        assert persisted.value["committed"]
        assert phrase not in journal.path.read_text(encoding="utf-8")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        process.stdin.close()


def test_interrupted_export_reverts_only_its_unpublished_public_key(tmp_path):
    journal = Journal.create(tmp_path, "companion_a", "export", MockClock())
    output = tmp_path / "exports/fictional.enc"
    public = tmp_path / "secrets/memory-recovery.pub"
    public.parent.mkdir()
    journal.prepare_file(output, b"fictional complete export")
    journal.prepare_public(public, b"fictional public value")
    public.write_bytes(b"fictional public value")
    assert journal.recover()["state"] == "interrupted"
    assert not public.exists()
    assert not output.exists()


def test_published_export_retains_public_key_and_newer_key_is_not_overwritten(tmp_path):
    journal = Journal.create(tmp_path, "companion_a", "export", MockClock())
    output = tmp_path / "fictional.enc"
    public = tmp_path / "public.pub"
    journal.prepare_file(output, b"fictional complete export")
    journal.prepare_public(public, b"fictional public value")
    public.write_bytes(b"fictional public value")
    output.write_bytes(b"fictional complete export")
    assert journal.recover()["committed"]
    other = Journal.create(tmp_path, "companion_b", "export", MockClock())
    other.prepare_file(tmp_path / "other.enc", b"other export")
    other.prepare_public(public, b"other public value")
    public.write_bytes(b"unrelated newer public value")
    assert other.recover()["state"] == "recovery_blocked"
    assert public.read_bytes() == b"unrelated newer public value"


def test_public_key_recovery_uses_the_existing_instance_export_lock(tmp_path):
    from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity

    journal = Journal.create(tmp_path, "companion_a", "export", MockClock())
    public = tmp_path / "secrets/memory-recovery.pub"
    public.parent.mkdir()
    journal.prepare_file(tmp_path / "fictional.enc", b"fictional export")
    journal.prepare_public(public, b"fictional public value")
    public.write_bytes(b"fictional public value")
    with file_lock(tmp_path / "run/memory-recovery.lock", process_identity()):
        with pytest.raises(OperationBusy):
            journal.recover()
        assert public.read_bytes() == b"fictional public value"
    assert journal.recover()["state"] == "interrupted"
    assert not public.exists()


def test_recovery_checks_process_creation_identity_not_pid_alone(tmp_path):
    from clytie.api.management_journal import executor_alive
    from clytie.plugin_host.locking import process_identity

    identity = process_identity()
    assert executor_alive(identity) is True
    assert executor_alive({**identity, "started_at": "2000-01-01T00:00:00+00:00"}) is False
    assert executor_alive({"pid": 2**30, "started_at": identity["started_at"]}) is False


@pytest.mark.parametrize(
    "phase,committed",
    [
        ("before_snapshot", False),
        ("before_commit", False),
        ("after_commit", True),
    ],
)
async def test_real_worker_kill_rolls_back_or_retains_commit_without_finally(
    tmp_path, phase, committed
):
    import asyncio

    from clytie.api.management_journal import ManagementBusy, gate, recover_pending
    from clytie.clock import SystemClock
    from clytie.plugin_host.support import timed
    from clytie.supervisor.processes import python_process

    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", phase, clock)
    executable, env = python_process()
    process = await asyncio.create_subprocess_exec(
        executable,
        "-m",
        "tests.management_crash_fixture",
        str(tmp_path),
        journal.value["id"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        assert (await timed(process.stdout.readline(), SystemClock(), 30)).strip() == b"ready"
        with pytest.raises(ManagementBusy):
            with gate(tmp_path, "companion_a"):
                pass
        process.kill()
        await process.wait()
        recover_pending(tmp_path, "companion_a", clock)
        recovered = Journal.load(journal.path, clock)
        assert recovered.value["committed"] is committed
        assert recovered.value["state"] == ("committed" if committed else "interrupted")
        assert recovered.public()["delivery"] == "unconfirmed"
        if phase == "before_snapshot":
            assert not list((tmp_path / "backups/memory/companion_a").iterdir())
        # A second startup only inspects the resolved result; it cannot invoke the fixture again.
        recover_pending(tmp_path, "companion_a", clock)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("whole_tree", [False, True])
async def test_parent_loss_or_tree_loss_never_starts_a_second_writer(tmp_path, whole_tree):
    import asyncio
    import os
    import signal
    import sys

    from clytie.api.management_journal import ManagementBusy, executor_alive, recover_pending
    from clytie.clock import SystemClock
    from clytie.plugin_host.locking import process_identity
    from clytie.plugin_host.support import timed
    from clytie.supervisor.processes import python_process

    clock = MockClock()
    real_clock = SystemClock()
    journal = Journal.create(tmp_path, "companion_a", "before_commit", clock)
    executable, env = python_process()
    parent = await asyncio.create_subprocess_exec(
        executable,
        "-m",
        "tests.management_parent_fixture",
        str(tmp_path),
        journal.value["id"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **({"start_new_session": True} if sys.platform != "win32" else {}),
    )
    identity = None
    try:
        message = json.loads(await timed(parent.stdout.readline(), real_clock, 30))
        identity = process_identity(message["pid"])
        if whole_tree:
            if sys.platform == "win32":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(parent.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                assert await timed(killer.wait(), real_clock, 30) == 0
            else:
                os.killpg(parent.pid, signal.SIGKILL)
        else:
            parent.kill()
        await timed(parent.wait(), real_clock, 30)
        if not whole_tree:
            assert executor_alive(identity) is True
            with pytest.raises(ManagementBusy):
                recover_pending(tmp_path, "companion_a", clock)
            # Only this test's positively identified surviving worker is terminated.
            assert process_identity(identity["pid"]) == identity
            os.kill(identity["pid"], signal.SIGTERM)

        async def exited():
            while executor_alive(identity) is not False:
                await real_clock.sleep(0.01)

        await timed(exited(), real_clock, 30)
        recover_pending(tmp_path, "companion_a", clock)
        assert Journal.load(journal.path, clock).value["state"] == "interrupted"
        recover_pending(tmp_path, "companion_a", clock)
        assert not journal.block_path.exists()
    finally:
        if identity is not None and executor_alive(identity) is True:
            os.kill(identity["pid"], signal.SIGTERM)
        if parent.returncode is None:
            parent.kill()
            await parent.wait()
