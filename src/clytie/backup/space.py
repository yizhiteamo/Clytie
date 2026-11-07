"""备份空间的保守元数据估算和持续保护；估算不授予内容读取或清理权。"""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
import threading
from pathlib import Path

from clytie.clock import SystemClock
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained, identifier

from .container import CHUNK, MAX_FILES, MAX_MANIFEST, _safe
from .errors import BackupError
from .journal import BackupJournal
from .repository import repository_lock


class SpaceError(BackupError):
    def __init__(self, dimension):
        self.dimension = dimension
        super().__init__("space_insufficient")


def _walk(paths, check=None, *, volatile=False):
    """只读属性，拒绝链接和未知类型；在两次 yield 间不持目录句柄。"""
    pending = list(map(Path, paths))
    seen, count = set(), 0
    while pending:
        if check:
            check()
        path = pending.pop()
        _safe(path)
        try:
            info = path.stat()
        except FileNotFoundError:
            # Optional roots may be absent; a listed child vanishing invalidates
            # this estimate instead of silently turning its size into zero.
            if path in paths or volatile:
                continue
            raise BackupError("snapshot_unavailable") from None
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as directory:
                pending.extend(Path(entry.path) for entry in directory)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("snapshot_unavailable")
        if path in seen:
            continue
        seen.add(path)
        count += 1
        if count > MAX_FILES:
            raise BackupError("container_limit")
        yield path, info


def _volume(path):
    path = Path(path)
    _safe(path)
    while not path.exists():
        path = path.parent
    return str(path.stat().st_dev), str(path)


def _bytes(paths, check=None):
    return sum(info.st_size for _, info in _walk(list(paths), check))


def estimate(
    home, operation_id, *, companions, scope, reserve_bytes, destination, check=None, clock=None
):
    """以明确候选的属性计算峰值；不读取任意凭据、库内容或模型缓存。"""
    home = Path(home).resolve()
    BackupJournal.path_for(home, operation_id)
    if scope not in {"instance", "memory"} or reserve_bytes < 0:
        raise ValueError("invalid_space_request")
    roots = [home / "shared/assets"]
    databases = []
    for cid in companions:
        base = home / "companions" / identifier(cid)
        roots.append(base / "assets")
        for kind in ("memory", "ledger"):
            database = base / kind / (kind + ".sqlite3")
            roots.extend([database, database.with_name(database.name + "-wal")])
            databases.append(database)
        if scope == "instance":
            roots.extend(
                base / name
                for name in (
                    "companion.yaml",
                    "persona",
                    "plugin-versions.json",
                    "plugin-data",
                    "management/operations",
                )
            )
    if scope == "instance":
        roots.extend(home / name for name in ("clytie.local.yaml", "plugins"))
        supervisor = home / "supervisor"
        if supervisor.exists():
            roots.extend(
                path
                for path in supervisor.iterdir()
                if path.name
                in {
                    "silence.json",
                    "notify-state.json",
                    "old-instances.json",
                    "audit.jsonl",
                }
                or path.name.startswith("audit.jsonl.archive.")
            )
    source_bytes, file_count, database_bytes = 0, 0, 0
    for path, info in _walk(roots, check):
        source_bytes += info.st_size
        file_count += 1
        if path in databases:
            database_bytes += info.st_size

    # zlib compressBound(n) = n + (n>>12) + (n>>14) + (n>>25) + 13.
    # Each file adds F/E frames, one path (<=1024 bytes), and a manifest row.
    chunks = math.ceil(source_bytes / CHUNK) + file_count
    format_bytes = (
        MAX_MANIFEST + file_count * 1100 + chunks * ((CHUNK >> 12) + (CHUNK >> 14) + 34) + 4 * 1024
    )
    package_bytes = source_bytes + format_bytes
    core_bytes = database_bytes + sum(
        p.with_name(p.name + "-wal").stat().st_size if p.with_name(p.name + "-wal").exists() else 0
        for p in databases
    )
    # Existing online code retains a core view materialization and its exported
    # database, then builds the package and a separate verified expansion.
    temporary_bytes = core_bytes + 2 * source_bytes + package_bytes
    # Small databases can legitimately append several MiB during a short online
    # capture. Give that normal burst bounded headroom, never an unbounded WAL.
    wal_budget = max(CHUNK, min(reserve_bytes, max(64 * CHUNK, core_bytes)))
    wal = []
    volumes = {}

    def require(path, amount, category):
        device, probe = _volume(path)
        row = volumes.setdefault(
            device,
            dict(
                device=device,
                probe=probe,
                required_bytes=reserve_bytes,
                reserve_bytes=reserve_bytes,
                categories={},
            ),
        )
        row["required_bytes"] += amount
        row["categories"][category] = row["categories"].get(category, 0) + amount

    work = home / "run/backup-work" / operation_id
    require(work, temporary_bytes, "temporary")
    # Publication on the work volume is a rename/link. A distinct destination
    # volume must reserve another package; current publisher may still reject it.
    if _volume(destination)[0] != _volume(work)[0]:
        require(destination, package_bytes, "published_package")
    by_device = {}
    for database in databases:
        path = database.with_name(database.name + "-wal")
        _safe(path)
        device, _ = _volume(database.parent)
        size = path.stat().st_size if path.exists() else 0
        wal.append(dict(path=str(path), baseline_bytes=size, device=device))
        by_device.setdefault(device, database.parent)
    for path in by_device.values():
        require(path, wal_budget, "wal_growth")
    from .old_instances import OldInstances
    from .status import storage_categories, usage

    old = OldInstances(home, clock or SystemClock()).status()
    occupied = dict(
        regular_repository=usage(destination),
        categories=storage_categories(home),
        old_instances=dict(known_bytes=old["known_bytes"], unknown=old["unknown"]),
    )
    return dict(
        schema_version=1,
        operation_id=operation_id,
        estimated=True,
        guaranteed=False,
        source_bytes=source_bytes,
        source_files=file_count,
        core_bytes=core_bytes,
        package_bytes=package_bytes,
        format_bytes=format_bytes,
        temporary_budget_bytes=temporary_bytes,
        wal_growth_budget_bytes=wal_budget,
        reserve_bytes=reserve_bytes,
        work=str(work),
        wal=wal,
        volumes=list(volumes.values()),
        occupied=occupied,
    )


