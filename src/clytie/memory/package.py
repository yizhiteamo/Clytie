"""一致快照、认证导出与固定文件表；临时明文仅在实例内。"""

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from importlib import metadata
from pathlib import Path
from uuid import uuid4

from packaging.version import Version

from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.snapshots import atomic_write, contained, identifier
from clytie.sdk.types import MemoryRecord

from .crypto import PackageError, b64, decrypt, encrypt, new_recovery, public_key, un64
from .origins import FIELDS as ORIGIN_FIELDS
from .origins import TABLES as ORIGIN_TABLES
from .store import canonical, content_hash, fingerprint

# 每个表重新建表并复制值，不复制源库中的触发器、视图或其他表。
TABLES = {
    "metadata": "key TEXT PRIMARY KEY, value TEXT NOT NULL",
    "records": "id TEXT PRIMARY KEY, data TEXT NOT NULL",
    "revisions": "record_id TEXT, revision INTEGER, data TEXT NOT NULL, hash TEXT NOT NULL, "
    "parent TEXT, at TEXT NOT NULL, operator TEXT NOT NULL, trace TEXT, import_id TEXT, "
    "reason TEXT, PRIMARY KEY(record_id,revision)",
    "audit": "id INTEGER PRIMARY KEY, data TEXT NOT NULL",
    "imports": "id TEXT PRIMARY KEY, data TEXT NOT NULL",
    "foreign_revisions": "package TEXT, record_id TEXT, revision INTEGER, data TEXT, "
    "PRIMARY KEY(package,record_id,revision)",
}


FADE_COLUMNS = (
    "record_id TEXT PRIMARY KEY, last_recalled_at TEXT NOT NULL, half_life_days REAL NOT NULL"
)


def export_tables(version):
    return {**TABLES, **({"fade": FADE_COLUMNS, **ORIGIN_TABLES} if version == 3 else {})}


@contextmanager
def temporary_database(home, companion_id, kind):
    home = Path(home).resolve()
    directory = contained(home, home / "run" / "memory-temp")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=directory, prefix=f"{identifier(companion_id)}-{kind}-", suffix=".db"
    )
    os.close(descriptor)
    path = Path(name)
    try:
        yield path
    finally:
        # 每次只删除这个临时数据库的一个明确文件；失败保留并报告。
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = contained(home, Path(str(path) + suffix))
            if candidate.exists():
                candidate.unlink()


def snapshot(store, home, companion_id):
    with temporary_database(home, companion_id, "export") as path:
        return _snapshot(store, path)


def _snapshot(store, path):
    source = sqlite3.connect(":memory:")
    output = sqlite3.connect(path)
    try:
        output.execute("PRAGMA journal_mode=DELETE")
        store.db.backup(source)
        for table, columns in export_tables(3).items():
            output.execute(f"CREATE TABLE {table}({columns})")
            rows = (
                source.execute(f"SELECT * FROM {table}").fetchall()
                if store.has_table(table)
                else []
            )
            if rows:
                placeholders = ",".join("?" for _ in rows[0])
                output.executemany(f"INSERT INTO {table} VALUES({placeholders})", rows)
        from .fade import eligible, state

        for record in store.all_records():
            if eligible(store, record):
                at, half = state(store, record)
                output.execute(
                    "INSERT INTO fade SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM fade WHERE record_id=?)",
                    (record.record_id, at.isoformat(), half, record.record_id),
                )
        output.commit()
        return path.read_bytes()
    finally:
        source.close()
        output.close()


def open_database(data, home, companion_id, version=3):
    with temporary_database(home, companion_id, "import") as path:
        path.write_bytes(data)
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            _validate_database(connection, version)
            result = sqlite3.connect(":memory:")
            try:
                connection.backup(result)
                result.execute("PRAGMA trusted_schema=OFF")
                result.execute("PRAGMA query_only=ON")
                result.row_factory = sqlite3.Row
            except BaseException:
                result.close()
                raise
        finally:
            connection.close()
    return result


