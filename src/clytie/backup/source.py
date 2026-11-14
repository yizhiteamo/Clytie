"""主包源组装：只捕获明确材料，在线失败不猜测离线，不执行恢复。"""

import asyncio
import hashlib
import json
import sqlite3
import threading
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import yaml

from clytie.api.management_journal import gate
from clytie.config import repository_root
from clytie.memory.transfer import runtime_lock
from clytie.plugin_host.locking import Operations, file_lock
from clytie.plugin_host.manifests import discover
from clytie.plugin_host.snapshots import atomic_read, contained, identifier
from clytie.plugin_host.support import HostLog

from .configuration import config_commit_lock
from .container import _name
from .coordination import companion_coordinator
from .credentials import credential_exclusions, redact_document
from .io_client import heavy_io, io_checks, io_read, io_write
from .mirror import publish_no_replace
from .native_sqlite import native_checks, validation_stage
from .restore import source_registration
from .view_limits import ViewLimits


class SourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _small(path, limit=1024 * 1024):
    if path.stat().st_size > limit:
        raise SourceError("snapshot_metadata_too_large")
    value = atomic_read(path)
    if len(value) > limit:
        raise SourceError("snapshot_metadata_too_large")
    return value


def _hash(value):
    return hashlib.sha256(value).hexdigest()


@heavy_io
def _entry(root, name, check=None):
    path = contained(root, root / name)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := io_read(stream, 1024 * 1024):
            if check:
                check()
            digest.update(chunk)
    return {"path": name, "size": path.stat().st_size, "sha256": digest.hexdigest()}


def _put(root, name, value):
    path = contained(root, root / name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)
    return _entry(root, name)


@heavy_io
def _copy(home, source, root, name, check):
    source = contained(home, source)
    target = contained(root, root / name)
    target.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while chunk := io_read(incoming, 1024 * 1024):
            check()
            io_write(outgoing, chunk)
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SourceError("snapshot_inconsistent")
    return _entry(root, name)


def _group(epoch, entries):
    return {
        "epoch": epoch,
        "members": [e["path"] for e in entries],
        "sha256": {e["path"]: e["sha256"] for e in entries},
    }


def _accept(captured, work, staging, result, documents, scope, cid):
    seen, keep = {}, []
    declared_assets = {asset["path"] for asset in captured["assets"]}
    for entry in captured["files"]:
        name = entry["path"]
        _name(name, seen)
        if (
            name.split("/")[0] not in {"companions", "plugins", "shared"}
            or name.startswith("shared/")
            and name not in declared_assets
            or name.endswith((".lock", "-wal", "-shm"))
            or name in documents
            or scope == "memory"
            and name != f"companions/{cid}/memory/memory.sqlite3"
            and not any(a["path"] == name for a in captured["assets"])
        ):
            raise SourceError("snapshot_inconsistent")
        if _entry(work, name) != entry:
            raise SourceError("snapshot_inconsistent")
        destination = contained(staging, staging / name)
        if destination.exists():
            if _entry(staging, name) != entry:
                raise SourceError("snapshot_inconsistent")
        else:
            keep.append(entry)
    kept = {entry["path"] for entry in keep}
    prepared_groups = []
    covered = set()
    for group in captured["consistency_groups"]:
        if set(group["members"]) != set(group["sha256"]) or not group["epoch"]:
            raise SourceError("snapshot_inconsistent")
        for name in group["members"]:
            entry = next((e for e in captured["files"] if e["path"] == name), None)
            if name in covered or entry is None or entry["sha256"] != group["sha256"][name]:
                raise SourceError("snapshot_inconsistent")
            covered.add(name)
        names = [name for name in group["members"] if name in kept]
        if names:
            prepared_groups.append(
                {
                    **group,
                    "members": names,
                    "sha256": {name: group["sha256"][name] for name in names},
                }
            )
    if covered != {entry["path"] for entry in captured["files"]}:
        raise SourceError("snapshot_inconsistent")
    for entry in keep:
        destination = staging / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        publish_no_replace(work / entry["path"], destination)
    result["files"].extend(keep)
    result["consistency_groups"].extend(prepared_groups)
    for key in ("companions", "plugins", "assets", "missing"):
        result[key].extend(captured[key])


