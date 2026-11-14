from collections import Counter

import pytest

from clytie.backup.container import MAGIC, pack, unpack
from clytie.backup.io_budget import CHUNK
from clytie.backup.io_client import IoClient, io_scope
from clytie.backup.source import _copy, _entry


def budget_client(calls):
    def request(arguments, cleanup=False):
        calls.append(arguments)
        return {}

    return IoClient(request)


def charges(calls):
    result = Counter()
    for call in calls:
        if call["action"] == "charge":
            assert 0 < call["size"] <= CHUNK
            result[call["direction"]] += call["size"]
    return result


def test_source_copy_and_digest_bill_both_directions_without_nested_stage(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"fictional" * CHUNK)
    destination = tmp_path / "copy"
    calls = []
    with io_scope(budget_client(calls)):
        result = _copy(tmp_path, source, destination, "asset.bin", lambda: None)
    assert charges(calls) == {"read": source.stat().st_size * 2, "write": source.stat().st_size}
    assert [call["action"] for call in calls].count("acquire") == 1
    assert [call["action"] for call in calls].count("release") == 1
    assert result == _entry(destination, "asset.bin")
    assert (destination / "asset.bin").read_bytes() == source.read_bytes()


def test_container_keeps_format_and_charges_chunked_original_bytes(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    data = b"fictional" * CHUNK
    (staging / "data.bin").write_bytes(data)
    package = tmp_path / "backup.enc"
    target = tmp_path / "restored"
    target.mkdir()
    calls = []
    key = bytes(range(32))
    with io_scope(budget_client(calls)):
        pack(staging, package, key, {"purpose": "fictional"})
    assert package.read_bytes().startswith(MAGIC)
    assert charges(calls) == {"read": len(data), "write": package.stat().st_size}
    calls.clear()
    with io_scope(budget_client(calls)):
        result = unpack(package, target, key)
    assert charges(calls) == {"read": package.stat().st_size, "write": len(data)}
    assert result["manifest"] == {"purpose": "fictional"}
    assert (target / "data.bin").read_bytes() == data


def test_phase_checkpoint_is_checked_inside_budget_wait(tmp_path):
    from clytie.backup.io_client import checkpoint

    source = tmp_path / "source"
    source.write_bytes(b"fictional")
    cancelled = False
    calls = []

    def check():
        if cancelled:
            raise InterruptedError("fictional_cancel")

    def request(arguments, cleanup=False):
        nonlocal cancelled
        calls.append(arguments)
        if arguments["action"] == "charge":
            cancelled = True
            checkpoint()
            pytest.fail("budget wait missed phase cancellation")
        return {}

    with io_scope(IoClient(request)), pytest.raises(InterruptedError, match="fictional_cancel"):
        _copy(tmp_path, source, tmp_path / "output", "data.bin", check)
    assert calls[-1]["action"] == "release"
    assert (tmp_path / "output/data.bin").stat().st_size == 0


def test_container_budget_requests_follow_file_chunks_not_crypto_frame_headers(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    for index in range(40):
        (staging / f"file_{index}.bin").write_bytes(b"fictional")
    package = tmp_path / "backup.enc"
    calls = []
    with io_scope(budget_client(calls)):
        pack(staging, package, bytes(range(32)), {})
    writes = [call for call in calls if call["action"] == "charge" and call["direction"] == "write"]
    assert len(writes) == (package.stat().st_size + CHUNK - 1) // CHUNK
    target = tmp_path / "target"
    target.mkdir()
    calls.clear()
    with io_scope(budget_client(calls)):
        unpack(package, target, bytes(range(32)))
    reads = [call for call in calls if call["action"] == "charge" and call["direction"] == "read"]
    assert len(reads) == (package.stat().st_size + CHUNK - 1) // CHUNK


async def test_production_asset_capture_bills_copy_without_throttling_tool_put(tmp_path):
    import asyncio

    from clytie.assets import AssetStore
    from clytie.backup.assets import boundary, capture
    from clytie.sdk.testing import MockClock

    store = AssetStore(tmp_path / "home", MockClock())
    calls = []
    data = b"fictional asset" * CHUNK
    with io_scope(budget_client(calls)):
        ref = await store.bind("companion_a").put(data, "text/plain", "tool")
    assert not calls  # Ordinary tool publication never enters the backup budget.
    _, fixed = boundary(store, "companion_a")
    with io_scope(budget_client(calls)):
        files, assets, missing = await asyncio.to_thread(
            capture, store, "companion_a", fixed, {ref.asset_id}, tmp_path / "copy"
        )
    assert not missing
    assert len(files) == len(assets) == 1
    assert charges(calls) == {"read": len(data), "write": len(data)}
    assert (tmp_path / "copy" / files[0]["path"]).read_bytes() == data


async def test_offline_databases_use_budgeted_sqlite_batches(tmp_path, monkeypatch):
    from tests.test_backup_source import fictional

    from clytie.backup import sqlite_io
    from clytie.backup.source import capture_instance

    home, clock = fictional(tmp_path)
    original = sqlite_io.copy_database
    copied = []

    def copy(source, destination, **kwargs):
        copied.append(source.execute("PRAGMA database_list").fetchone()[2])
        return original(source, destination, **kwargs)

    monkeypatch.setattr(sqlite_io, "copy_database", copy)
    calls = []
    with io_scope(budget_client(calls)):
        result = await capture_instance(
            home,
            clock,
            tmp_path / "stage",
            scope="instance",
            epoch="test_epoch",
            source_instance_id="fictional_instance",
        )
    assert result["manifest"]["completeness"] == "complete", result["missing"]
    assert len(copied) == 2
    assert charges(calls)["write"] > 0


async def test_plugin_sink_releases_slot_before_waiting_for_next_chunk(tmp_path):
    from clytie.backup.providers import NamespaceSink

    calls = []
    sink = NamespaceSink(tmp_path / "plugin")

    async def chunks():
        yield b"a" * (CHUNK * 2)
        assert calls[-1]["action"] == "release"
        yield b"b" * 7

    with io_scope(budget_client(calls)):
        result = await sink.write("state.bin", chunks())
    assert result.size == CHUNK * 2 + 7
    assert charges(calls) == {"write": result.size}
    assert sum(call["action"] == "acquire" for call in calls) == 2


def test_mirror_copy_budget_finishes_before_publication_and_recovery(tmp_path, monkeypatch):
    from tests.test_backup_mirror import reference, setup, source

    manager, primary, target, _ = setup(tmp_path)
    record, digest = source(primary)
    manager.confirm(manager.plan(target, [record])["plan_id"])
    calls = []
    receipt = manager._receipt

    def fail_receipt(*args):
        assert calls[-1]["action"] == "release"
        raise OSError("fictional_crash")

    monkeypatch.setattr(manager, "_receipt", fail_receipt)
    with io_scope(budget_client(calls)):
        result = manager.copy_verified(record, digest, reference=reference, verify=lambda _: True)
    assert result["status"] == "failed"
    assert charges(calls) == {"read": record.size * 3, "write": record.size}
    monkeypatch.setattr(manager, "_receipt", receipt)
    calls.clear()
    with io_scope(budget_client(calls)):
        assert manager.recover_pending()[0]["status"] == "committed"
    assert not calls  # Necessary recovery cannot wait for an artificial token.


def test_restore_verification_handoff_excludes_necessary_completion(tmp_path, monkeypatch):
    from tests.test_backup_restore import configured_package

    from clytie.backup.io_client import _stage, current_client

    manager, package, key, target, workspace = configured_package(
        tmp_path, monkeypatch, missing_credentials=False
    )
    calls = []
    with io_scope(budget_client(calls)):
        plan = manager.plan(package, key, target, workspace)
    assert charges(calls)["read"] > package.stat().st_size
    finish = manager._finish_committed
    entered = []

    def verify_handoff(*args):
        assert current_client() is None
        held = _stage.get()
        assert held is not None
        assert calls[-1] == {"action": "acquire", "stage": held["id"]}
        entered.append(len(calls))
        result = finish(*args)
        # Necessary completion retains the unique slot, with no token wait or
        # nested reacquire; only actual return may release its resources.
        assert len(calls) == entered[-1]
        assert _stage.get() is held
        return result

    monkeypatch.setattr(manager, "_finish_committed", verify_handoff)
    calls.clear()
    with io_scope(budget_client(calls)):
        state = manager.apply(plan["id"])
    assert state["data_state"] == "ready_for_switch"
    assert entered == [len(calls) - 1]
    assert calls[-1]["action"] == "release"
    calls.clear()
    with io_scope(budget_client(calls)):
        assert manager.recover(plan["id"])["data_state"] == "ready_for_switch"
    assert not calls
