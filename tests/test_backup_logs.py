import importlib.util
import json
import subprocess
import sys


def writer(tmp_path, **kwargs):
    assert importlib.util.find_spec("clytie.backup.logs") is not None
    from clytie.backup.logs import RotatingLogWriter

    return RotatingLogWriter(tmp_path, "logs/test/process.log", **kwargs)


def contents(w):
    status = w.status()
    return (
        b"".join((w.home / row["path"]).read_bytes() for row in status["archives"])
        + w.path.read_bytes()
    )


def test_normal_log_rotates_exact_bytes_and_keeps_current_plus_archives(tmp_path):
    w = writer(tmp_path, rotate_bytes=4, archives=2)
    w.write(b"abcdefghijklmnop")
    status = w.status()
    assert w.path.read_bytes() == b"mnop"
    assert len(status["archives"]) == 2
    assert contents(w) == b"efghijklmnop"
    assert all(row["size"] == 4 for row in status["archives"])


def test_audit_never_deletes_and_records_stay_whole(tmp_path):
    w = writer(tmp_path, rotate_bytes=5, archives=1, audit=True)
    records = [json.dumps({"n": i}).encode() + b"\n" for i in range(8)]
    for record in records:
        w.write(record)
    assert contents(w) == b"".join(records)
    assert len(w.status()["archives"]) == 7


def test_unknown_archive_is_never_deleted(tmp_path):
    w = writer(tmp_path, rotate_bytes=4, archives=1)
    w.path.parent.mkdir(parents=True)
    unknown = w.path.with_name(w.path.name + ".archive.unknown")
    unknown.write_bytes(b"keep")
    w.write(b"abcdefghijkl")
    assert unknown.read_bytes() == b"keep"
    assert w.status()["untracked_archives"] == 1


def test_real_process_concurrent_writes_preserve_every_record(tmp_path):
    w = writer(tmp_path, rotate_bytes=64, archives=1000)
    script = """
import sys
from clytie.backup.logs import RotatingLogWriter
w = RotatingLogWriter(sys.argv[1], 'logs/test/process.log', rotate_bytes=64, archives=1000)
for i in range(50):
    w.write(f'{sys.argv[2]}:{i:03}\\n'.encode())
"""
    children = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(i)]) for i in range(4)
    ]
    try:
        assert [child.wait(timeout=30) for child in children] == [0] * 4
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    lines = contents(w).decode().splitlines()
    assert sorted(lines) == sorted(f"{p}:{i:03}" for p in range(4) for i in range(50))


def test_pipe_drain_finishes_after_child_eof_without_touching_control_stdin(tmp_path):
    import threading

    from clytie.backup.logs import copy_log_pipe

    w = writer(tmp_path, rotate_bytes=256, archives=1000)
    script = (
        "import sys; sys.stderr.buffer.write(b'x'*10_000); sys.stderr.flush(); sys.stdin.readline()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stderr=subprocess.PIPE
    )
    results = []
    thread = threading.Thread(target=lambda: results.append(copy_log_pipe(child.stderr, w)))
    thread.start()
    try:
        child.stdin.write(b"finish\n")
        child.stdin.close()
        assert child.wait(timeout=30) == 0
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert results == [10_000]
        assert contents(w) == b"x" * 10_000
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        thread.join(timeout=30)
        child.stderr.close()


def test_audit_adapter_rotates_without_losing_callback_or_fields(tmp_path):
    from clytie.sdk.testing import MockClock
    from clytie.supervisor.common import Audit

    audit = Audit(tmp_path, MockClock(), rotate_bytes=50)
    seen = []
    audit.on_entry = seen.append
    audit.append("test_operation", "companion_a", count=1)
    audit.append("test_operation", "companion_a", count=2)
    assert [item["count"] for item in seen] == [1, 2]
    assert len(audit.writer.status()["archives"]) == 1
    assert [json.loads(line)["count"] for line in contents(audit.writer).splitlines()] == [1, 2]


def test_interrupted_explicit_archive_delete_recovers_without_touching_current(
    tmp_path, monkeypatch
):
    from pathlib import Path

    import pytest

    w = writer(tmp_path, rotate_bytes=3, archives=1)
    w.write(b"abcdef")
    old = Path.unlink

    def interrupted(path, *args, **kwargs):
        old(path, *args, **kwargs)
        raise OSError("fictional crash after explicit deletion")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(OSError):
            w.write(b"ghi")
    w.write(b"jkl")
    assert contents(w) == b"defjkl"
    assert w.status().get("deleting") is None


