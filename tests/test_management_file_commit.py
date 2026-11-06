"""Production MemoryAPI file commits and audit recovery use fictional instances only."""

import json

import pytest

from clytie.api.management_journal import Journal, current
from clytie.api.memory import MemoryAPI
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock

from .test_config import make_home


@pytest.mark.parametrize("operation", ["recovery-code", "export"])
def test_memory_api_published_file_audit_failure_is_recoverable(tmp_path, monkeypatch, operation):
    home, repo, _, _ = make_home(tmp_path)
    clock = MockClock()
    api = MemoryAPI(home, repo_root=repo, clock=clock)
    with api.open("companion_a") as store:
        store.materialize()
        store.write(
            {"kind": "fact", "content": "fictional memory", "visibility": "public"},
            scene="owner_private",
            conversation_id="test_private",
        )
    journal = Journal.create(home, "companion_a", operation, clock)
    original = MemoryStore.audit

    def fail(store, kind, **fields):
        if kind in {"memory_export", "memory_recovery_rotated"}:
            raise OSError("fictional audit failure")
        return original(store, kind, **fields)

    monkeypatch.setattr(MemoryStore, "audit", fail)
    token = current.set(journal)
    try:
        with pytest.raises(OSError, match="fictional audit failure"):
            if operation == "export":
                api.export(
                    "companion_a", home / "fictional.enc", passphrase="fictional-long-passphrase"
                )
            else:
                api.new_recovery_code("companion_a", confirmed=True)
    finally:
        current.reset(token)
    assert journal.value["state"] != "committed"
    monkeypatch.setattr(MemoryStore, "audit", original)
    public = (home / "secrets/memory-recovery.pub").read_bytes()
    result = Journal.load(journal.path, clock).recover()
    assert result["state"] == "committed"
    assert result["delivery"] == "unconfirmed"
    assert (home / "secrets/memory-recovery.pub").read_bytes() == public
    assert Journal.load(journal.path, clock).recover() == result
    with api.open("companion_a") as store:
        entries = [json.loads(row[0]) for row in store.db.execute("SELECT data FROM audit")]
    kind = "memory_export" if operation == "export" else "memory_recovery_rotated"
    assert len([entry for entry in entries if entry["kind"] == kind]) == 1


def test_audited_rotation_never_overwrites_a_later_completed_public_key(tmp_path, monkeypatch):
    home, repo, _, _ = make_home(tmp_path)
    clock = MockClock()
    api = MemoryAPI(home, repo_root=repo, clock=clock)
    journal = Journal.create(home, "companion_a", "recovery-code", clock)

    def lost(*args):
        raise OSError("fictional journal loss")

    monkeypatch.setattr(journal, "committed", lost)
    token = current.set(journal)
    try:
        with pytest.raises(OSError, match="fictional journal loss"):
            api.new_recovery_code("companion_a", confirmed=True)
    finally:
        current.reset(token)
    api.new_recovery_code("companion_a", confirmed=True)
    path = home / "secrets/memory-recovery.pub"
    newer = path.read_bytes()
    recovered = Journal.load(journal.path, clock).recover()
    assert recovered["committed"]
    assert path.read_bytes() == newer


@pytest.mark.parametrize("operation", ["export", "recovery-code"])
@pytest.mark.parametrize("stage", ["public_replaced", "before_audit", "after_audit"])
async def test_real_memory_api_file_kill_recovers_audit_without_reexecution(
    tmp_path, operation, stage
):
    import asyncio

    from clytie.clock import SystemClock
    from clytie.plugin_host.support import timed
    from clytie.supervisor.processes import python_process

    home, repo, _, _ = make_home(tmp_path)
    clock = MockClock()
    api = MemoryAPI(home, repo_root=repo, clock=clock)
    with api.open("companion_a") as store:
        store.materialize()
        store.write(
            {"kind": "fact", "content": "fictional memory", "visibility": "public"},
            scene="owner_private",
            conversation_id="test_private",
        )
    journal = Journal.create(home, "companion_a", operation, clock)
    executable, env = python_process()
    env.pop("CLYTIE_HOME", None)
    process = await asyncio.create_subprocess_exec(
        executable,
        "-m",
        "tests.management_file_crash_fixture",
        str(home),
        str(repo),
        operation,
        journal.value["id"],
        stage,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert (await timed(process.stdout.readline(), SystemClock(), 30)).strip() == b"ready"
        process.kill()
        await timed(process.wait(), SystemClock(), 30)
        published = not (operation == "export" and stage == "public_replaced")
        recovered = Journal.load(journal.path, clock)
        result = recovered.recover()
        assert result["committed"] is published
        assert result["state"] == ("committed" if published else "interrupted")
        assert result["delivery"] == "unconfirmed"
        assert Journal.load(journal.path, clock).recover() == result
        assert (home / "secrets/memory-recovery.pub").exists() is published
        with api.open("companion_a") as store:
            entries = [json.loads(row[0]) for row in store.db.execute("SELECT data FROM audit")]
        kind = "memory_export" if operation == "export" else "memory_recovery_rotated"
        assert len([entry for entry in entries if entry["kind"] == kind]) == int(published)
        assert "fictional-long-passphrase" not in journal.path.read_text()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
