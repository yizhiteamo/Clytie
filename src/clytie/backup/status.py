"""认证管理状态的尺寸统计；只看文件元数据，不读取正文或凭据。"""

import os
import stat
from pathlib import Path

from clytie.plugin_host.snapshots import contained


def usage(root, select=None):
    root = Path(root)
    try:
        root.lstat()
    except FileNotFoundError:
        return {"bytes": 0, "files": 0, "unknown": 0}
    except OSError:
        return {"bytes": 0, "files": 0, "unknown": 1}
    result = {"bytes": 0, "files": 0, "unknown": 0}
    pending = [root]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                result["unknown"] += 1
            elif stat.S_ISREG(info.st_mode):
                if select is None or select(path.relative_to(root)):
                    result["bytes"] += info.st_size
                    result["files"] += 1
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
            else:
                result["unknown"] += 1
        except OSError:
            result["unknown"] += 1
    return result


def storage_categories(home):
    home = Path(home).resolve()
    result = {
        label: usage(contained(home, home / relative))
        for label, relative in {
            "temporary_backup_work": "run/backup-work",
            "ordinary_logs": "logs",
        }.items()
    }
    for label, relative in (
        ("plugin_upgrade", "backups/plugin-upgrades"),
        ("ledger", "backups/ledger"),
    ):
        root = contained(home, home / relative)

        def is_failure(path):
            return any(part.startswith("failed-") for part in path.parts)

        result[label + "_failed_sites"] = usage(root, is_failure)
        result[label + "_snapshots"] = usage(root, lambda path: not is_failure(path))
    root = contained(home, home / "backups/memory")
    result["memory_migration_snapshots"] = usage(
        root, lambda path: path.name.startswith("pre-migrate-") and path.suffix == ".enc"
    )
    result["memory_import_snapshots"] = usage(
        root, lambda path: not path.name.startswith("pre-migrate-") and path.suffix == ".enc"
    )
    result["memory_snapshot_metadata"] = usage(root, lambda path: path.suffix != ".enc")
    return result
