"""在线数据持有者响应本体组捕获；不启动运行时或执行恢复。"""

import asyncio
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from clytie.api.management_io import FileCommands
from clytie.api.management_journal import ManagementBusy, gate
from clytie.plugin_host.snapshots import contained, identifier

from .capture import capture_group
from .coordination import CaptureError, companion_coordinator
from .io_client import heavy_io, io_read
from .native_sqlite import native_checks


def _unavailable(cid, epoch, reason):
    return {
        "status": "unavailable",
        "group": "memory_ledger_progress",
        "companion_id": cid,
        "epoch": epoch,
        "reason": reason,
    }


@heavy_io
def _describe(paths, check):
    members = {}
    for name, path in paths.items():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := io_read(stream, 1024 * 1024):
                if check:
                    check()
                digest.update(chunk)
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            with native_checks(db):
                member = {
                    "path": path.name,
                    "size": path.stat().st_size,
                    "sha256": digest.hexdigest(),
                    "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
                }
                if name == "memory":
                    metadata = dict(
                        db.execute(
                            "SELECT key,value FROM metadata WHERE key IN ('generation','companion_uid')"
                        )
                    )
                    member["generation"] = json.loads(metadata.get("generation", "0"))
                    member["companion_uid"] = json.loads(metadata.get("companion_uid", "null"))
                members[name] = member
        finally:
            db.close()
    return members


async def _describe_async(paths, check):
    cancelled = threading.Event()

    def checkpoint():
        if cancelled.is_set():
            raise CaptureError("cancelled")
        if check:
            check()

    task = asyncio.create_task(asyncio.to_thread(_describe, paths, checkpoint))
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


async def capture_companion(companion, directory, *, epoch, check=None):
    """仅对现有在线持有者捕获；返回组证据，不等同于 instance complete。

    调用方须像管理请求一样跟踪本协程，停止时等它释放读视图。
    这里只检查并使用已存在的 store，不调用 memory_store/open/recover。
    """
    cid = identifier(companion.cid)
    home = Path(companion.home).resolve()
    base = contained(home, home / "companions" / cid)
    resources = companion.host.memory_resources.get(cid)
    runtime = companion.runtime
    if not resources or runtime.guard is None or runtime.ledger is None:
        return _unavailable(cid, epoch, "snapshot_unavailable")
    store = resources[0]
    ledger = runtime.ledger
    coordinator = companion_coordinator(home, cid, companion.clock)
    if any(
        item.coordination is None
        or Path(item.coordination.path).resolve() != coordinator.path.resolve()
        for item in (store, ledger)
    ):
        return _unavailable(cid, epoch, "snapshot_unsupported")
    view = None
    try:
        # 操作锁与迁移状态只读检查；持有期间仅固定视图，不复制数据库。
        async with FileCommands(companion.clock) as files:
            manager = gate(home, cid)

            def acquire():
                manager.__enter__()

            await files.run(acquire)
            try:
                blocked = await files.run(
                    lambda: (base / "management/blocked.json").exists() or runtime.journal.exists()
                )
                if blocked:
                    return _unavailable(cid, epoch, "snapshot_unavailable")
                view = await capture_group(
                    {"memory": store.path, "ledger": ledger.path},
                    coordinator,
                    epoch=epoch,
                    owner=getattr(companion, "storage_owner", None),
                )
            finally:
                await files.run(manager.__exit__, None, None, None)
    except ManagementBusy:
        return _unavailable(cid, epoch, "capture_busy")
    except CaptureError as exc:
        return _unavailable(cid, epoch, exc.code)
    except (OSError, sqlite3.Error):
        return _unavailable(cid, epoch, "snapshot_inconsistent")
    try:
        paths = await view.materialize(directory, check=check)
        # 数据已离开业务门闩；只在本次独占输出上核验，不读活动源文件。
        members = await _describe_async(paths, check)
        return {
            "status": "group_complete",
            "group": "memory_ledger_progress",
            "companion_id": cid,
            "epoch": epoch,
            "captured_at": view.captured_at,
            "members": members,
            "requires_groups": ["configuration", "plugins", "assets", "management"],
        }
    finally:
        await view.close()


class _LiveProvider:
    """调用已装配对象；所有阶段和迟到执行者仍登记在运行时中。"""

    def __init__(self, companion, runtime, implementation):
        self.companion, self.runtime, self.implementation = companion, runtime, implementation

    def __getattr__(self, name):
        async def call(*args):
            task = asyncio.current_task()
            self.runtime.calls.add(task)
            self.companion.management_requests.add(task)
            try:
                return await getattr(self.implementation, name)(*args)
            finally:
                self.runtime.calls.discard(task)
                self.companion.management_requests.discard(task)

        return call


