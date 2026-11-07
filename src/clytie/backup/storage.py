"""主包持久索引及逐文件保留执行；未登记文件没有删除权。"""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.locking import file_lock
from clytie.plugin_host.snapshots import contained
from clytie.supervisor.common import read, write

from .restore import digest, identity, read_state
from .retention import BackupRecord, plan_retention


class BackupStorage:
    def __init__(self, home, clock):
        self.home, self.clock = Path(home).resolve(), clock
        self.root = contained(self.home, self.home / "backups/regular")
        self.path = contained(self.home, self.home / "supervisor/backup-index.json")
        from .repository import INDEX, SourceRepository

        legacy_path = self.path
        instance_id = self.instance_id()
        self.repository = SourceRepository.initialize(
            self.root, clock, owner_instance_id=instance_id, migration_complete=False
        )
        self.path = self.root / INDEX
        if not self.repository.marker.get("migration_complete"):
            self._migrate(legacy_path)

    def lock(self):
        return file_lock(self.path.with_suffix(".lock"), {"operation": "backup_index"})

    def state(self):
        return self.repository.index()

    def _migrate(self, legacy_path):
        """Copy protection before enabling deletion; retain legacy evidence."""
        import hashlib

        from clytie.plugin_host.snapshots import atomic_write, canonical

        from .previews import PreviewCatalog, _LocalCatalog, source_key
        from .repository import MARKER, SourceRepository, holder_identity

        with self.lock():
            if legacy_path.exists():
                legacy_index = read(legacy_path, {})
                if legacy_index.get("schema_version") != 1:
                    raise ValueError("reference_recovery_blocked")
                current = self.state()
                for key, row in legacy_index["records"].items():
                    if key in current["records"] and current["records"][key] != row:
                        raise ValueError("reference_recovery_blocked")
                    current["records"][key] = row
                write(self.path, current)
            holder = holder_identity(self.home)
            legacy = _LocalCatalog(self.home, self.clock)
            local = PreviewCatalog(self.home, self.clock)
            authority = self.repository.catalog(holder)
            with legacy._transaction() as old:
                bindings = {}
                for key, row in self.state()["records"].items():
                    binding = self.repository.resolve(key)
                    binding["holder_instance_id"] = holder
                    bindings[source_key(row["path"])] = binding
                with authority._transaction() as dest:
                    for owner, ref in old["references"].items():
                        if ref["source"] not in bindings:
                            if Path(ref["source"]).is_relative_to(self.root):
                                raise ValueError("reference_recovery_blocked")
                            continue
                        new_key = authority._refkey(owner)
                        copied = {
                            **ref,
                            "owner_id": new_key,
                            "holder_instance_id": holder,
                            "holder_operation_id": owner,
                            "source_repository_id": self.repository.repository_id,
                            "backup_id": bindings[ref["source"]]["backup_id"],
                            "source_identity": bindings[ref["source"]]["source_identity"],
                        }
                        prior = dest["references"].get(new_key)
                        if prior and prior != copied:
                            raise ValueError("reference_recovery_blocked")
                        dest["references"][new_key] = copied
                    for plan_id, plan in old["previews"].items():
                        if plan["source"] not in bindings:
                            continue
                        copied = {
                            **plan,
                            "holder_instance_id": holder,
                            "reference_id": authority._refkey(plan["reference_id"]),
                            "target": hashlib.sha256(canonical(plan["target"])).hexdigest(),
                            "fingerprint": hashlib.sha256(
                                canonical(plan["fingerprint"])
                            ).hexdigest(),
                        }
                        prior = dest["previews"].get(plan_id)
                        if prior and prior != copied:
                            raise ValueError("reference_recovery_blocked")
                        dest["previews"][plan_id] = copied
                with local._routes() as routes:
                    routes["sources"].update(bindings)
                    for owner, ref in old["references"].items():
                        if ref["source"] in bindings:
                            routes["owners"][owner] = bindings[ref["source"]]
                    for plan_id, plan in old["previews"].items():
                        if plan["source"] in bindings:
                            routes["plans"][plan_id] = {
                                "kind": plan["kind"],
                                "source_repository": bindings[plan["source"]],
                                "target": plan["target"],
                                "fingerprint": plan["fingerprint"],
                                "phase": "published",
                                "generation_id": None,
                            }
            marker = dict(self.repository.marker, migration_complete=True)
            atomic_write(self.root / MARKER, canonical(marker))
            self.repository = SourceRepository(self.root, self.clock)

    def instance_id(self):
        with self.lock():
            path = self.home / "supervisor/backup-instance.json"
            state = read(path, {})
            restored = read_state(self.home)
            expected = restored.get("target_instance_id") if restored else None
            if not state:
                state = {"instance_id": expected or uuid4().hex}
                write(path, state)
            if expected and state["instance_id"] != expected:
                raise ValueError("instance_identity_mismatch")
            return state["instance_id"]

    def list(self):
        return list(self.state()["records"].values())

    def get(self, backup_id):
        return self.state()["records"][backup_id]

    def valid(self, record):
        path = contained(self.root, Path(record["path"]))
        return (
            identity(path) == record["identity"]
            and path.stat().st_size == record["size"]
            and digest(path) == record["sha256"]
        )

    def verified(self, record):
        """Only unchanged owned evidence can supply a valid recovery point."""
        try:
            self.repository.resolve(record["backup_id"])
            return record.get("verified", True) and self.valid(record)
        except (OSError, ValueError, KeyError):
            return False

    def register(self, record):
        with self.lock():
            if not self.valid(record):
                raise ValueError("backup_evidence_mismatch")
            state = self.state()
            old = state["records"].get(record["backup_id"])
            if old is not None and old != record:
                raise ValueError("backup_identity_conflict")
            state["records"][record["backup_id"]] = record
            write(self.path, state)
            latest = {}
            for row in sorted(state["records"].values(), key=lambda r: r["completed_at"]):
                latest[row["scope"] + ":" + row["group"] + ":" + row["completeness"]] = row[
                    "backup_id"
                ]
            write(self.home / "backups/latest.json", latest)

    @staticmethod
    def record(row, *, referenced=False, verified=True):
        return BackupRecord(
            row["backup_id"],
            row["path"],
            row["scope"],
            row["group"],
            datetime.fromisoformat(row["completed_at"]),
            row["size"],
            completeness=row["completeness"],
            manual=row["manual"],
            referenced=referenced,
            verified=verified,
        )

    def status(self):
        rows = self.list()
        return {
            "count": len(rows),
            "bytes": sum(r["size"] for r in rows),
            "manual_bytes": sum(r["size"] for r in rows if r["manual"]),
            "partial_bytes": sum(r["size"] for r in rows if r["completeness"] == "partial"),
            "latest": read(self.home / "backups/latest.json", {}),
        }

    def retention_now(self):
        from zoneinfo import ZoneInfo

        import yaml

        path = self.home / "clytie.local.yaml"
        source = (yaml.safe_load(path.read_bytes()) or {}) if path.exists() else {}
        return self.clock.now().astimezone(ZoneInfo(source.get("timezone", "UTC")))

    def rotate(self, config, *, started_bytes, new_backup_id, revalidate=None):
        from .configuration import config_commit_lock
        from .previews import PreviewCatalog

        refs = PreviewCatalog(self.home, self.clock)
        with self.lock():
            state = self.state()
            records = [
                self.record(r, referenced=bool(refs.holders(r["path"])), verified=self.verified(r))
                for r in state["records"].values()
            ]
            plan = plan_retention(
                records,
                config,
                now=self.retention_now(),
                started_bytes=started_bytes,
                new_backup_id=new_backup_id,
            )
            for path in plan.delete:
                row = next(r for r in state["records"].values() if r["path"] == path)
                with config_commit_lock(self.home), refs.deletion(path) as permitted:
                    if not permitted:
                        continue
                    if revalidate is not None:
                        revalidate()
                    if not self.valid(row):
                        raise ValueError("backup_evidence_mismatch")
                    receipt = {
                        "backup_id": row["backup_id"],
                        "path": path,
                        "state": "deleting",
                        "at": self.clock.now().isoformat(),
                    }
                    state["deletions"].append(receipt)
                    write(self.path, state)
                    Path(path).unlink()
                    receipt["state"] = "deleted"
                    del state["records"][row["backup_id"]]
                    write(self.path, state)
            return plan.status