def test_audit_seal_freezes_pre_boundary_volumes_across_real_writers(tmp_path):
    w = writer(tmp_path, rotate_bytes=1024, audit=True)
    script = """
import sys
from clytie.backup.logs import RotatingLogWriter
w = RotatingLogWriter(sys.argv[1], 'logs/test/process.log', rotate_bytes=1024, audit=True)
w.write((sys.argv[2] + ':before\\n').encode())
print('ready', flush=True)
sys.stdin.readline()
w.write((sys.argv[2] + ':after\\n').encode())
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(i)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for i in range(2)
    ]
    try:
        assert [child.stdout.readline().strip() for child in children] == ["ready", "ready"]
        sealed = w.seal()
        before = b"".join((w.home / row["path"]).read_bytes() for row in sealed)
        assert sorted(before.decode().splitlines()) == ["0:before", "1:before"]
        for child in children:
            child.communicate("\n", timeout=30)
            assert child.returncode == 0
        assert b"".join((w.home / row["path"]).read_bytes() for row in sealed) == before
        all_sealed = w.seal()
        assert sorted(
            b"".join((w.home / row["path"]).read_bytes() for row in all_sealed)
            .decode()
            .splitlines()
        ) == ["0:after", "0:before", "1:after", "1:before"]
        assert w.seal() == all_sealed
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


def test_threads_preserve_records_and_changed_archive_blocks_deletion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    import pytest

    w = writer(tmp_path, rotate_bytes=32, archives=100)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: w.write(f"record_{i:03}\n".encode()), range(40)))
    assert sorted(contents(w).decode().splitlines()) == [f"record_{i:03}" for i in range(40)]
    first = w.home / w.status()["archives"][0]["path"]
    first.write_bytes(b"changed")
    strict = writer(tmp_path, rotate_bytes=32, archives=1)
    with pytest.raises(ValueError, match="identity_changed"):
        strict.write(b"new")
    assert first.read_bytes() == b"changed"


def test_audit_seal_rejects_unregistered_archive_and_write_errors_propagate(tmp_path, monkeypatch):
    import pytest

    from clytie.sdk.testing import MockClock
    from clytie.supervisor.common import Audit

    audit = Audit(tmp_path, MockClock())
    audit.path.parent.mkdir(parents=True)
    audit.path.with_name(audit.path.name + ".archive.unknown").write_bytes(b"fictional")
    with pytest.raises(ValueError, match="untracked_archive"):
        audit.writer.seal()
    seen = []
    audit.on_entry = seen.append

    def fail(data):
        raise OSError("fictional writer failure")

    monkeypatch.setattr(audit.writer, "write", fail)
    with pytest.raises(OSError):
        audit.append("test")
    assert seen == []


def test_writer_revalidates_link_paths_and_never_resolves_locked_leaf(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest

    w = writer(tmp_path, rotate_bytes=4)
    w.write(b"one")
    original = Path.resolve

    def locked_leaf(path, *args, **kwargs):
        if path.name.endswith(".rotation.lock"):
            raise PermissionError("fictional locked leaf resolution")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", locked_leaf)
        w.write(b"two")
    other = tmp_path / "outside"
    other.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    from clytie.backup.logs import RotatingLogWriter

    with pytest.raises(ValueError):
        RotatingLogWriter(tmp_path, "linked/process.log")


def test_process_owned_fd_drain_preserves_native_stderr_and_protocol_stdout(tmp_path):
    w = writer(tmp_path, rotate_bytes=1024, archives=100)
    script = """
import os, sys
from clytie.backup.logs import RotatingLogWriter, process_logs
w = RotatingLogWriter(sys.argv[1], 'logs/test/process.log', rotate_bytes=1024, archives=100)
with process_logs(w):
    for i in range(100):
        os.write(2, (f'{i:03}:' + 'x'*500 + '\\n').encode())
    print('protocol', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"protocol\r\n" or result.stdout == b"protocol\n"
    assert result.stderr == b""
    assert contents(w).decode().splitlines() == [f"{i:03}:" + "x" * 500 for i in range(100)]


def test_process_owned_drain_reports_write_failure_without_deadlock(tmp_path):
    script = """
import os
from clytie.backup.logs import process_logs
class Failed:
    def write(self, value):
        raise OSError('fictional log failure')
try:
    with process_logs(Failed(), stdout=True):
        for _ in range(100):
            os.write(2, b'x'*(64*1024))
except OSError:
    print('failure observed', flush=True)
else:
    raise AssertionError('failure was lost')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b"failure observed"


def test_restored_audit_index_rebuilds_local_identity_after_hash_verification(tmp_path):
    import hashlib

    import pytest

    w = writer(tmp_path, audit=True)
    w.path.parent.mkdir(parents=True)
    archive = w.path.with_name(w.path.name + ".archive.fictional")
    archive.write_bytes(b"fictional audit\n")
    index = dict(
        schema_version=1,
        audit=True,
        restore_index=True,
        archives=[
            dict(
                path=archive.relative_to(tmp_path).as_posix(),
                size=archive.stat().st_size,
                sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
        ],
    )
    w.index_path.write_text(json.dumps(index), encoding="utf-8")
    rows = w.seal()
    assert rows[0]["inode"] == archive.stat().st_ino
    assert "restore_index" not in json.loads(w.index_path.read_bytes())
    w.index_path.write_text(json.dumps(index), encoding="utf-8")
    archive.write_bytes(b"changed")
    with pytest.raises(ValueError, match="audit_restore_changed"):
        w.seal()