def _validate_database(connection, version):
    tables = export_tables(version)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        schema = connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if sorted(schema) != sorted((name, "table") for name in tables):
            raise PackageError("记忆包包含非预期的数据库对象")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise PackageError("记忆数据库校验失败")
        connection.row_factory = sqlite3.Row
        # 在接触数据前逐表核对列，绝不执行包中的 SQL。
        expected = sqlite3.connect(":memory:")
        try:
            for table, columns in tables.items():
                expected.execute(f"CREATE TABLE {table}({columns})")
                names = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                if names != [row[1] for row in expected.execute(f"PRAGMA table_info({table})")]:
                    raise PackageError("记忆数据库字段不匹配")
        finally:
            expected.close()
        records = {}
        for row in connection.execute("SELECT * FROM records"):
            record = MemoryRecord.model_validate_json(row["data"])
            if record.record_id != row["id"]:
                raise PackageError("记忆编号不匹配")
            records[record.record_id] = record
        history = {}
        for row in connection.execute("SELECT * FROM revisions ORDER BY record_id,revision"):
            record = MemoryRecord.model_validate_json(row["data"])
            chain = history.setdefault(record.record_id, [])
            if (
                record.record_id != row["record_id"]
                or record.revision != row["revision"]
                or content_hash(record) != row["hash"]
                or row["parent"] != (chain[-1]["hash"] if chain else None)
                or record.revision != len(chain) + 1
            ):
                raise PackageError("记忆修订链校验失败")
            chain.append(dict(row))
        if set(records) != set(history) or any(
            records[key].model_dump_json() != history[key][-1]["data"] for key in records
        ):
            raise PackageError("记忆当前版本与修订链不一致")
        if version == 3:
            for row in connection.execute("SELECT * FROM fade"):
                at = datetime.fromisoformat(row["last_recalled_at"])
                half = row["half_life_days"]
                if (
                    row["record_id"] not in records
                    or at.tzinfo is None
                    or not isinstance(half, (float, int))
                    or not math.isfinite(half)
                    or half <= 0
                ):
                    raise PackageError("淡化状态无效")
        if version == 3:
            registry = {}
            for row in connection.execute("SELECT * FROM conversation_registry"):
                entry = {k: row[k] for k in ORIGIN_FIELDS}
                if (
                    any(not isinstance(v, str) or not v or len(v) > 512 for v in entry.values())
                    or entry["kind"] not in {"private", "group"}
                    or entry["category"] not in {"owner_private", "internal", "group"}
                    or fingerprint(entry) != row["key"]
                ):
                    raise PackageError("会话登记无效")
                registry[row["key"]] = entry
            referenced = set()
            for row in connection.execute("SELECT * FROM revision_origins"):
                chain = history.get(row["record_id"], [])
                revision = row["revision"]
                entry = registry.get(row["registry_key"])
                if type(revision) is not int or not 1 <= revision <= len(chain) or not entry:
                    raise PackageError("会话登记引用无效")
                record = MemoryRecord.model_validate_json(chain[revision - 1]["data"])
                if entry["conversation_id"] != record.origin_conversation_id:
                    raise PackageError("会话登记与修订不匹配")
                referenced.add(row["registry_key"])
            if referenced != set(registry):
                raise PackageError("会话登记存在未引用条目")
        return connection
    except BaseException:
        connection.close()
        raise