def _online_files(companion, work, documents, versions_raw, expected, scope, check):
    from clytie.config import load_home, thaw

    from .configuration import config_commit_lock
    from .source import SourceError, _copy, _put, _small

    # Manifest paths are canonical. Compare the same namespace even when the
    # caller supplied a Windows short alias or an equivalent relative path.
    home, cid, host = Path(companion.home).resolve(), companion.cid, companion.host
    base = home / "companions" / cid
    if companion.report is None:
        raise SourceError("configuration_runtime_unverified")
    with config_commit_lock(home):
        if any(_small(home / name) != raw for name, raw in documents.items()):
            raise SourceError("snapshot_inconsistent")
        current = load_home(home, repo_root=host.repo_root)
    if current.errors or thaw(current.plugin_configs.get(cid)) != thaw(
        companion.report.plugin_configs.get(cid)
    ):
        raise SourceError("configuration_runtime_unverified")
    # 同一加载配置对应业务状态；备份配置自身变化不改变插件状态含义。
    old_instance = thaw(companion.report.instance)
    new_instance = thaw(current.instance)
    old_instance.pop("backup", None)
    new_instance.pop("backup", None)
    if old_instance != new_instance:
        raise SourceError("configuration_runtime_unverified")
    for path in (base / "management/operations").glob("*.json"):
        if json.loads(_small(contained(home, path))).get("state") not in {
            "committed",
            "cancelled",
            "interrupted",
            "failed",
        }:
            raise SourceError("snapshot_unavailable")
    if any(p.suffix == ".json" for p in (base / "plugin-recovery").glob("*") if p.is_file()):
        raise SourceError("snapshot_unavailable")
    entries, plugins, missing = [], [], []
    if scope == "memory":
        return entries, plugins, missing
    versions = json.loads(versions_raw)
    for pid in sorted(expected):
        metadata, installed = host.manifests.get(pid), versions.get(pid)
        runtime = host.runtimes.get((cid, pid))
        active = runtime is not None and runtime.status == "active"
        if (
            metadata is None
            or installed is None
            or installed.get("plugin_version") != str(metadata.version)
            or installed.get("data_version") != metadata.data.get("data", {}).get("version", 1)
        ):
            missing.append(
                {"component": f"plugin:{cid}:{pid}", "reason": "plugin_version_unverified"}
            )
            continue
        if json.loads(_small(metadata.path)) != thaw(metadata.data):
            raise SourceError("plugin_manifest_changed")
        data = contained(home, base / "plugin-data" / pid)
        paths = []
        files = sorted(data.rglob("*")) if data.exists() else []
        if metadata.backup_mode == "stateless" and files:
            missing.append({"component": f"plugin:{cid}:{pid}", "reason": "stateless_has_data"})
        elif active:
            if (
                metadata.backup_mode == "unsupported"
                or metadata.backup_mode == "stateless"
                and files
            ):
                missing.append(
                    {
                        "component": f"plugin:{cid}:{pid}",
                        "reason": "snapshot_unsupported"
                        if metadata.backup_mode == "unsupported"
                        else "stateless_has_data",
                    }
                )
        elif runtime is not None and (
            runtime.status not in {"disabled", "stopped"} or runtime.calls or runtime.feeds
        ):
            missing.append({"component": f"plugin:{cid}:{pid}", "reason": "snapshot_unavailable"})
        else:
            for path in files:
                contained(home, path)
                if path.is_file():
                    name = path.relative_to(home).as_posix()
                    entries.append(_copy(home, path, work, name, check))
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
                entries.append(_copy(home, path, work, path.relative_to(home).as_posix(), check))
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
    if versions_raw != b"{}" or (base / "plugin-versions.json").exists():
        entries.append(_put(work, f"companions/{cid}/plugin-versions.json", versions_raw))
    for path in sorted((base / "management/operations").glob("*.json")):
        entries.append(_copy(home, path, work, path.relative_to(home).as_posix(), check))
    return entries, plugins, missing


