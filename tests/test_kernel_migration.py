"""仅临时实例：加密失败现场、恢复日志、只读查询与进程锁。"""

import json
import sqlite3

import pytest

from clytie.cli import main
from clytie.kernel.ledger import Ledger, LedgerError
from clytie.kernel.migration import LedgerRuntime, migration_status
from clytie.kernel.query import local_task, lock_held, task_exit_code
from clytie.sdk.testing import MockClock

from .test_kernel_reactions import fixture


def old_ledger(home, clock):
    path = home / "companions/companion_a/ledger/ledger.sqlite3"
    ledger = Ledger(path, "companion_a", clock)
    ledger.db.execute("PRAGMA user_version=0")
    ledger.close()
    return path


async def test_successful_migration_encrypts_snapshot_and_holds_lock(tmp_path):
    clock = MockClock()
    path = old_ledger(tmp_path, clock)

    async def migrate(db, cancel):
        db.execute("CREATE TABLE example(value TEXT)")
        db.execute("INSERT INTO example VALUES ('fictional value')")

    runtime = LedgerRuntime(tmp_path, "companion_a", clock, migrations={1: migrate})
    ledger = await runtime.open()
    assert lock_held(path.parent / ".lock")
    assert ledger.db.execute("SELECT value FROM example").fetchone()[0] == "fictional value"
    snapshots = list((tmp_path / "backups/ledger/companion_a").glob("*/snapshot.enc"))
    assert len(snapshots) == 1
    assert b"SQLite format" not in snapshots[0].read_bytes()
    assert not runtime.journal.exists()
    runtime.close()
    assert not lock_held(path.parent / ".lock")


async def test_failed_migration_preserves_encrypted_scene_then_restores(tmp_path):
    clock = MockClock()
    path = old_ledger(tmp_path, clock)
    original = path.read_bytes()

    async def fail(db, cancel):
        (path.parent / "new-file.txt").write_text("fictional failed scene", encoding="utf-8")
        db.execute("CREATE TABLE never_committed(value TEXT)")
        raise ValueError("fictional failure")

    runtime = LedgerRuntime(tmp_path, "companion_a", clock, migrations={1: fail})
    with pytest.raises(LedgerError, match="failed and restored"):
        await runtime.open()
    record = json.loads(runtime.journal.read_bytes())
    assert record["phase"] == "failed"
    assert path.read_bytes() == original
    assert not (path.parent / "new-file.txt").exists()
    payload = runtime.snapshots.read(tmp_path / record["failed_snapshot"])
    assert "new-file.txt" in payload["files"]
    assert ".lock" not in payload["files"] and ".migration.json" not in payload["files"]
    assert migration_status(tmp_path)[0]["failed_snapshot"] == record["failed_snapshot"]
    blocked = LedgerRuntime(tmp_path, "companion_a", clock, migrations={1: fail})
    with pytest.raises(LedgerError, match="requires user action"):
        await blocked.open()
    assert (
        local_task("abcdef12", home=tmp_path, companion_id="companion_a")["error"]["code"]
        == "ledger_unavailable"
    )


async def test_failed_scene_verification_failure_never_overwrites_current(tmp_path, monkeypatch):
    clock = MockClock()
    path = old_ledger(tmp_path, clock)

    async def fail(db, cancel):
        (path.parent / "evidence.txt").write_text("fictional", encoding="utf-8")
        raise ValueError("fictional failure")

    runtime = LedgerRuntime(tmp_path, "companion_a", clock, migrations={1: fail})
    create = runtime.snapshots.create

    def break_failed(*args, **kwargs):
        if kwargs.get("failed"):
            raise OSError("fictional unavailable backup")
        return create(*args, **kwargs)

    monkeypatch.setattr(runtime.snapshots, "create", break_failed)
    with pytest.raises(LedgerError, match="recovery incomplete"):
        await runtime.open()
    assert (path.parent / "evidence.txt").exists()
    assert json.loads(runtime.journal.read_bytes())["phase"] == "restoring"


def test_query_missing_does_not_create_files(tmp_path):
    result = local_task("abcdef12", home=tmp_path, companion_id="companion_a")
    assert result["error"]["code"] == "ledger_missing" and task_exit_code(result) == 3
    assert list(tmp_path.iterdir()) == []