class SpaceGuard:
    """多执行者共享预算，短证据锁；扫描及 free 查询均在锁外执行。"""

    def __init__(self, home, operation_id, *, clock=None):
        self.clock = clock or SystemClock()
        self.home = Path(home).resolve()
        BackupJournal.path_for(self.home, operation_id)
        self.path = contained(
            self.home, self.home / "supervisor/backup-space" / (operation_id + ".json")
        )
        self.lock_path = self.path.with_suffix(".lock")
        self.plan = None
        self._scan = None
        self._scan_bytes = 0
        self._next_scan = 0
        self._next_evidence = 0
        self._thread_lock = threading.Lock()

    def _lock(self):
        return repository_lock(self.lock_path, "backup_space")

    def _read(self):
        contained(self.home, self.path)
        return json.loads(atomic_read(self.path))

    def _write(self, value):
        atomic_write(self.path, canonical(value), retry_sharing=True)

    def start(self, plan):
        if plan["operation_id"] != self.path.stem:
            raise BackupError("snapshot_inconsistent")
        with self._lock():
            if self.path.exists():
                raise BackupError("operation_mismatch")
            self._write(
                dict(
                    plan=plan,
                    state="estimated",
                    peak_temporary_bytes=0,
                    peak_wal_growth_bytes=0,
                    observed_volumes=[],
                    failure=None,
                )
            )
        self.plan = plan
        self.check(preflight=True)

    def load(self):
        self.plan = self._read()["plan"]
        if self.plan["operation_id"] != self.path.stem:
            raise BackupError("snapshot_inconsistent")
        return self

    def check(self, *, preflight=False, force=False):
        with self._thread_lock:
            if force:
                self._next_evidence = 0
            return self._check(preflight=preflight)

    def _check(self, *, preflight=False):
        plan = self.plan
        if plan is None:
            raise BackupError("snapshot_unavailable")
        observed = []
        dimension = None
        for volume in plan["volumes"]:
            device, _ = _volume(volume["probe"])
            if device != volume["device"]:
                raise BackupError("source_identity_mismatch")
            free = shutil.disk_usage(volume["probe"]).free
            observed.append(dict(device=device, free_bytes=free))
            required = volume["required_bytes"] if preflight else volume["reserve_bytes"]
            if free < required:
                dimension = "preflight_free" if preflight else "reserve_free"
        growth = {}
        for row in plan["wal"]:
            path = contained(self.home, Path(row["path"]))
            current = path.stat().st_size if path.exists() else 0
            growth[row["device"]] = growth.get(row["device"], 0) + max(
                0, current - row["baseline_bytes"]
            )
        wal_growth = max(growth.values(), default=0)
        if wal_growth > plan["wal_growth_budget_bytes"]:
            dimension = "wal_growth"
        now = self.clock.monotonic()
        temporary = 0
        if not preflight and now >= self._next_scan:
            if self._scan is None:
                self._scan = _walk([Path(plan["work"])], volatile=True)
                self._scan_bytes = 0
            # A finite metadata batch also bounds tests using a frozen MockClock.
            for _ in range(128):
                try:
                    _, info = next(self._scan)
                except StopIteration:
                    self._scan = None
                    self._next_scan = self.clock.monotonic() + 1
                    break
                self._scan_bytes += info.st_size
                if self._scan_bytes > plan["temporary_budget_bytes"]:
                    dimension = "temporary_budget"
                    break
            temporary = self._scan_bytes
        if preflight or dimension or now >= self._next_evidence:
            with self._lock():
                evidence = self._read()
                if evidence["state"] == "blocked":
                    dimension = evidence["failure"]["dimension"]
                evidence["peak_temporary_bytes"] = max(evidence["peak_temporary_bytes"], temporary)
                evidence["peak_wal_growth_bytes"] = max(
                    evidence["peak_wal_growth_bytes"], wal_growth
                )
                evidence["observed_volumes"] = observed
                if dimension:
                    evidence["state"] = "blocked"
                    evidence["failure"] = dict(code="space_insufficient", dimension=dimension)
                elif evidence["state"] != "blocked":
                    evidence["state"] = "monitoring"
                self._write(evidence)
            self._next_evidence = now + 1
        if dimension:
            raise SpaceError(dimension)

    async def watch(self, operation, *, clock):
        """门闩释放后监测无写入的 provider 等待；取消仍交原释放协议收尾。"""
        import asyncio

        async def sample():
            running = asyncio.create_task(asyncio.to_thread(self.check, force=True))
            try:
                return await asyncio.shield(running)
            finally:
                # Cancellation is not proof that a metadata worker has exited.
                if not running.done():
                    await asyncio.shield(running)

        async def monitor():
            while True:
                await sample()
                await clock.sleep(0.25)

        task = asyncio.create_task(operation)
        observer = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait({task, observer}, return_when=asyncio.FIRST_COMPLETED)
            if observer in done:
                await observer
            result = await task
            await sample()
            return result
        finally:
            for pending in (task, observer):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, observer, return_exceptions=True)


def space_status(home):
    """管理状态只返回数字、原因码和逻辑操作编号，不暴露候选路径。"""
    root = contained(Path(home), Path(home) / "supervisor/backup-space")
    result = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        contained(Path(home), path)
        value = json.loads(atomic_read(path))
        plan = value["plan"]
        result.append(
            dict(
                operation_id=plan["operation_id"],
                state=value["state"],
                estimated=True,
                guaranteed=False,
                source_bytes=plan["source_bytes"],
                package_bytes=plan["package_bytes"],
                temporary_budget_bytes=plan["temporary_budget_bytes"],
                wal_growth_budget_bytes=plan["wal_growth_budget_bytes"],
                reserve_bytes=plan["reserve_bytes"],
                occupied=plan["occupied"],
                peak_temporary_bytes=value["peak_temporary_bytes"],
                peak_wal_growth_bytes=value["peak_wal_growth_bytes"],
                volumes=value["observed_volumes"],
                failure=value["failure"],
            )
        )
    return result
