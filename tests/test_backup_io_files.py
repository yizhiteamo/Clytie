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