def _configuration(home, root):
    with config_commit_lock(home):
        raw = _small(contained(home, home / "clytie.local.yaml"))
        instance = yaml.safe_load(raw)
        documents = {"clytie.local.yaml": raw}
        for cid in instance["companions"]:
            name = f"companions/{identifier(cid)}/companion.yaml"
            documents[name] = _small(contained(home, home / name))
        exclusions = credential_exclusions(home, repo_root=root)
    return instance, documents, exclusions


def _offline(home, cid, staging, clock, epoch, scope, documents, manifests, check, repo_root=None):
    from clytie.assets import AssetStore

    from .asset_references import ledger_references
    from .assets import boundary, capture, configuration

    asset_store = AssetStore(home, clock)
    assets = []
    dependencies = (
        configuration(home, cid, repo_root, manifests, documents=documents)
        if scope == "instance"
        else []
    )
    base = home / "companions" / cid
    missing, entries, groups, plugins = [], [], [], []
    config = yaml.safe_load(documents[f"companions/{cid}/companion.yaml"])
    instance = yaml.safe_load(documents["clytie.local.yaml"])
    expected = set(config.get("plugins", {})) | set(instance.get("plugins", {}))
    if config.get("model", {}).get("plugin"):
        expected.add(config["model"]["plugin"])
    expected.update(c["plugin"] for c in config.get("channels", []))
    version_path = contained(home, base / "plugin-versions.json")
    operations = Operations(home, HostLog(), clock)
    sources = {"memory": base / "memory/memory.sqlite3"}
    if scope == "instance":
        sources["ledger"] = base / "ledger/ledger.sqlite3"
    with ExitStack() as locks:
        # 原生命周期锁保持至离线物化完成；不取得业务执行权、不启动恢复。
        locks.enter_context(file_lock(base / "ledger/.lock", {"operation": "backup_offline"}))
        locks.enter_context(runtime_lock(home, cid, "backup_offline"))
        locks.enter_context(gate(home, cid))
        if (base / "ledger/.migration.json").exists() or (
            base / "management/blocked.json"
        ).exists():
            raise SourceError("snapshot_unavailable")
        management = base / "management/operations"
        if management.exists():
            for path in management.glob("*.json"):
                if json.loads(_small(contained(home, path))).get("state") not in {
                    "committed",
                    "cancelled",
                    "interrupted",
                    "failed",
                }:
                    raise SourceError("snapshot_unavailable")
        versions_raw = _small(version_path) if version_path.exists() else b"{}"
        versions = json.loads(versions_raw)
        expected.update(versions)
        locks.enter_context(operations.hold(cid, sorted(expected), "backup_offline"))
        locks.enter_context(
            file_lock(base / "plugin-versions.lock", {"operation": "backup_capture"})
        )
        if any(
            p.suffix == ".json" and not p.name.endswith(".audit.jsonl")
            for p in (base / "plugin-recovery").glob("*")
            if p.is_file()
        ):
            raise SourceError("snapshot_unavailable")
        readers = {}
        boundary_raw = None
        outer_check = check
        limits = None

        def check():
            outer_check()
            if limits is not None:
                limits.check()

        try:
            with companion_coordinator(home, cid, clock).commit():
                pinned_at = clock.monotonic()
                _, fixed_assets = boundary(asset_store, cid) if scope == "instance" else (None, {})
                if scope == "instance":
                    state_path = contained(home, base / "boundary/state.json")
                    if state_path.exists():
                        boundary_raw = _small(state_path)
                for kind, path in sources.items():
                    contained(home, path)
                    db = sqlite3.connect(
                        path.resolve().as_uri() + "?mode=ro",
                        uri=True,
                        isolation_level=None,
                        timeout=0,
                    )
                    readers[kind] = db
                    db.execute("BEGIN")
                    db.execute("SELECT count(*) FROM sqlite_schema").fetchone()
                limits = ViewLimits(sources, clock, pinned_at=pinned_at)
            with io_checks(check), validation_stage(), ExitStack() as native:
                for db in readers.values():
                    native.callback(db.close)
                    native.enter_context(native_checks(db))
                if boundary_raw is not None:
                    entries.append(
                        _put(staging, f"companions/{cid}/boundary/state.json", boundary_raw)
                    )
                for kind, db in readers.items():
                    check()
                    name = f"companions/{cid}/{kind}/{kind}.sqlite3"
                    target = staging / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb"):
                        pass
                    destination = sqlite3.connect(target)
                    try:
                        from .sqlite_io import copy_database

                        copy_database(db, destination, pages=128, check=check)
                        if scope == "memory":
                            destination.execute("DELETE FROM recollect_progress")
                            destination.commit()
                        with native_checks(destination):
                            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                                raise SourceError("snapshot_inconsistent")
                    finally:
                        destination.close()
                    entries.append(_entry(staging, name))
                uid = (
                    readers["memory"]
                    .execute("SELECT value FROM metadata WHERE key='companion_uid'")
                    .fetchone()
                )
                companion = {
                    "id": cid,
                    "uid": json.loads(uid[0]) if uid else None,
                    "memory": f"companions/{cid}/memory/memory.sqlite3",
                }
                if scope == "instance":
                    companion["ledger"] = f"companions/{cid}/ledger/ledger.sqlite3"
                    companion["configuration_assets"] = dependencies
                referenced, declarations = set(), []
                for kind, db in readers.items():
                    tables = (
                        ["records", "revisions"]
                        if kind == "memory"
                        else [
                            "inbound",
                            "record",
                            "invocation",
                            "intent",
                            "attempt",
                            "dedupe",
                            "audit",
                        ]
                    )
                    for table in tables:
                        for row in db.execute(f'SELECT data FROM "{table}"'):
                            check()
                            if kind == "ledger":
                                referenced.update(
                                    ledger_references(
                                        table, json.loads(row[0]), details=declarations
                                    )
                                )
            readers.clear()
            limits = None
            asset_files, assets, asset_missing = capture(
                asset_store,
                cid,
                fixed_assets,
                referenced | {row["asset_id"] for row in dependencies},
                staging,
                declarations=declarations,
                check=check,
            )
            entries.extend(asset_files)
            missing.extend(asset_missing)
        finally:
            for db in readers.values():
                db.close()
        groups.append(_group(epoch, entries))
        if scope == "instance":
            state_entries = []
            for pid in sorted(expected):
                identifier(pid)
                metadata = manifests.get(pid)
                installed = versions.get(pid)
                if (
                    metadata is None
                    or installed is None
                    or installed.get("plugin_version") != str(metadata.version)
                    or installed.get("data_version")
                    != metadata.data.get("data", {}).get("version", 1)
                ):
                    missing.append(
                        {"component": f"plugin:{cid}:{pid}", "reason": "plugin_version_unverified"}
                    )
                    continue
                data = contained(home, base / "plugin-data" / pid)
                paths = []
                files = sorted(data.rglob("*")) if data.exists() else []
                if files and metadata.backup_mode == "stateless":
                    missing.append(
                        {
                            "component": f"plugin:{cid}:{pid}",
                            "reason": "stateless_has_data",
                        }
                    )
                elif files:
                    for path in files:
                        contained(home, path)
                        if path.is_file():
                            name = path.relative_to(home).as_posix()
                            state_entries.append(_copy(home, path, staging, name, check))
                            paths.append(name)
                manifest_path = "repository:plugins/" + pid + "/plugin.json"
                if metadata.path.is_relative_to(home):
                    manifest_path = f"plugins/{pid}/plugin.json"
                    for path in sorted(metadata.path.parent.rglob("*")):
                        contained(home, path)
                        if not path.is_file() or "__pycache__" in path.parts:
                            continue
                        if path.suffix not in {".py", ".json", ".toml", ".md"}:
                            missing.append(
                                {"component": f"plugin_code:{pid}", "reason": "opaque_plugin_file"}
                            )
                            continue
                        name = path.relative_to(home).as_posix()
                        if not (staging / name).exists():
                            state_entries.append(_copy(home, path, staging, name, check))
                plugins.append(
                    {
                        "companion_id": cid,
                        "id": pid,
                        "version": str(metadata.version),
                        "data_version": installed["data_version"],
                        "manifest_path": manifest_path,
                        "version_path": f"companions/{cid}/plugin-versions.json",
                        "state_paths": paths,
                    }
                )
            if version_path.exists():
                if _small(version_path) != versions_raw:
                    raise SourceError("snapshot_inconsistent")
                state_entries.append(
                    _put(staging, f"companions/{cid}/plugin-versions.json", versions_raw)
                )
            for path in sorted(management.glob("*.json")) if management.exists() else []:
                state_entries.append(
                    _copy(home, path, staging, path.relative_to(home).as_posix(), check)
                )
            entries.extend(state_entries)
            if state_entries:
                groups.append(_group(epoch, state_entries))
    return {
        "files": entries,
        "companions": [companion],
        "plugins": plugins,
        "assets": assets,
        "consistency_groups": groups,
        "missing": missing,
        "configuration_versions": {name: _hash(raw) for name, raw in documents.items()},
    }


