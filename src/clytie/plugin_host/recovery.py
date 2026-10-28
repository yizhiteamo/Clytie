"""保留故障目录的恢复与显式确认清理。所有位置都在指定实例内。"""

import base64
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .snapshots import SnapshotError, atomic_write, canonical, contained


class RecoveryError(SnapshotError):
    def __init__(self, locations):
        self.locations = locations
        super().__init__("recovery stopped; directories preserved: " + json.dumps(locations))


def restore_preserving(manager, snapshot, companion_id, plugin_id):
    payload = manager.read(snapshot, companion_id, plugin_id)
    destination = manager.state_path(companion_id, plugin_id)
    archive = contained(manager.home, Path(snapshot).parent)
    failed = contained(archive, archive / ("failed-" + Path(snapshot).stem + "-" + uuid4().hex))
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
        contained(manager.home, temporary)
        for name in payload["directories"]:
            contained(temporary, temporary / name).mkdir(parents=True, exist_ok=True)
        for name, entry in payload["files"].items():
            atomic_write(contained(temporary, temporary / name), base64.b64decode(entry["data"]))
        if manager._tree(temporary) != payload:
            raise SnapshotError("restored private state verification failed")
        # 不跟随故障目录中的链接；目录整体保留，不读取或删除其内容。
        contained(manager.home, destination)
        if destination.exists():
            destination.rename(failed)
        contained(manager.home, temporary).rename(destination)
    except Exception as exc:
        positions = {
            "state": str(destination.relative_to(manager.home)),
            "failed": str(failed.relative_to(manager.home)),
            "temporary": str(temporary.relative_to(manager.home)) if temporary else None,
        }
        raise RecoveryError(positions) from exc
    manager.log.info("plugin.snapshot_restored", plugin_id=plugin_id, companion_id=companion_id)
    return failed if failed.exists() else None


def inventory(directory):
    """不跟随链接，且把内容与目录身份纳入确认计划。"""
    contained(directory.parent, directory)
    entries = []
    for file in [directory, *sorted(directory.rglob("*"))]:
        contained(directory, file)
        if not file.is_file() and not file.is_dir():
            raise SnapshotError("unsupported failed-state entry")
        info = file.stat()
        entries.append(
            {
                "path": file.relative_to(directory).as_posix(),
                "directory": file.is_dir(),
                "size": info.st_size if file.is_file() else 0,
                "mtime": info.st_mtime_ns,
                "digest": hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None,
            }
        )
    return entries


def failed_status(home):
    root = Path(home).resolve()
    base = contained(root, root / "backups" / "plugin-upgrades")
    result = []
    for path in sorted(base.glob("*/*/failed-*")):
        resolved = []
        try:
            contained(root, path)
            if not path.is_dir():
                continue
            resolved_file = contained(root, path.parent / "resolved.json")
            resolved = json.loads(resolved_file.read_bytes()) if resolved_file.exists() else []
            entries = inventory(path)
            size = sum(item["size"] for item in entries)
            count = sum(not item["directory"] for item in entries)
            error = None
        except (OSError, ValueError):
            size, count, error = None, None, "unsafe_entry; manual inspection required"
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": size,
                "files": count,
                "resolved": path.name in resolved,
                "error": error,
            }
        )
    return {
        "directories": result,
        "total_bytes": sum(r["bytes"] or 0 for r in result),
        "size_complete": all(r["bytes"] is not None for r in result),
    }


@dataclass(frozen=True)
class CleanupDirectory:
    path: str
    files: int
    bytes: int


@dataclass(frozen=True)
class CleanupPlan:
    plan_id: str
    directories: tuple[str, ...]
    files: int
    bytes: int
    details: tuple[CleanupDirectory, ...]


class Cleanup:
    def __init__(self, home, log):
        self.home, self.log, self.plans = Path(home).resolve(), log, {}

    def _inventory(self, directories):
        result = {}
        for name in directories:
            path = contained(self.home, self.home / name)
            relative = path.relative_to(self.home)
            if (
                len(relative.parts) != 5
                or relative.parts[:2] != ("backups", "plugin-upgrades")
                or not path.name.startswith("failed-")
                or not path.is_dir()
            ):
                raise SnapshotError("only retained failed directories can be cleaned")
            result[name] = inventory(path)
        return result

    def plan(self, directories):
        names = tuple(sorted(set(directories)))
        data = self._inventory(names)
        plan_id = hashlib.sha256(canonical(data)).hexdigest()
        entries = [entry for values in data.values() for entry in values]
        plan = CleanupPlan(
            plan_id,
            names,
            sum(not e["directory"] for e in entries),
            sum(e["size"] for e in entries),
            tuple(
                CleanupDirectory(
                    name, sum(not e["directory"] for e in values), sum(e["size"] for e in values)
                )
                for name, values in data.items()
            ),
        )
        self.plans[plan_id] = plan, data
        return plan

    def apply(self, plan_id):
        from .host import PlanStale

        if plan_id not in self.plans:
            raise PlanStale("cleanup plan unknown; regenerate and confirm")
        plan, data = self.plans[plan_id]
        if self._inventory(plan.directories) != data:
            raise PlanStale("cleanup contents changed; regenerate and confirm")
        self.plans.pop(plan_id)
        for name in plan.directories:
            directory = contained(self.home, self.home / name)
            expected = {e["path"]: e for e in data[name]}
            for entry in sorted(data[name], key=lambda e: len(Path(e["path"]).parts), reverse=True):
                # 每次动作前检查尚存集合，拒绝执行期间出现的计划外文件或替换。
                remaining = inventory(directory)
                if {e["path"] for e in remaining} != set(expected):
                    raise PlanStale("unexpected cleanup entry; stopped")
                for current in remaining:
                    old = expected[current["path"]]
                    if not current["directory"] and current != old:
                        raise PlanStale("cleanup file changed; stopped")
                path = contained(directory, directory / entry["path"])
                if entry["directory"]:
                    path.rmdir()
                else:
                    path.unlink()
                expected.pop(entry["path"])
                self.log.info(
                    "plugin.failed_state_removed", path=path.relative_to(self.home).as_posix()
                )
        return plan.directories
