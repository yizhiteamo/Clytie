"""Authenticated new-directory restore; no business executors or replay entrypoints."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
from contextlib import ExitStack, contextmanager
from importlib.metadata import version
from pathlib import Path
from uuid import getnode, uuid4

import yaml

from clytie.clock import SystemClock
from clytie.config import repository_root
from clytie.memory.store import content_hash, fingerprint
from clytie.plugin_host.locking import _os_lock, _owner, file_lock, process_identity
from clytie.plugin_host.snapshots import atomic_write, canonical, identifier
from clytie.sdk.types import MemoryRecord

from .container import _name, _safe, owned_marker, unpack
from .credentials import validate_restored
from .io_client import checkpoint, heavy_io, io_read, io_scope
from .native_sqlite import native_checks, validation_stage
from .previews import PreviewCatalog, PreviewError

MARKER = "supervisor/restore.json"


class RestoreError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


@heavy_io
def budget_digest(path, check=None):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := io_read(stream, 1024 * 1024):
            if check:
                check()
            result.update(block)
    return result.hexdigest()


def identity(path):
    _safe(Path(path))
    info = Path(path).stat()
    return dict(device=info.st_dev, inode=info.st_ino)


def machine_id():
    return hashlib.sha256(f"{platform.node()}:{getnode()}".encode()).hexdigest()


def source_registration(home):
    home = Path(home).resolve()
    return dict(machine_id=machine_id(), home=str(home), identity=identity(home))


def _target_lock(target):
    target = Path(target)
    _safe(target.parent)
    name = str(target.resolve())
    if os.name == "nt":
        name = name.casefold()
    return target.parent / (
        ".clytie-restore-" + hashlib.sha256(name.encode()).hexdigest() + ".lock"
    )


def _hold(target):
    return file_lock(_target_lock(target), {**process_identity(), "operation": "restore"})


def _write(home, state):
    atomic_write(Path(home) / MARKER, canonical(state))


def read_state(home):
    home = Path(home)
    _safe(home)
    marker = home / MARKER
    _safe(marker)
    if not marker.exists():
        return None
    try:
        value = json.loads(marker.read_bytes())
        if (
            value["target"] != str(home.resolve())
            or value["target_identity"] != identity(home)
            or value["schema_version"] != 1
            or not value["restore_id"]
        ):
            raise RestoreError("restore_target_mismatch")
        return value
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RestoreError):
            raise
        raise RestoreError("restore_recovery_blocked") from exc


def check_source(state):
    """Production check: source registration, OS locks and PID creation identities."""
    registration = state.get("source_registration")
    if not isinstance(registration, dict) or not registration.get("machine_id"):
        return {"status": "unavailable"}
    if registration["machine_id"] != machine_id():
        return {"status": "remote_unverifiable"}
    try:
        home = Path(registration["home"])
        if identity(home) != registration["identity"]:
            return {"status": "identity_mismatch"}
        locks = [home / "run/supervisor.lock"]
        for companion in state["companions"]:
            cid = identifier(companion["id"])
            locks += [home / "companions" / cid / name / ".lock" for name in ("ledger", "memory")]
        for path in locks:
            _safe(path)
            if not path.exists():
                continue
            fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                owner = _owner(fd)
                try:
                    _os_lock(fd, True)
                except OSError:
                    return {"status": "active"}
                else:
                    _os_lock(fd, False)
                if owner.get("pid") and not owner.get("released"):
                    try:
                        actual = process_identity(owner["pid"])
                    except ProcessLookupError:
                        actual = None
                    except OSError:
                        return {"status": "unavailable"}
                    if actual and actual["started_at"] == owner.get("started_at"):
                        return {"status": "active"}
            finally:
                os.close(fd)
        return {"status": "stopped"}
    except OSError:
        return {"status": "inaccessible"}
    except (ValueError, KeyError, TypeError):
        return {"status": "unavailable"}


def _source(state, source_check, *, confirming=False):
    status = (source_check or check_source)(state).get("status")
    allowed = {"stopped", "remote_unverifiable"}
    if confirming or state.get("source_stopped"):
        allowed.add("inaccessible")
    if status not in allowed:
        raise RestoreError(
            "restore_source_active" if status == "active" else "restore_source_unverified"
        )
    return status


def assert_start_allowed(home, *, maintenance=False, source_check=None):
    state = read_state(home)
    if state is None:
        return
    if state["data_state"] != "ready_for_switch":
        raise RestoreError("restore_recovery_blocked")
    if maintenance:
        return
    if not state.get("source_stopped"):
        raise RestoreError("restore_source_unconfirmed")
    _source(state, source_check)


def source_stopped(home, restore_id, *, source_check=None, clock=None):
    with _hold(home):
        state = read_state(home)
        if (
            not state
            or state["restore_id"] != restore_id
            or state["data_state"] != "ready_for_switch"
        ):
            raise RestoreError("restore_recovery_blocked")
        status = _source(state, source_check, confirming=True)
        if not state.get("source_stopped"):
            state.update(source_stopped=True, source_check=status)
            state.setdefault("audit", []).append(
                dict(
                    id=uuid4().hex,
                    kind="source_stopped",
                    at=(clock or SystemClock()).now().isoformat(),
                    evidence=status,
                )
            )
            _write(home, state)
        return state


def acknowledge(home, restore_id, *, runtime_check, clock=None):
    with _hold(home):
        state = read_state(home)
        if (
            not state
            or state["restore_id"] != restore_id
            or not state.get("source_stopped")
            or state["data_state"] != "ready_for_switch"
        ):
            raise RestoreError("restore_recovery_blocked")
        result = runtime_check()
        if (
            result.get("home") != str(Path(home).resolve())
            or result.get("mode") != "normal"
            or result.get("running") is not True
            or result.get("configuration_valid") is not True
            or result.get("restore_id") != restore_id
        ):
            raise RestoreError("restore_not_running")
        _source(state, None)
        from .old_instances import OldInstances

        # Registration is durable first. A crash before switched is written is
        # recovered by the same operation id, with no duplicate inventory row.
        OldInstances(home, clock or SystemClock()).register_switch({**state, "switched": True})
        if not state.get("switched"):
            state["switched"] = True
            state.setdefault("audit", []).append(
                dict(
                    id=uuid4().hex,
                    kind="switched",
                    at=(clock or SystemClock()).now().isoformat(),
                    evidence="normal_runtime_verified",
                )
            )
            _write(home, state)
        return state


def _member(root, name):
    _name(name, {})
    path = Path(root) / name
    _safe(path)
    if not path.is_file():
        raise RestoreError("restore_member_missing")
    return path


def _legacy_ledger_schema(db):
    """Read an affected SQLite ledger only after exact core DDL allowlisting.

    Some SQLite builds mark their built-in JSON functions as unsafe. The
    immutable/query-only connection cannot parse our expression indexes with
    trusted_schema=OFF on those builds. No schema SQL from the file is executed;
    before any data or integrity query, compare every object with core DDL.
    """
    from clytie.kernel.ledger import HEALTH_SQL, SCHEMA_SQL

    def authorize(action, first, second, database, source):
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and second in {
            "json_extract",
            "like",
            "coalesce",
            "max",
        }:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and first in {
            "trusted_schema",
            "query_only",
            "user_version",
            "integrity_check",
            "table_info",
        }:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    db.set_authorizer(authorize)
    db.execute("PRAGMA trusted_schema=ON")
    expected_db = sqlite3.connect(":memory:")
    try:
        for statement in (*SCHEMA_SQL, *HEALTH_SQL):
            expected_db.execute(statement)
        expected = {
            name: (kind, sql)
            for name, kind, sql in expected_db.execute("SELECT name,type,sql FROM sqlite_master")
        }
    finally:
        expected_db.close()
    actual = {
        name: (kind, sql)
        for name, kind, sql in db.execute("SELECT name,type,sql FROM sqlite_master")
    }
    optional = {"health_tasks", "health_sending"}
    if not (set(expected) - optional <= set(actual) <= set(expected)) or any(
        item != expected[name] for name, item in actual.items()
    ):
        raise RestoreError("restore_database_invalid")


@contextmanager
def _database(path, required):
    with validation_stage():
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            with native_checks(db):
                db.execute("PRAGMA trusted_schema=OFF")
                db.execute("PRAGMA query_only=ON")
                if required is LEDGER_TABLES and any(
                    row[0] == "json_extract" and not row[5] & (1 << 21)
                    for row in db.execute("PRAGMA function_list")
                ):
                    _legacy_ledger_schema(db)
                objects = db.execute(
                    "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                tables = {name for name, kind in objects if kind == "table"}
                if (
                    tables != set(required)
                    or any(kind not in {"table", "index"} for _, kind in objects)
                    or db.execute("PRAGMA user_version").fetchone()[0] != 2
                    or db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                ):
                    raise RestoreError("restore_database_invalid")
                for table, columns in required.items():
                    if [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')] != columns:
                        raise RestoreError("restore_database_invalid")
                yield db
        finally:
            db.close()


MEMORY_TABLES = {
    "metadata": ["key", "value"],
    "records": ["id", "data"],
    "revisions": [
        "record_id",
        "revision",
        "data",
        "hash",
        "parent",
        "at",
        "operator",
        "trace",
        "import_id",
        "reason",
    ],
    "audit": ["id", "data"],
    "keywords": ["token", "record_id"],
    "vectors": ["record_id", "revision", "embedder_id", "vector"],
    "imports": ["id", "data"],
    "foreign_revisions": ["package", "record_id", "revision", "data"],
    "recollect_progress": ["conversation_id", "data"],
    "fade": ["record_id", "last_recalled_at", "half_life_days"],
    "conversation_registry": [
        "key",
        "conversation_id",
        "channel",
        "kind",
        "platform_id",
        "category",
    ],
    "revision_origins": ["record_id", "revision", "registry_key"],
}
LEDGER_TABLES = {
    name: ["id", "data"]
    for name in ("inbound", "record", "invocation", "intent", "attempt", "dedupe", "audit")
}


def _asset_references(value, assets):
    if isinstance(value, dict):
        if "asset_id" in value and value["asset_id"] not in assets:
            raise RestoreError("restore_asset_missing")
        for child in value.values():
            _asset_references(child, assets)
    elif isinstance(value, list):
        for child in value:
            _asset_references(child, assets)


def _companion(root, entry, assets):
    from .asset_references import ledger_references

    resources = ExitStack()
    warnings = []
    try:
        memory = resources.enter_context(_database(_member(root, entry["memory"]), MEMORY_TABLES))
        ledger = resources.enter_context(_database(_member(root, entry["ledger"]), LEDGER_TABLES))
        uid = memory.execute("SELECT value FROM metadata WHERE key='companion_uid'").fetchone()
        if entry["uid"] is None:
            if (
                uid is not None
                or memory.execute("SELECT 1 FROM records LIMIT 1").fetchone()
                or memory.execute("SELECT 1 FROM revisions LIMIT 1").fetchone()
            ):
                raise RestoreError("restore_uid_mismatch")
        elif (
            not uid
            or json.loads(uid[0]) != entry["uid"]
            or not re.fullmatch("[0-9a-f]{32}", entry["uid"])
        ):
            raise RestoreError("restore_uid_mismatch")
        for key, raw in memory.execute("SELECT id,data FROM records"):
            checkpoint()
            record = MemoryRecord.model_validate_json(raw)
            if record.record_id != key:
                raise RestoreError("restore_memory_invalid")
            previous, number, last = None, 0, None
            for rev, data, hashed, parent in memory.execute(
                "SELECT revision,data,hash,parent FROM revisions WHERE record_id=? ORDER BY revision",
                (key,),
            ):
                revision = MemoryRecord.model_validate_json(data)
                number += 1
                if (
                    revision.record_id != key
                    or rev != number
                    or revision.revision != number
                    or content_hash(revision) != hashed
                    or parent != previous
                ):
                    raise RestoreError("restore_memory_invalid")
                previous, last = hashed, revision
                _asset_references(revision.model_dump(mode="json"), assets)
            if last != record:
                raise RestoreError("restore_memory_invalid")
        if memory.execute(
            "SELECT 1 FROM revisions WHERE record_id NOT IN (SELECT id FROM records) LIMIT 1"
        ).fetchone():
            raise RestoreError("restore_memory_invalid")
        if memory.execute(
            "SELECT 1 FROM revision_origins o LEFT JOIN revisions r ON o.record_id=r.record_id AND o.revision=r.revision LEFT JOIN conversation_registry c ON o.registry_key=c.key WHERE r.record_id IS NULL OR c.key IS NULL LIMIT 1"
        ).fetchone():
            raise RestoreError("restore_origin_invalid")
        for row in memory.execute(
            "SELECT key,conversation_id,channel,kind,platform_id,category FROM conversation_registry"
        ):
            registry = dict(
                zip(("conversation_id", "channel", "kind", "platform_id", "category"), row[1:])
            )
            if (
                fingerprint(registry) != row[0]
                or registry["kind"] not in {"private", "group"}
                or registry["category"] not in {"owner_private", "internal", "group"}
                or any(not isinstance(value, str) or not value for value in registry.values())
            ):
                raise RestoreError("restore_origin_invalid")
            references = memory.execute(
                "SELECT r.data FROM revision_origins o JOIN revisions r ON o.record_id=r.record_id AND o.revision=r.revision WHERE o.registry_key=?",
                (row[0],),
            ).fetchall()
            if not references or any(
                MemoryRecord.model_validate_json(ref[0]).origin_conversation_id
                != registry["conversation_id"]
                for ref in references
            ):
                raise RestoreError("restore_origin_invalid")
        for table in LEDGER_TABLES:
            for _, raw in ledger.execute(f'SELECT id,data FROM "{table}"'):
                value = json.loads(raw)
                if not isinstance(value, dict) or (
                    value.get("companion_id", entry["id"]) != entry["id"]
                ):
                    raise RestoreError("restore_ledger_invalid")
                if not ledger_references(table, value) <= set(assets):
                    raise RestoreError("restore_asset_missing")
                if (
                    table in {"invocation", "intent"}
                    and value.get("trace_id")
                    and not ledger.execute(
                        'SELECT 1 FROM "record" WHERE id=?', (value["trace_id"],)
                    ).fetchone()
                ):
                    raise RestoreError("restore_ledger_reference_invalid")
                if (
                    table == "attempt"
                    and not ledger.execute(
                        "SELECT 1 FROM intent WHERE id=?", (value.get("intent_id"),)
                    ).fetchone()
                ):
                    raise RestoreError("restore_ledger_reference_invalid")
        bounds = {
            table: ledger.execute(f'SELECT coalesce(max(rowid),0) FROM "{table}"').fetchone()[0]
            for table in ("inbound", "intent")
        }
        rows = memory.execute("SELECT conversation_id,data FROM recollect_progress").fetchall()
        if not any(cid == "*" for cid, _ in rows):
            warnings.append(
                dict(
                    companion_id=entry["id"], code="continuity_unverified", reason="baseline_absent"
                )
            )
        for cid, raw in rows:
            progress = json.loads(raw)
            if cid == "!state":
                if (
                    type(progress.get("enabled")) is not bool
                    or type(progress.get("skipped")) is not int
                    or progress["skipped"] < 0
                ):
                    raise RestoreError("restore_progress_invalid")
                continue
            for table, limit in bounds.items():
                number = progress.get(table)
                if type(number) is not int or not 0 <= number <= limit:
                    raise RestoreError("restore_progress_invalid")
        return warnings
    finally:
        resources.close()


@heavy_io
def validate_contents(root, envelope, *, repo_root=None, budgeted=False, check=None):
    """Read-only component/version/reference checks after container authentication."""
    try:
        manifest = envelope["manifest"]
        if (
            manifest["schema_version"] not in {1, 2}
            or manifest["scope"] != "instance"
            or manifest["completeness"] != "complete"
            or manifest["code_version"] != version("clytie")
            or not manifest["source_instance_id"]
            or manifest.get("missing")
            or manifest.get("restore_dependencies")
        ):
            raise RestoreError("restore_incompatible")
        members = {item["path"]: item for item in envelope["files"]}
        for name, item in members.items():
            if (
                name == MARKER
                or name.split("/")[0] in {"secrets", "run", "backups", ".git"}
                or name.endswith((".lock", "-wal", "-shm"))
            ):
                raise RestoreError("restore_forbidden_member")
            path = _member(root, name)
            hashed = budget_digest(path, check) if budgeted else digest(path)
            if path.stat().st_size != item["size"] or hashed != item["sha256"]:
                raise RestoreError("restore_member_changed")
        covered, membership = set(), {}
        for number, group in enumerate(manifest["consistency_groups"]):
            if not group["epoch"] or set(group["members"]) != set(group["sha256"]):
                raise RestoreError("restore_consistency_invalid")
            for name in group["members"]:
                if (
                    name in covered
                    or name not in members
                    or group["sha256"][name] != members[name]["sha256"]
                ):
                    raise RestoreError("restore_consistency_invalid")
                covered.add(name)
                membership[name] = number
        if covered != set(members):
            raise RestoreError("restore_consistency_invalid")
        from .assets import layout

        permissions, _ = layout(root, envelope)
        config = yaml.safe_load(_member(root, "clytie.local.yaml").read_text(encoding="utf-8"))
        companions = manifest["companions"]
        if len({c["id"] for c in companions}) != len(companions) or set(config["companions"]) != {
            c["id"] for c in companions
        }:
            raise RestoreError("restore_companion_mismatch")
        warnings = []
        for entry in companions:
            cid = identifier(entry["id"])
            if (
                entry["memory"] != f"companions/{cid}/memory/memory.sqlite3"
                or entry["ledger"] != f"companions/{cid}/ledger/ledger.sqlite3"
                or membership[entry["memory"]] != membership[entry["ledger"]]
            ):
                raise RestoreError("restore_consistency_invalid")
            warnings.extend(_companion(root, entry, permissions.get(cid, set())))
        root_path = Path(repo_root or repository_root())
        seen_plugins = set()
        for plugin in manifest["plugins"]:
            cid, pid = identifier(plugin["companion_id"]), identifier(plugin["id"])
            pair = (cid, pid)
            if pair in seen_plugins or cid not in {c["id"] for c in companions}:
                raise RestoreError("restore_plugin_incompatible")
            seen_plugins.add(pair)
            name = plugin["manifest_path"]
            if name.startswith("repository:"):
                name = name.removeprefix("repository:")
                if name != f"plugins/{pid}/plugin.json":
                    raise RestoreError("restore_plugin_incompatible")
                metadata = json.loads(_member(root_path, name).read_bytes())
            else:
                if name != f"plugins/{pid}/plugin.json":
                    raise RestoreError("restore_plugin_incompatible")
                metadata = json.loads(_member(root, name).read_bytes())
            if (
                metadata["version"] != plugin["version"]
                or metadata.get("data", {}).get("version", 1) != plugin["data_version"]
            ):
                raise RestoreError("restore_plugin_incompatible")
            if plugin["version_path"] != f"companions/{cid}/plugin-versions.json":
                raise RestoreError("restore_plugin_incompatible")
            versions = json.loads(_member(root, plugin["version_path"]).read_bytes())
            installed = versions[pid]
            if (
                installed["plugin_version"] != plugin["version"]
                or installed["data_version"] != plugin["data_version"]
            ):
                raise RestoreError("restore_plugin_incompatible")
            for path in plugin["state_paths"]:
                if (
                    not path.startswith(f"companions/{cid}/plugin-data/{pid}/")
                    or path not in members
                    or membership[path] != membership[plugin["version_path"]]
                ):
                    raise RestoreError("restore_plugin_incompatible")
        for companion in companions:
            cid = companion["id"]
            document_path = Path(root) / f"companions/{cid}/companion.yaml"
            if document_path.exists():
                document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
                expected = set(config.get("plugins", {})) | set(document.get("plugins", {}))
                if document.get("model", {}).get("plugin"):
                    expected.add(document["model"]["plugin"])
                expected.update(channel["plugin"] for channel in document.get("channels", []))
                version_path = Path(root) / f"companions/{cid}/plugin-versions.json"
                if version_path.exists():
                    expected.update(json.loads(version_path.read_bytes()))
                if any((cid, pid) not in seen_plugins for pid in expected):
                    raise RestoreError("restore_plugin_incompatible")
        declared_state = {path for plugin in manifest["plugins"] for path in plugin["state_paths"]}
        for name in members:
            if "/plugin-data/" in name and name not in declared_state:
                raise RestoreError("restore_plugin_incompatible")
            if "/management/operations/" in name and name.endswith(".json"):
                operation = json.loads(_member(root, name).read_bytes())
                if operation.get("state") not in {
                    "committed",
                    "cancelled",
                    "interrupted",
                    "failed",
                }:
                    raise RestoreError("restore_management_incomplete")
            if "/plugin-recovery/" in name or name.endswith(".migration.json"):
                raise RestoreError("restore_management_incomplete")
        credentials = validate_restored(
            root, manifest["credential_exclusions"], repo_root=repo_root
        )
        if not credentials["data_valid"]:
            raise RestoreError("restore_configuration_invalid")
        from .assets import validate_configuration

        validate_configuration(root, envelope, permissions, credentials, root_path)
        return dict(**credentials, continuity=warnings, old_point_warning=True)
    except RestoreError:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error, OSError) as exc:
        raise RestoreError("restore_incompatible") from exc


class RestoreManager:
    def __init__(
        self, management_home, clock, *, repo_root=None, managed_paths=(), reserve_bytes=1024**3
    ):
        self.home = Path(management_home).resolve()
        _safe(self.home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.directory = self.home / "supervisor/restore-plans"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock, self.repo_root = clock, repo_root
        self.previews = PreviewCatalog(self.home, clock)
        self.managed_paths = [self.home, *(Path(p).resolve() for p in managed_paths)]
        self.reserve_bytes = reserve_bytes

    def _path(self, plan_id):
        if not re.fullmatch("[0-9a-f]{32}", plan_id):
            raise RestoreError("restore_plan_invalid")
        return self.directory / (plan_id + ".json")

    def _load(self, plan_id):
        try:
            return json.loads(self._path(plan_id).read_bytes())
        except (OSError, ValueError):
            raise RestoreError("restore_plan_invalid") from None

    def _target_relation(self, target, source, envelope=None):
        _safe(target)
        target = target.resolve()
        forbidden_paths = [*self.managed_paths, source.parent.resolve()]
        registration = (envelope or {}).get("manifest", {}).get("source_registration")
        if isinstance(registration, dict) and registration.get("machine_id") == machine_id():
            recorded = registration.get("home")
            if not isinstance(recorded, str) or not Path(recorded).is_absolute():
                raise RestoreError("restore_incompatible")
            # The authenticated historical path is only a deny boundary. Do not
            # resolve, stat or discover an instance there: it grants no access.
            forbidden_paths.append(Path(os.path.normpath(recorded)))
        for forbidden in forbidden_paths:
            if target.is_relative_to(forbidden) or forbidden.is_relative_to(target):
                raise RestoreError("restore_target_invalid")
        return target

    def _empty(self, target, source, envelope=None):
        target = self._target_relation(target, source, envelope)
        if not target.parent.is_dir():
            raise RestoreError("restore_target_invalid")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise RestoreError("restore_target_not_empty")
        return identity(target) if target.exists() else None

    def _space(self, target, needed):
        free = shutil.disk_usage(target if target.exists() else target.parent).free
        if free < needed + self.reserve_bytes:
            raise RestoreError("space_insufficient")
        return free

    def _checkpoint(self, target, check):
        def checkpoint():
            if check is not None:
                check()
            self._space(target, 1024 * 1024)

        return checkpoint

    def plan(
        self,
        source,
        key_path,
        target,
        workspace,
        *,
        check=None,
        plan_id=None,
        source_repository=None,
        confirmed=False,
    ):
        if source_repository is not None:
            self.previews.bind_source(source, source_repository, confirmed=confirmed)
        if self.previews._source_binding(source) is None:
            raise RestoreError("source_repository_binding_required")
        plan_id = plan_id or uuid4().hex
        path = self._path(plan_id)
        with file_lock(
            path.with_suffix(".lock"), {**process_identity(), "operation": "restore_plan"}
        ):
            if path.exists():
                prior = self._load(plan_id)
                self._target_relation(
                    Path(prior["target"]), Path(prior["source"]), prior["envelope"]
                )
                for item in (source, key_path, target):
                    _safe(Path(item))
                if any(
                    prior[field] != str(Path(value).resolve())
                    for field, value in (
                        ("source", source),
                        ("key_path", key_path),
                        ("target", target),
                    )
                ):
                    raise RestoreError("restore_plan_changed")
                if self.previews.get("instance", plan_id)["state"] == "ready":

                    def validate():
                        if (
                            prior["source_sha256"] != digest(source)
                            or prior["key_fingerprint"]
                            != hashlib.sha256(Path(key_path).read_bytes()).hexdigest()
                        ):
                            raise RestoreError("restore_plan_changed")

                    with self.previews.reading("instance", plan_id, validate=validate):
                        pass
                return prior
            owner = "generation:" + plan_id
            with self.previews.protect(source, owner, kind="generation"):
                plan = self._plan(source, key_path, target, workspace, check=check, plan_id=plan_id)
                self.publish_preview(plan)
                return plan

    def publish_preview(self, plan):
        return self.previews.publish(
            "instance",
            plan["id"],
            plan["source"],
            "generation:" + plan["id"],
            target=plan["target"],
            fingerprint={
                "source_sha256": plan["source_sha256"],
                "key_fingerprint": plan["key_fingerprint"],
                "parent_identity": plan["parent_identity"],
                "target_identity": plan["target_identity"],
            },
        )

    def admit(self, plan_id, operation_id):
        plan = self._load(plan_id)
        self._target_relation(Path(plan["target"]), Path(plan["source"]), plan["envelope"])

        prior = self.previews.get("instance", plan_id)
        if prior["state"] == "consumed":
            return prior["operation_id"], False
        source = Path(plan["source"])

        def stamp():
            _safe(source)
            info = source.stat()
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        def validate():
            target, source = Path(plan["target"]), Path(plan["source"])
            if (
                self._empty(target, source, plan["envelope"]) != plan["target_identity"]
                or identity(target.parent) != plan["parent_identity"]
            ):
                raise RestoreError("restore_target_changed")
            _safe(Path(plan["key_path"]))
            if (
                source_digest != plan["source_sha256"]
                or before != after
                or stamp() != after
                or hashlib.sha256(Path(plan["key_path"]).read_bytes()).hexdigest()
                != plan["key_fingerprint"]
            ):
                raise RestoreError("restore_source_changed")

        # A durable reader protects the source while hashing outside the registry
        # lock. The short accept transaction rechecks state/expiry and identity,
        # then atomically replaces preview protection with execution protection.
        with self.previews.reading("instance", plan_id):
            before = stamp()
            source_digest = budget_digest(source)
            after = stamp()
            checkpoint()
            return self.previews.accept("instance", plan_id, operation_id, validate=validate)

    def _publish_plan(self, plan):
        atomic_write(self._path(plan["id"]), canonical(plan))

    def _plan(self, source, key_path, target, workspace, *, check, plan_id):
        source, key_path, target, workspace = map(Path, (source, key_path, target, workspace))
        for path in (source, key_path, target, workspace):
            _safe(path)
        source, key_path, target, workspace = (
            p.resolve() for p in (source, key_path, target, workspace)
        )
        target_identity = self._empty(target, source)
        if (
            workspace == target
            or workspace.is_relative_to(target)
            or target.is_relative_to(workspace)
        ):
            raise RestoreError("restore_target_invalid")
        key, before = key_path.read_bytes(), budget_digest(source, check)
        envelope = unpack(source, workspace, key, check=self._checkpoint(workspace, check))
        target_identity = self._empty(target, source, envelope)
        validation = validate_contents(
            workspace, envelope, repo_root=self.repo_root, budgeted=True, check=check
        )
        if before != budget_digest(source, check):
            raise RestoreError("restore_source_changed")
        from .assets import restore_size

        size = restore_size(envelope)
        free = self._space(target, size)
        from .old_instances import OldInstances

        old_instances = OldInstances(self.home, self.clock).status()
        plan = dict(
            schema_version=1,
            id=plan_id,
            source=str(source),
            source_repository=self.previews._source_binding(source),
            source_sha256=before,
            key_path=str(key_path),
            key_fingerprint=hashlib.sha256(key).hexdigest(),
            target=str(target),
            target_identity=target_identity,
            parent_identity=identity(target.parent),
            created_at=self.clock.now().isoformat(),
            envelope=envelope,
            validation=validation,
            old_instances=old_instances,
            space=dict(
                target_bytes=size,
                temporary_bytes=0,
                required_bytes=size + self.reserve_bytes,
                available_bytes=free,
            ),
        )
        self._publish_plan(plan)
        return plan

    def _initial(self, plan, target):
        manifest = plan["envelope"]["manifest"]
        return dict(
            schema_version=1,
            restore_id=plan["id"],
            operation_id=plan["id"],
            target=str(target),
            target_identity=identity(target),
            target_instance_id=uuid4().hex,
            source_instance_id=manifest["source_instance_id"],
            source_registration=manifest.get("source_registration"),
            companions=manifest["companions"],
            source_sha256=plan["source_sha256"],
            source_repository=plan.get("source_repository"),
            data_state="incomplete",
            source_stopped=False,
            switched=False,
            credential_exclusions=plan["validation"]["credential_exclusions"],
            configuration=plan["validation"]["configuration"],
            continuity=plan["validation"]["continuity"],
        )

    def apply(self, plan_id, *, check=None, operation_id=None):
        requested_operation = operation_id
        operation_id, fresh = self.admit(plan_id, operation_id or plan_id)
        plan = self._load(plan_id)
        target, source, key_path = (Path(plan[n]) for n in ("target", "source", "key_path"))
        if not fresh and requested_operation != operation_id:
            state = read_state(target) if target.exists() else None
            if (
                state
                and state["restore_id"] == plan_id
                and state["data_state"] == "ready_for_switch"
            ):
                return state
            if state:
                raise RestoreError("restore_recovery_blocked")
            raise PreviewError("plan_in_use", operation_id=operation_id)
        with _hold(target):
            state = read_state(target) if target.exists() else None
            if state:
                if state["restore_id"] == plan_id and state["data_state"] == "ready_for_switch":
                    return state
                raise RestoreError("restore_recovery_blocked")
            if (
                self._empty(target, source, plan["envelope"]) != plan["target_identity"]
                or identity(target.parent) != plan["parent_identity"]
            ):
                raise RestoreError("restore_target_changed")
            _safe(source)
            _safe(key_path)
            key = key_path.read_bytes()
            if (
                budget_digest(source, check) != plan["source_sha256"]
                or hashlib.sha256(key).hexdigest() != plan["key_fingerprint"]
            ):
                raise RestoreError("restore_source_changed")
            self._space(target, plan["space"]["target_bytes"])
            target.mkdir(exist_ok=True)
            state = self._initial(plan, target)
            state["operation_id"] = operation_id
            _write(target, state)
            envelope = unpack(
                source, target, key, check=check, owned_marker=owned_marker(target, target / MARKER)
            )
            if (
                envelope != plan["envelope"]
                or budget_digest(source, check) != plan["source_sha256"]
            ):
                raise RestoreError("restore_source_changed")
            state["data_state"] = "verifying"
            _write(target, state)
            return self._finish(plan, state)

    def _finish(self, plan, state):
        # Existing publishing/recovery boundary: necessary completion must not
        # acquire an artificial token, even when invoked by a budgeted worker.
        with validation_stage(), io_scope(None):
            return self._finish_committed(plan, state)

    def _finish_committed(self, plan, state):
        target = Path(plan["target"])
        try:
            from .assets import materialize

            asset_paths = materialize(target, plan["envelope"])
            validation = validate_contents(target, plan["envelope"], repo_root=self.repo_root)
            allowed = {item["path"] for item in plan["envelope"]["files"]} | {MARKER} | asset_paths
            for path in target.rglob("*"):
                _safe(path)
                if path.is_file() and path.relative_to(target).as_posix() not in allowed:
                    raise RestoreError("restore_unknown_member")
            for plugin in plan["envelope"]["manifest"]["plugins"]:
                directory = (
                    target
                    / "companions"
                    / identifier(plugin["companion_id"])
                    / "plugin-data"
                    / identifier(plugin["id"])
                )
                _safe(directory)
                directory.mkdir(parents=True, exist_ok=True)
            state.update(
                data_state="ready_for_switch",
                credential_exclusions=validation["credential_exclusions"],
                configuration=validation["configuration"],
                continuity=validation["continuity"],
                source_release="pending",
            )
            _write(target, state)
        except BaseException:
            state["data_state"] = "recovery_blocked"
            _write(target, state)
            raise
        # Data commit is durable before release. A metadata outage must never
        # turn that committed result into a failed restore or launch a replay.
        try:
            if self.previews.release(state["operation_id"]) is not False:
                state["source_release"] = "released"
                _write(target, state)
        except (OSError, ValueError):
            state["source_release"] = "pending"
        return state

    def recover(self, plan_id):
        plan = self._load(plan_id)
        target = Path(plan["target"])
        self._target_relation(target, Path(plan["source"]), plan["envelope"])
        with _hold(target):
            state = read_state(target)
            if not state or state["restore_id"] != plan_id:
                raise RestoreError("restore_recovery_blocked")
            if state["data_state"] == "ready_for_switch":
                if state.get("source_release") == "pending":
                    state["source_release"] = (
                        "pending"
                        if self.previews.release(state["operation_id"]) is False
                        else "released"
                    )
                    _write(target, state)
                return state
            if state["data_state"] == "verifying":
                return self._finish(plan, state)
            state["data_state"] = "recovery_blocked"
            _write(target, state)
            return state