async def _thread(function, check):
    cancelled = threading.Event()

    def checkpoint():
        if cancelled.is_set():
            raise SourceError("cancelled")
        if check:
            check()

    task = asyncio.create_task(asyncio.to_thread(function, checkpoint))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


def _prepare_source(home, staging, scope, cid, repo_root, check):
    home, staging = Path(home).resolve(), Path(staging).absolute()
    contained(Path(staging.anchor), staging)
    if staging.exists() and any(staging.iterdir()):
        raise SourceError("capture_target_not_empty")
    staging.mkdir(parents=True, exist_ok=True)
    root = Path(repo_root or repository_root())
    instance, documents, exclusions = _configuration(home, root)
    selected = [identifier(cid)] if scope == "memory" else instance["companions"]
    if any(c not in instance["companions"] for c in selected):
        raise SourceError("capture_companion_unknown")
    manifests = discover(root / "plugins", home / "plugins", repo_root=root)
    result = {
        key: []
        for key in ("files", "companions", "plugins", "assets", "consistency_groups", "missing")
    }
    versions = {name: _hash(raw) for name, raw in documents.items()}
    check()
    return home, staging, root, documents, exclusions, selected, manifests, result, versions


def _work_directory(staging, temporaries):
    work = staging.parent / (".capture-" + uuid4().hex)
    work.mkdir()
    temporaries.append(str(work))
    return work


