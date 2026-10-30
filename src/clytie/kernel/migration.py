"""账本启动锁、认证加密快照、失败现场和不自动重试的恢复日志。"""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.snapshots import (
    UpgradeSnapshots,
    atomic_write,
    canonical,
    contained,
    identifier,
)
from clytie.plugin_host.support import HostLog
from clytie.sdk import CancelToken

from .events import background
from .ledger import SCHEMA_VERSION, Ledger, LedgerError

PROTECTED = {".lock", ".migration.json"}


async def migrate_dialogue(db, cancel):
    """旧 JSON 入站记录补齐合并与忽略原因字段；由运行锁/快照事务包装。"""
    for key, raw in db.execute("SELECT id, data FROM inbound").fetchall():
        cancel.raise_if_cancelled()
        value = json.loads(raw)
        value.setdefault("merged_into", None)
        value.setdefault("reason", None)
        db.execute("UPDATE inbound SET data=? WHERE id=?", (json.dumps(value), key))


class LedgerSnapshots:
    def __init__(self, home, cid, clock, log):
        self.home, self.cid, self.clock, self.log = (
            Path(home).resolve(),
            identifier(cid),
            clock,
            log,
        )
        self.directory = contained(self.home, self.home / "companions" / cid / "ledger")
        self.base = contained(self.home, self.home / "backups" / "ledger" / cid)
        self.keys = UpgradeSnapshots(self.home, clock, log)

    def tree(self, root, *, protect=False):
        files, directories = {}, []
        for path in sorted(root.rglob("*")):
            contained(root, path)
            name = path.relative_to(root).as_posix()
            if protect and name in PROTECTED:
                continue
            if path.is_dir():
                directories.append(name)
            else:
                content = path.read_bytes()
                files[name] = {
                    "data": base64.b64encode(content).decode("ascii"),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
        return dict(files=files, directories=directories)

    def create(self, old, new, *, failed=False):
        key = self.keys._key(create=True)
        now = self.clock.now()
        name = ("failed-" if failed else "") + f"{old}-{new}-" + now.strftime("%Y%m%dT%H%M%S%fZ")
        directory = contained(self.home, self.base / name)
        directory.mkdir(parents=True, exist_ok=False)
        header = dict(
            format_version=1,
            kind="ledger",
            companion_id=self.cid,
            old=old,
            new=new,
            failed=failed,
            generated_at=now.isoformat(),
            fingerprint=hashlib.sha256(key).hexdigest(),
        )
        payload = self.tree(self.directory, protect=True)
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, canonical(payload), canonical(header))
        path = directory / "snapshot.enc"
        atomic_write(
            path,
            canonical(
                dict(
                    header=header,
                    nonce=base64.b64encode(nonce).decode("ascii"),
                    ciphertext=base64.b64encode(encrypted).decode("ascii"),
                )
            ),
        )
        restored = self.read(path)
        temporary = contained(self.home, self.base / (".verify-" + uuid4().hex))
        temporary.mkdir()
        self.write_tree(temporary, restored)
        if self.tree(temporary) != payload:
            raise LedgerError("snapshot_invalid", "snapshot verification failed")
        self.remove_tree(temporary)
        return path

    def read(self, path):
        key = self.keys._key()
        value = json.loads(contained(self.home, path).read_bytes())
        header = value["header"]
        if header["fingerprint"] != hashlib.sha256(key).hexdigest():
            raise LedgerError("snapshot_key_mismatch", "wrong backup key fingerprint")
        if (
            header.get("kind") != "ledger"
            or header.get("format_version") != 1
            or header.get("companion_id") != self.cid
        ):
            raise LedgerError("snapshot_invalid", "snapshot format or companion mismatch")
        payload = json.loads(
            AESGCM(key).decrypt(
                base64.b64decode(value["nonce"], validate=True),
                base64.b64decode(value["ciphertext"], validate=True),
                canonical(header),
            )
        )
        for name in [*payload["files"], *payload["directories"]]:
            part = PurePosixPath(name)
            if (
                part.is_absolute()
                or ".." in part.parts
                or "\\" in name
                or ":" in name
                or name in PROTECTED
            ):
                raise LedgerError("snapshot_invalid", "invalid snapshot path")
        for entry in payload["files"].values():
            if (
                hashlib.sha256(base64.b64decode(entry["data"], validate=True)).hexdigest()
                != entry["sha256"]
            ):
                raise LedgerError("snapshot_invalid", "snapshot hash mismatch")
        return payload

    def write_tree(self, root, payload):
        for name in payload["directories"]:
            contained(root, root / name).mkdir(parents=True, exist_ok=True)
        for name, entry in payload["files"].items():
            atomic_write(
                contained(root, root / name), base64.b64decode(entry["data"], validate=True)
            )

    def remove_tree(self, directory):
        """仅逐个移除明确目录内已经验证的文件；拒绝任何链接或越界路径。"""
        directory = contained(self.base, directory)
        paths = list(directory.rglob("*"))
        for path in paths:
            contained(directory, path)
        for path in paths:
            if path.is_file():
                path.unlink()
                self.log.info(
                    "ledger.snapshot_file_removed", path=path.relative_to(self.home).as_posix()
                )
        for path in sorted(
            (p for p in paths if p.is_dir()), key=lambda p: len(p.parts), reverse=True
        ):
            path.rmdir()
        directory.rmdir()

    def restore(self, path):
        payload = self.read(path)
        before = self.tree(self.directory, protect=True)
        self.write_tree(self.directory, payload)
        for name in before["files"]:
            if name not in payload["files"]:
                contained(self.directory, self.directory / name).unlink()
                self.log.info("ledger.migration_file_removed", path=name)
        for name in sorted(
            before["directories"], key=lambda x: len(PurePosixPath(x).parts), reverse=True
        ):
            if name not in payload["directories"]:
                contained(self.directory, self.directory / name).rmdir()
        if self.tree(self.directory, protect=True) != payload:
            raise LedgerError("restore_invalid", "restored ledger differs from snapshot")

    def cleanup(self, retention_days=None, *, migrated=False):
        if (self.directory / ".migration.json").exists():
            return
        snapshots = []
        if not self.base.exists():
            return
        for path in sorted(self.base.glob("*/snapshot.enc")):
            try:
                contained(self.base, path)
                envelope = json.loads(path.read_bytes())
                header = envelope["header"]
                # 不可信快照不清理；不妨碍健康账本继续运行。
                self.read(path)
            except Exception as exc:
                self.log.warning(
                    "ledger.snapshot_cleanup_skipped",
                    path=path.relative_to(self.home).as_posix(),
                    error=type(exc).__name__,
                )
                continue
            snapshots.append(
                (datetime.fromisoformat(header["generated_at"]), bool(header["failed"]), path)
            )
        normal = sorted((s for s in snapshots if not s[1]), key=lambda s: (s[0], s[2].as_posix()))
        excess = {p for _, _, p in normal[:-2]} if migrated else set()
        for created, _, path in snapshots:
            expired = retention_days is not None and self.clock.now() - created > timedelta(
                days=retention_days
            )
            if expired or path in excess:
                self.remove_tree(path.parent)