def payload(store, companion_id, home):
    try:
        clytie_version = metadata.version("clytie")
        Version(clytie_version)
    except (metadata.PackageNotFoundError, ValueError) as exc:
        raise ValueError("无法读取有效的 Clytie 安装版本，拒绝导出") from exc
    data = snapshot(store, home, companion_id)
    db = open_database(data, home, companion_id)
    try:
        uid_row = db.execute("SELECT value FROM metadata WHERE key='companion_uid'").fetchone()
        if uid_row is None:
            raise ValueError("还没有记忆，不需要导出")
        uid = json.loads(uid_row[0])
        manifest = {
            "version": 3,
            "clytie_version": clytie_version,
            "companion_uid": uid,
            "companion_id": companion_id,
            "record_count": db.execute("SELECT count(*) FROM records").fetchone()[0],
            "exported_at": store.clock.now().isoformat(),
        }
        return canonical(
            {
                "manifest": manifest,
                "files": {
                    "memory.sqlite3": {
                        "data": b64(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                },
            }
        ).encode()
    finally:
        db.close()


def unpack(raw, home, companion_id):
    try:
        value = json.loads(raw)
        if set(value) != {"manifest", "files"} or set(value["files"]) != {"memory.sqlite3"}:
            raise PackageError("记忆包文件表不匹配")
        item, manifest = value["files"]["memory.sqlite3"], value["manifest"]
        format_version = manifest.get("version")
        if type(format_version) is not int or format_version not in {1, 2, 3}:
            raise PackageError("不支持的记忆包清单版本")
        fields = {
            "version",
            "companion_uid",
            "companion_id",
            "record_count",
            "exported_at",
        }
        if format_version >= 2:
            fields.add("clytie_version")
        if set(manifest) != fields:
            raise PackageError("记忆包清单字段不匹配")
        if format_version >= 2:
            if not isinstance(manifest["clytie_version"], str):
                raise PackageError("Clytie 版本格式无效")
            Version(manifest["clytie_version"])
        if not isinstance(manifest["companion_uid"], str) or not re.fullmatch(
            r"[0-9a-f]{32}", manifest["companion_uid"]
        ):
            raise PackageError("记忆包身份格式无效")
        identifier(manifest["companion_id"])
        data = un64(item["data"])
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise PackageError("记忆包文件校验失败")
        db = open_database(data, home, companion_id, format_version)
        try:
            uid = db.execute("SELECT value FROM metadata WHERE key='companion_uid'").fetchone()
            if (
                not uid
                or json.loads(uid[0]) != manifest["companion_uid"]
                or db.execute("SELECT count(*) FROM records").fetchone()[0]
                != manifest["record_count"]
            ):
                raise PackageError("记忆包清单校验失败")
        except BaseException:
            db.close()
            raise
        return manifest, db
    except PackageError:
        raise
    except Exception as exc:
        raise PackageError("记忆包格式无效") from exc


class Exports:
    def __init__(self, store, home, companion_id):
        self.store, self.home = store, Path(home).resolve()
        self.companion_id = identifier(companion_id)

    def recovery_path(self):
        return contained(self.home, self.home / "secrets" / "memory-recovery.pub")

    def rotate_recovery(self, *, confirmed=False):
        if not confirmed:
            raise ValueError("需要确认；请保留旧恢复码，旧包仍使用旧恢复码")
        with self.recovery_lock():
            return self._rotate_recovery()

    def recovery_lock(self):
        path = contained(self.home, self.home / "run" / "memory-recovery.lock")
        return file_lock(path, {**process_identity(), "operation": "memory_export"})

    def _rotate_recovery(self):
        code, public = new_recovery()
        path = self.recovery_path()
        previous = path.read_bytes() if path.exists() else None
        try:
            atomic_write(path, public)
            with self.store.transaction():
                self.store.audit(
                    "memory_recovery_rotated", fingerprint=hashlib.sha256(public).hexdigest()
                )
        except BaseException:
            if previous is None:
                if path.exists():
                    path.unlink()
            else:
                atomic_write(path, previous)
            raise
        return code

    def export(self, out, *, passphrase=None, format="encrypted"):
        if not self.store.uid:
            raise ValueError("还没有记忆，不需要导出")
        with self.recovery_lock():
            return self._export(out, passphrase=passphrase, format=format)

    def _export(self, out, *, passphrase, format):
        out = Path(out)
        if format == "jsonl":
            exports = self.home / "exports"
            contained(exports, out)
            data = "".join(
                canonical(r.model_dump(mode="json")) + "\n" for r in self.store.all_records()
            )
            atomic_write(out, data.encode())
            return {"path": str(out), "recovery_code": None}
        if format != "encrypted":
            raise ValueError("unsupported export format")
        if passphrase is None or len(passphrase) < 12:
            raise ValueError("passphrase must contain at least 12 characters")
        # 先建立并校验一致快照，失败不生成恢复凭据或目标文件。
        raw = payload(self.store, self.companion_id, self.home)
        path = self.recovery_path()
        code = None
        if path.exists():
            recipient = path.read_bytes()
            public_key(recipient)
        else:
            code, recipient = new_recovery()
        encrypted = encrypt(raw, passphrase, recipient)
        _, checked = unpack(decrypt(encrypted, passphrase=passphrase), self.home, self.companion_id)
        checked.close()
        pending = out.with_name(out.name + "." + uuid4().hex + ".pending")
        created_public = False
        try:
            atomic_write(pending, encrypted)
            _, checked = unpack(
                decrypt(pending.read_bytes(), passphrase=passphrase), self.home, self.companion_id
            )
            checked.close()
            if code is not None:
                atomic_write(path, recipient)
                created_public = True
            pending.replace(out)
            with self.store.transaction():
                self.store.audit("memory_export", records=len(self.store.all_records()))
        except BaseException:
            # 首次导出失败不能留下没有交给用户恢复码的公钥。
            if created_public:
                path.unlink()
            raise
        finally:
            if pending.exists():
                pending.unlink()
        return {"path": str(out), "recovery_code": code}
