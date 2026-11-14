"""受认证的备份源投影；合并、修订及撤销继续由原 Transfers 完成。"""

import hashlib
import json
import sqlite3
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from clytie.api.management_journal import checkpoint
from clytie.memory.package import _validate_database, export_tables
from clytie.plugin_host.snapshots import contained, identifier

from .container import unpack
from .io_client import io_checks
from .native_sqlite import native_checks, validation_stage
from .previews import PreviewCatalog
from .restore import MEMORY_TABLES, _database, _member
from .temporary import TemporaryArtifacts


class BackupMemorySource:
    def __init__(self, home, clock, companion_id, binding):
        self.home, self.clock, self.cid = Path(home).resolve(), clock, identifier(companion_id)
        if set(binding) not in (
            {"backup_id", "key_fingerprint"},
            {"backup_id", "key_fingerprint", "key_path", "source_repository"},
        ):
            raise ValueError("backup_memory_source_invalid")
        self.binding = dict(binding)
        self.catalog = PreviewCatalog(self.home, clock)
        self.generation_id = None
        self.publishing = False

    def begin(self, source):
        self.publishing = False
        if "source_repository" in self.binding:
            self.catalog.bind_source(source, self.binding["source_repository"], confirmed=True)
        self.generation_id = "memory-generation:" + uuid4().hex
        self.catalog.reference(source, self.generation_id, kind="generation")

    def failed_generation(self):
        if self.generation_id is not None:
            self.catalog.release(self.generation_id, recovery_required=self.publishing)

    def publish(self, plan, path):
        from clytie.api.management_journal import current
        from clytie.plugin_host.snapshots import atomic_write, canonical

        plan["backup_generation_id"] = self.generation_id
        data = canonical(plan)
        self.publishing = True
        journal = current.get()
        if journal is not None:
            journal.value["backup_preview"] = {"plan_id": plan["id"]}
            journal.prepare_file(path, data, {"plan_id": plan["id"]})
        atomic_write(path, data)
        publish_preview(self.home, self.clock, plan)
        if journal is not None:
            journal.committed({"plan_id": plan["id"]})

    @contextmanager
    def open(self, source):
        key_path = (
            Path(self.binding["key_path"])
            if "key_path" in self.binding
            else contained(self.home, self.home / "secrets/backup.key")
        )
        with key_path.open("rb") as stream:
            key = stream.read(33)
        if len(key) != 32 or hashlib.sha256(key).hexdigest() != self.binding["key_fingerprint"]:
            raise ValueError("backup_key_changed")
        temporary_id = uuid4().hex
        temporaries = TemporaryArtifacts(self.home, temporary_id)
        temporaries.begin()
        root = temporaries.root / "memory-source"
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(mode=0o700)
        envelope = unpack(source, root, key, check=checkpoint)
        # Only exact authenticated file names are eligible for subsequent cleanup.
        for row in envelope["files"]:
            _member(root, row["path"])
        temporaries.register([root])
        projected = root.parent / (root.name + ".sqlite3")
        resources = ExitStack()
        db = original = None
        try:
            resources.enter_context(io_checks(checkpoint))
            resources.enter_context(validation_stage())
            manifest = envelope["manifest"]
            entries = manifest.get("companions", [])
            if (
                manifest.get("schema_version") != 1
                or manifest.get("scope") != "memory"
                or manifest.get("completeness") != "complete"
                or len(entries) != 1
                or not isinstance(manifest.get("code_version"), str)
            ):
                raise ValueError("backup_memory_source_invalid")
            entry = entries[0]
            # Asset import has no approved file transaction/undo protocol yet.
            # Authenticated but unsupported payloads must not be silently lost.
            if manifest.get("assets") or {row["path"] for row in envelope["files"]} != {
                entry["memory"]
            }:
                raise ValueError("backup_memory_assets_unsupported")
            original = resources.enter_context(
                _database(_member(root, entry["memory"]), MEMORY_TABLES)
            )
            uid = original.execute(
                "SELECT value FROM metadata WHERE key='companion_uid'"
            ).fetchone()
            if not uid or not entry["uid"] or json.loads(uid[0]) != entry["uid"]:
                raise ValueError("backup_memory_source_invalid")
            db = sqlite3.connect(projected)
            resources.callback(db.close)
            resources.enter_context(native_checks(db))
            for table, columns in export_tables(3).items():
                checkpoint()
                db.execute(f"CREATE TABLE {table}({columns})")
                cursor = original.execute(f'SELECT * FROM "{table}"')
                while rows := cursor.fetchmany(128):
                    checkpoint()
                    if table == "metadata":
                        rows = [row for row in rows if row[0] != "missing_origins"]
                    if rows:
                        marks = ",".join("?" for _ in rows[0])
                        db.executemany(f'INSERT INTO "{table}" VALUES({marks})', rows)
            db.commit()
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            # This scope owns handler removal and actual connection close. The
            # package reader's default error-close would hide the original error.
            _validate_database(db, 3, close_on_error=False)
            db.row_factory = sqlite3.Row
            native = {
                "version": 3,
                "clytie_version": manifest["code_version"],
                "companion_uid": entry["uid"],
                "companion_id": entry["id"],
                "record_count": db.execute("SELECT count(*) FROM records").fetchone()[0],
            }
            yield native, db
        finally:
            try:
                resources.close()
            finally:
                if projected.exists():
                    temporaries.register([projected])
                temporaries.cleanup()


def publish_preview(home, clock, plan):
    if plan.get("source_type") != "backup" or plan.get("kind") != "import":
        raise ValueError("backup_memory_source_invalid")
    return PreviewCatalog(home, clock).publish(
        "memory",
        plan["id"],
        plan["source"],
        plan["backup_generation_id"],
        target=plan["companion_id"],
        fingerprint={
            key: plan[key] for key in ("source_hash", "database", "configuration", "backup_binding")
        },
    )