class LedgerRuntime:
    def __init__(self, home, cid, clock, *, migrations=None, retention_days=None, log=None):
        self.home, self.cid, self.clock = Path(home).resolve(), identifier(cid), clock
        self.log, self.migrations, self.retention_days = (
            log or HostLog(),
            {2: migrate_dialogue, **(migrations or {})},
            retention_days,
        )
        self.snapshots = LedgerSnapshots(self.home, cid, clock, self.log)
        self.directory = self.snapshots.directory
        self.path, self.journal = (
            self.directory / "ledger.sqlite3",
            self.directory / ".migration.json",
        )
        self.guard, self.ledger = None, None
        self.maintenance = None

    async def _maintain(self):
        while True:
            await self.clock.sleep(60)
            if self.journal.exists():
                continue
            try:
                self.snapshots.cleanup(self.retention_days)
                self.ledger.expire_content(self.retention_days)
            except Exception as exc:
                self.log.warning("ledger.retention_failed", error=type(exc).__name__)

    def version(self):
        # Called under the runtime lock: SQLite must recover hot journals first.
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        try:
            return db.execute("PRAGMA user_version").fetchone()[0]
        finally:
            db.close()

    def save(self, record):
        atomic_write(self.journal, canonical(record))

    def restore(self, record):
        record["phase"] = "restoring"
        self.save(record)
        failed = self.snapshots.create(record["from_version"], record["to_version"], failed=True)
        record["failed_snapshot"] = failed.relative_to(self.home).as_posix()
        self.save(record)
        self.snapshots.restore(self.home / record["snapshot"])
        record["phase"] = "failed"
        self.save(record)

    async def open(self, cancel=None):
        cancel = cancel or CancelToken()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.guard = file_lock(
            self.directory / ".lock", {**process_identity(), "operation": "ledger"}, self.log
        )
        self.guard.__enter__()
        try:
            if self.journal.exists():
                record = json.loads(self.journal.read_bytes())
                if record["phase"] == "migrating":
                    try:
                        current = self.version()
                    except (OSError, sqlite3.Error):
                        current = None
                    if current == record["to_version"]:
                        self.journal.unlink()
                        with sqlite3.connect(self.path) as db:
                            db.execute(
                                "INSERT INTO audit(data) VALUES (?)",
                                (
                                    json.dumps(
                                        {
                                            "kind": "migration_committed_journal_removed",
                                            "trace_id": None,
                                            "at": self.clock.now().isoformat(),
                                        }
                                    ),
                                ),
                            )
                    elif current == record["from_version"]:
                        try:
                            self.restore(record)
                        except Exception as exc:
                            record["error"] = type(exc).__name__
                            self.save(record)
                            raise LedgerError(
                                "ledger_unavailable", "migration recovery incomplete"
                            ) from exc
                    else:
                        record["error"] = "schema version cannot be reconciled"
                        self.save(record)
                if self.journal.exists():
                    raise LedgerError(
                        "ledger_unavailable", "migration journal requires user action"
                    )
            migrated = False
            if self.path.exists() and self.version() < SCHEMA_VERSION:
                old = self.version()
                if any(v not in self.migrations for v in range(old + 1, SCHEMA_VERSION + 1)):
                    raise LedgerError("ledger_unavailable", "missing ledger migration")
                snapshot = self.snapshots.create(old, SCHEMA_VERSION)
                record = dict(
                    from_version=old,
                    to_version=SCHEMA_VERSION,
                    snapshot=snapshot.relative_to(self.home).as_posix(),
                    failed_snapshot=None,
                    phase="migrating",
                    executor=process_identity(),
                    error=None,
                )
                self.save(record)
                try:
                    db = sqlite3.connect(self.path)
                    try:
                        for version in range(old + 1, SCHEMA_VERSION + 1):
                            cancel.raise_if_cancelled()
                            with db:
                                db.execute("BEGIN IMMEDIATE")
                                await self.migrations[version](db, cancel)
                                cancel.raise_if_cancelled()
                                if version == SCHEMA_VERSION:
                                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    finally:
                        db.close()
                except (Exception, asyncio.CancelledError) as exc:
                    record["error"] = type(exc).__name__
                    try:
                        self.restore(record)
                    except Exception as failure:
                        record["error"] = type(failure).__name__
                        self.save(record)
                        raise LedgerError(
                            "ledger_unavailable", "migration recovery incomplete"
                        ) from failure
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise LedgerError(
                        "ledger_unavailable", "migration failed and restored"
                    ) from exc
                self.journal.unlink()
                migrated = True
            if self.ledger is None:
                self.ledger = Ledger(self.path, self.cid, self.clock)
            self.snapshots.cleanup(self.retention_days, migrated=migrated)
            self.ledger.expire_content(self.retention_days)
            if self.retention_days is not None:
                self.maintenance = background(self._maintain())
            return self.ledger
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.maintenance:
            self.maintenance.cancel()
            self.maintenance = None
        if self.ledger:
            self.ledger.close()
            self.ledger = None
        if self.guard:
            self.guard.__exit__(None, None, None)
            self.guard = None