async def capture_online(companion, work, epoch, *, scope="instance", check=None, space_guard=None):
    """source.capture_instance 的在线回调；只使用本次仍存活的本体/插件持有者。"""
    from contextlib import ExitStack
    from datetime import timedelta

    import yaml

    from clytie.plugin_host.locking import OperationBusy, file_lock
    from clytie.sdk import CancelToken
    from clytie.sdk.backup import BackupRequest

    from .configuration import config_commit_lock
    from .providers import NamespaceSink, ProviderCapture
    from .source import (
        SourceError,
        _configuration,
        _copy,
        _entry,
        _group,
        _hash,
        _put,
        _small,
        _thread,
    )

    home, cid, host = Path(companion.home).resolve(), identifier(companion.cid), companion.host
    from clytie.assets import AssetStore

    from .asset_references import ledger_references
    from .assets import boundary, capture, configuration

    asset_store = AssetStore(home, companion.clock)
    fixed_assets = {}
    boundary_raw = None
    work = Path(work)
    base = home / "companions" / cid
    resources = host.memory_resources.get(cid)
    runtime = companion.runtime
    if (
        scope not in {"instance", "memory"}
        or not resources
        or runtime.guard is None
        or runtime.ledger is None
    ):
        raise SourceError("snapshot_unavailable")
    store, ledger = resources[0], runtime.ledger
    coordinator = companion_coordinator(home, cid, companion.clock)
    if any(
        item.coordination is None
        or Path(item.coordination.path).resolve() != coordinator.path.resolve()
        for item in (store, ledger)
    ):
        raise SourceError("snapshot_unsupported")
    instance, documents, exclusions = await _thread(
        lambda tick: _configuration(home, host.repo_root), check
    )
    config = yaml.safe_load(documents[f"companions/{cid}/companion.yaml"])
    dependencies = (
        await _thread(
            lambda tick: configuration(
                home, cid, host.repo_root, host.manifests, documents=documents
            ),
            check,
        )
        if scope == "instance"
        else []
    )
    expected = set(config.get("plugins", {})) | set(instance.get("plugins", {}))
    if config.get("model", {}).get("plugin"):
        expected.add(config["model"]["plugin"])
    expected.update(c["plugin"] for c in config.get("channels", []))
    expected.update(pid for owner, pid in host.runtimes if owner == cid)
    version_path = base / "plugin-versions.json"
    versions_raw = await _thread(
        lambda tick: _small(version_path) if version_path.exists() else b"{}", check
    )
    expected.update(json.loads(versions_raw))
    providers = {}
    sessions = getattr(companion, "backup_provider_sessions", None)
    if sessions is None:
        sessions = companion.backup_provider_sessions = set()
    sessions.difference_update(
        session for session in tuple(sessions) if not session.result()["resource_pending"]
    )
    view = None

    async def monitored(operation):
        if space_guard:
            return await space_guard.watch(operation, clock=companion.clock)
        return await operation

    try:
        async with FileCommands(companion.clock) as files:
            locks = ExitStack()

            def acquire_lifecycle():
                try:
                    locks.enter_context(gate(home, cid))
                    if (base / "management/blocked.json").exists() or runtime.journal.exists():
                        raise SourceError("snapshot_unavailable")
                    locks.enter_context(
                        host.operations.hold(cid, sorted(expected), "backup_online")
                    )
                    # 备份只固定版本文件；Operations.metadata 的既定实现不变。
                    locks.enter_context(
                        file_lock(base / "plugin-versions.lock", {"operation": "backup_online"})
                    )
                    if version_path.exists() and _small(version_path) != versions_raw:
                        raise SourceError("snapshot_inconsistent")
                except BaseException:
                    locks.close()
                    raise

            try:
                await files.run(acquire_lifecycle)
                entries, plugins, missing = await monitored(
                    _thread(
                        lambda tick: _online_files(
                            companion, work, documents, versions_raw, expected, scope, tick
                        ),
                        check,
                    )
                )
                if scope == "instance":
                    for plugin in plugins:
                        pid = plugin["id"]
                        holder = host.runtimes.get((cid, pid))
                        if (
                            holder is None
                            or holder.status != "active"
                            or holder.manifest.backup_mode != "snapshot"
                        ):
                            continue
                        registrations = [
                            r
                            for r in host.registrations(cid, "backup.snapshot")
                            if r.plugin_id == pid
                        ]
                        if len(registrations) != 1:
                            missing.append(
                                {
                                    "component": f"plugin:{cid}:{pid}",
                                    "reason": "snapshot_unsupported",
                                }
                            )
                            continue
                        root = work / f"companions/{cid}/plugin-data/{pid}"
                        request = BackupRequest(
                            operation_id=epoch,
                            companion_id=cid,
                            plugin_id=pid,
                            data_version=plugin["data_version"],
                            deadline=companion.clock.now() + timedelta(seconds=10),
                        )
                        prefix = f"companions/{cid}/plugin-data/{pid}/"
                        forbidden = [
                            account["secret"][len(prefix) :]
                            for account in instance.get("accounts", {}).values()
                            if isinstance(account, dict)
                            and isinstance(account.get("secret"), str)
                            and account["secret"].startswith(prefix)
                        ]
                        run = ProviderCapture(
                            _LiveProvider(companion, holder, registrations[0].implementation),
                            request,
                            companion.clock,
                            NamespaceSink(root, credentials=forbidden, check=check),
                            cancel=CancelToken(),
                        )
                        providers[pid] = run
                        sessions.add(run)
                        await monitored(run.prepare())

                async def capture_providers(checkpoint):
                    nonlocal fixed_assets, boundary_raw
                    for run in providers.values():
                        checkpoint()
                        await run.capture(epoch)
                        checkpoint()
                    if scope == "instance":

                        def read_boundary():
                            _, assets = boundary(asset_store, cid)
                            state_path = contained(home, base / "boundary/state.json")
                            raw = _small(state_path) if state_path.exists() else None
                            return assets, raw

                        fixed_assets, boundary_raw = await files.run(read_boundary)
                        checkpoint()

                view = await capture_group(
                    {"memory": store.path, "ledger": ledger.path},
                    coordinator,
                    epoch=epoch,
                    capture=capture_providers,
                    owner=getattr(companion, "storage_owner", None),
                )
                # 元数据和插件生命周期锁仍持有，业务提交门闩已经释放。
                paths = await monitored(view.materialize(work / ".core", check=check))
                view = None
                if boundary_raw is not None:
                    entries.append(
                        await files.run(
                            _put, work, f"companions/{cid}/boundary/state.json", boundary_raw
                        )
                    )
                for pid, run in providers.items():
                    await monitored(run.materialize())
                    await run.release()
                    result = run.result()
                    if result["status"] != "complete":
                        missing.append(
                            {
                                "component": f"plugin:{cid}:{pid}",
                                "reason": "provider_partial",
                                "errors": result["errors"],
                                "resource_pending": result["resource_pending"],
                            }
                        )
                        continue
                    plugin = next(p for p in plugins if p["id"] == pid)
                    plugin["revision"] = run.view.revision
                    for entry in result["entries"]:
                        name = f"companions/{cid}/plugin-data/{pid}/" + entry["relative_path"]
                        actual = await monitored(
                            _thread(lambda tick, name=name: _entry(work, name, tick), check)
                        )
                        if any(actual[key] != entry[key] for key in ("size", "sha256")):
                            raise SourceError("snapshot_inconsistent")
                        entries.append(actual)
                        plugin["state_paths"].append(name)

                @heavy_io
                def core_files(check):
                    core_entries = []
                    referenced, declared = set(), []
                    uid = None
                    for kind, path in paths.items():
                        if scope == "memory" and kind == "ledger":
                            continue
                        db = sqlite3.connect(path)
                        try:
                            with native_checks(db):
                                if kind == "memory":
                                    row = db.execute(
                                        "SELECT value FROM metadata WHERE key='companion_uid'"
                                    ).fetchone()
                                    uid = json.loads(row[0]) if row else None
                                    if scope == "memory":
                                        db.execute("DELETE FROM recollect_progress")
                                        db.commit()
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
                                                    table, json.loads(row[0]), details=declared
                                                )
                                            )
                        finally:
                            db.close()
                        name = f"companions/{cid}/{kind}/{kind}.sqlite3"
                        core_entries.append(_copy(work, path, work, name, check))
                    return core_entries, uid, referenced, declared

                core, uid, referenced, ledger_assets = await monitored(_thread(core_files, check))
                entries.extend(core)
                declarations = tuple(ledger_assets) + tuple(
                    asset
                    for run in providers.values()
                    if run.view is not None
                    for asset in run.view.assets
                )
                asset_files, assets, asset_missing = await monitored(
                    _thread(
                        lambda tick: capture(
                            asset_store,
                            cid,
                            fixed_assets,
                            referenced | {row["asset_id"] for row in dependencies},
                            work,
                            declarations=declarations,
                            check=tick,
                        ),
                        check,
                    )
                )
                entries.extend(asset_files)
                missing.extend(asset_missing)

                def recheck_configuration():
                    with config_commit_lock(home):
                        if any(_small(home / name) != raw for name, raw in documents.items()):
                            raise SourceError("snapshot_inconsistent")

                await files.run(recheck_configuration)
                companion_entry = {
                    "id": cid,
                    "uid": uid,
                    "memory": f"companions/{cid}/memory/memory.sqlite3",
                }
                if scope == "instance":
                    companion_entry["ledger"] = f"companions/{cid}/ledger/ledger.sqlite3"
                    companion_entry["configuration_assets"] = dependencies
                return {
                    "files": entries,
                    "companions": [companion_entry],
                    "plugins": plugins,
                    "assets": assets,
                    "consistency_groups": [_group(epoch, entries)],
                    "missing": missing,
                    "configuration_versions": {name: _hash(raw) for name, raw in documents.items()},
                }
            finally:
                await files.run(locks.close)
    except CaptureError as exc:
        raise SourceError(exc.code) from None
    except (ManagementBusy, OperationBusy):
        raise SourceError("capture_busy") from None
    except (OSError, sqlite3.Error):
        raise SourceError("snapshot_inconsistent") from None
    finally:
        if view is not None:
            await view.close()
        for run in providers.values():
            await run.release()
            if not run.result()["resource_pending"]:
                sessions.discard(run)