def _deployment_source(home, work, selected, root, check):
    from .deployment_capture import capture_deployment

    return capture_deployment(home, work, selected, root, check)


def _publish_source_documents(
    home,
    staging,
    work,
    deployment_entries,
    documents,
    exclusions,
    result,
    epoch,
    audit_writers,
    check,
):
    if deployment_entries is not None:
        for entry in deployment_entries:
            check()
            destination = contained(staging, staging / entry["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            publish_no_replace(work / entry["path"], destination)
        result["files"].extend(deployment_entries)
        if deployment_entries:
            result["consistency_groups"].append(_group(epoch, deployment_entries))
    config_entries = []
    for name, raw in documents.items():
        check()
        document = redact_document(yaml.safe_load(raw), name, exclusions)
        config_entries.append(
            _put(staging, name, yaml.safe_dump(document, allow_unicode=True).encode())
        )
        if name != "clytie.local.yaml":
            companion_id = name.split("/")[1]
            for relative in document.get("persona", {}).get("files", {}).values():
                check()
                path = contained(
                    home / "companions" / companion_id / "persona",
                    home / "companions" / companion_id / "persona" / relative,
                )
                target = path.relative_to(home).as_posix()
                config_entries.append(_put(staging, target, _small(path)))
    result["files"].extend(config_entries)
    result["consistency_groups"].append(_group(epoch, config_entries))
    state_entries = []
    for name in (
        "supervisor/silence.json",
        "supervisor/notify-state.json",
        "supervisor/old-instances.json",
    ):
        path = contained(home, home / name)
        if path.exists():
            state_entries.append(_put(staging, name, _small(path)))
    if (home / "supervisor/audit.jsonl").exists() and not audit_writers:
        result["missing"].append({"component": "audit", "reason": "audit_source_unavailable"})
    for writer in audit_writers:
        check()
        rows = writer.seal()
        audit_rows = []
        for row in rows:
            copied = _copy(home, home / row["path"], staging, row["path"], check)
            state_entries.append(copied)
            audit_rows.append(dict(path=row["path"], size=copied["size"], sha256=copied["sha256"]))
        state_entries.append(
            _put(
                staging,
                "supervisor/audit.jsonl.rotation.json",
                json.dumps(
                    dict(schema_version=1, audit=True, restore_index=True, archives=audit_rows)
                ).encode(),
            )
        )
    if state_entries:
        result["files"].extend(state_entries)
        result["consistency_groups"].append(_group(epoch, state_entries))
    check()


def _finish_source(
    home,
    staging,
    scope,
    source_instance_id,
    epoch,
    exclusions,
    versions,
    result,
    temporaries,
    check,
):
    with config_commit_lock(home):
        if any(_hash(_small(home / name)) != hashed for name, hashed in versions.items()):
            result["missing"].append(
                {"component": "configuration", "reason": "snapshot_inconsistent"}
            )
    if scope == "instance":
        from .assets import consolidate

        consolidate(staging, result, epoch)
    manifest = {
        "schema_version": 2 if result["assets"] else 1,
        "scope": scope,
        "completeness": "partial" if result["missing"] else "complete",
        "source_instance_id": source_instance_id,
        "source_registration": source_registration(home),
        "code_version": version("clytie"),
        "credential_exclusions": exclusions if scope == "instance" else [],
        "configuration_versions": versions,
        **{k: result[k] for k in result if k != "files"},
    }
    check()
    return {
        "manifest": manifest,
        "files": result["files"],
        "missing": result["missing"],
        "temporary_directories": temporaries,
    }


async def capture_instance(
    home,
    clock,
    staging,
    *,
    source_instance_id,
    online=None,
    online_ids=frozenset(),
    scope="instance",
    cid=None,
    epoch,
    repo_root=None,
    check=None,
    audit_writers=(),
):
    if scope not in {"instance", "memory"} or not source_instance_id or not epoch:
        raise SourceError("capture_request_invalid")
    (
        home,
        staging,
        root,
        documents,
        exclusions,
        selected,
        manifests,
        result,
        versions,
    ) = await _thread(
        lambda tick: _prepare_source(home, staging, scope, cid, repo_root, tick), check
    )
    temporaries = []
    for companion_id in selected:
        work = await _thread(lambda _: _work_directory(staging, temporaries), check)
        try:
            if companion_id in online_ids:
                if online is None:
                    raise SourceError("snapshot_unsupported")
                captured = await online(companion_id, work, epoch)
                if captured.get("configuration_versions") != versions:
                    raise SourceError("snapshot_inconsistent")
            else:
                captured = await _thread(
                    lambda tick: _offline(
                        home,
                        companion_id,
                        work,
                        clock,
                        epoch,
                        scope,
                        documents,
                        manifests,
                        tick,
                        root,
                    ),
                    check,
                )
            await _thread(
                lambda tick: _accept(
                    captured, work, staging, result, documents, scope, companion_id
                ),
                check,
            )
        except Exception as exc:
            result["missing"].append(
                {
                    "component": f"companion:{companion_id}",
                    "reason": str(exc) if isinstance(exc, SourceError) else "snapshot_unavailable",
                }
            )
    if scope == "instance":
        work = await _thread(lambda _: _work_directory(staging, temporaries), check)
        deployment_entries = None
        try:
            deployment_entries = await _thread(
                lambda tick: _deployment_source(home, work, selected, root, tick), check
            )
        except Exception as exc:
            result["missing"].append(
                {
                    "component": "deployment_documents",
                    "reason": str(exc)
                    if isinstance(exc, SourceError)
                    else "deployment_unavailable",
                }
            )
        await _thread(
            lambda tick: _publish_source_documents(
                home,
                staging,
                work,
                deployment_entries,
                documents,
                exclusions,
                result,
                epoch,
                audit_writers,
                tick,
            ),
            check,
        )
    return await _thread(
        lambda tick: _finish_source(
            home,
            staging,
            scope,
            source_instance_id,
            epoch,
            exclusions,
            versions,
            result,
            temporaries,
            tick,
        ),
        check,
    )