def migration_status(home):
    home = Path(home)
    alerts = []
    for path in sorted((home / "companions").glob("*/ledger/.migration.json")):
        cid = path.parents[1].name
        try:
            contained(home, path)
            record = json.loads(path.read_bytes())
            detail = {
                key: record.get(key) for key in ("phase", "error", "snapshot", "failed_snapshot")
            }
        except (OSError, ValueError):
            detail = dict(phase="unreadable", error="invalid migration journal")
        alerts.append(
            dict(
                companion_id=cid,
                level="error",
                **detail,
                action="修好代码或手动用快照恢复账本后，由用户删除此伴侣 ledger/.migration.json，再启动伴侣",
            )
        )
    return alerts


def snapshot_notices(home):
    home = Path(home)
    notices = []
    for path in sorted((home / "backups" / "ledger").glob("*/*/snapshot.enc")):
        try:
            contained(home, path)
        except ValueError:
            continue
        notices.append(
            dict(
                companion_id=path.parents[1].name,
                path=path.relative_to(home).as_posix(),
                level="warning",
                message="未设置内容保留期时，失败现场需用户逐个处理明确文件手动清理，不批量删除。快照和失败现场按生成时间到期，较旧内容可能比原始内容保存更久；迁移日志阻止启动期间不清理。",
            )
        )
    return notices