def test_task_cli_exit_codes_and_read_only(tmp_path, capsys):
    assert main(["task", "invalid", "--home", str(tmp_path), "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_request"
    assert (
        main(["task", "abcdef12", "--home", str(tmp_path), "--companion", "companion_a", "--json"])
        == 3
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "ledger_missing"
    assert list(tmp_path.iterdir()) == []
    assert main(["task"]) == 4


def test_readonly_query_summary_full_and_ambiguous(tmp_path):
    # 手写假消息 fixture 放在标准伴侣目录，查询不改变任何字节。
    base = tmp_path / "companions/companion_a"
    ledger, _, _, clock, trace = fixture(base)
    task_id = ledger.get("record", trace)["task_id"]
    # fixture 路径与 runtime 一致。
    before = ledger.path.read_bytes()
    result = local_task(task_id, home=tmp_path)
    assert result["ok"] and result["task"]["unrecovered"]
    assert "content" not in result["task"]
    assert local_task(task_id, home=tmp_path, full=True)["task"]["content"]["inbound"]["parts"]
    assert ledger.path.read_bytes() == before
    other = Ledger(tmp_path / "companions/companion_b/ledger/ledger.sqlite3", "companion_b", clock)
    with other.transaction():
        r = ledger.get("record", trace)
        r["companion_id"] = "companion_b"
        other.put("record", trace, r)
    assert local_task(task_id, home=tmp_path)["error"]["code"] == "ambiguous"
    ledger.close()
    other.close()


async def test_crash_after_schema_commit_clears_only_residual_journal(tmp_path):
    clock = MockClock()
    path = old_ledger(tmp_path, clock)
    runtime = LedgerRuntime(tmp_path, "companion_a", clock)
    snapshot = runtime.snapshots.create(0, 1)
    runtime.save(
        dict(
            from_version=0,
            to_version=1,
            snapshot=snapshot.relative_to(tmp_path).as_posix(),
            failed_snapshot=None,
            phase="migrating",
            executor={},
            error=None,
        )
    )
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=1")
    db.close()
    ledger = await runtime.open()
    assert not runtime.journal.exists()
    assert ledger.rows("audit")[-1]["kind"] == "migration_committed_journal_removed"
    runtime.close()


@pytest.mark.parametrize(
    "phase,version,expected",
    [("migrating", 0, "failed"), ("migrating", 3, "migrating"), ("restoring", 0, "restoring")],
)
async def test_crash_loading_table_never_retries_migration(tmp_path, phase, version, expected):
    clock = MockClock()
    path = old_ledger(tmp_path, clock)
    runtime = LedgerRuntime(tmp_path, "companion_a", clock)
    snapshot = runtime.snapshots.create(0, 1)
    runtime.save(
        dict(
            from_version=0,
            to_version=1,
            snapshot=snapshot.relative_to(tmp_path).as_posix(),
            failed_snapshot=None,
            phase=phase,
            executor={},
            error=None,
        )
    )
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version={version}")
    db.close()
    with pytest.raises(LedgerError):
        await runtime.open()
    record = json.loads(runtime.journal.read_bytes())
    assert record["phase"] == expected
    if expected == "failed":
        assert record["failed_snapshot"]
        assert runtime.version() == 0
    else:
        assert runtime.version() == version


async def test_runtime_content_and_snapshot_retention_keeps_newest_two(tmp_path):
    clock = MockClock()
    runtime = LedgerRuntime(tmp_path, "companion_a", clock, retention_days=1)
    await runtime.open()
    snapshots = []
    for _ in range(3):
        snapshots.append(runtime.snapshots.create(0, 1))
        clock.advance(1)
    failed = runtime.snapshots.create(0, 1, failed=True)
    runtime.snapshots.cleanup(1, migrated=True)
    assert not snapshots[0].exists()
    assert all(p.exists() for p in snapshots[1:]) and failed.exists()
    await clock.drain()
    clock.advance(2 * 24 * 60 * 60)
    await clock.drain()
    assert not any(p.exists() for p in snapshots) and not failed.exists()
    runtime.close()
    await clock.drain()
