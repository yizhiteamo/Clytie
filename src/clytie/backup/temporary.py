"""明确登记的工作文件清理；恢复不枚举目录取得删除权。"""

from pathlib import Path

from clytie.plugin_host.snapshots import atomic_write, canonical, contained
from clytie.supervisor.common import read

from .restore import budget_digest, digest, identity


class TemporaryArtifacts:
    def __init__(self, home, operation_id):
        from .journal import BackupJournal

        self.home = Path(home).resolve()
        BackupJournal.path_for(self.home, operation_id)
        self.root = contained(self.home, self.home / "run/backup-work" / operation_id)
        self.path = contained(
            self.home, self.home / "supervisor/backup-temporary" / (operation_id + ".json")
        )

    def state(self):
        return read(self.path, {"state": "retained", "files": {}, "directories": {}})

    def begin(self):
        if not self.path.exists():
            self._save(self.state())

    def _save(self, state):
        # A successful unlink must be followed by a durable receipt even when a
        # Windows reader briefly holds the previous receipt without delete sharing.
        # Only the atomic receipt replacement retries; file deletion never does.
        contained(self.home, self.path)
        atomic_write(self.path, canonical(state), retry_sharing=True)

    def register(self, roots, *, budgeted=False, check=None):
        """登记已写完的明确材料；仅可取消的提交前调用显式启用摘要预算。"""

        def checkpoint():
            if check:
                check()

        checkpoint()
        state = self.state()
        for root in roots:
            checkpoint()
            root = contained(self.root, Path(root))
            for path in [root, *sorted(root.rglob("*"))]:
                checkpoint()
                contained(self.root, path)
                entry = {"identity": identity(path), "state": "retained"}
                if path.is_file():
                    size = path.stat().st_size
                    hashed = budget_digest(path, check=check) if budgeted else digest(path)
                    checkpoint()
                    entry.update(size=size, sha256=hashed)
                    state["files"][str(path)] = entry
                elif path.is_dir():
                    state["directories"][str(path)] = entry
                else:
                    raise ValueError("temporary_identity_mismatch")
            for parent in [root.parent, *root.parents]:
                checkpoint()
                if parent == self.root or parent.is_relative_to(self.root):
                    state["directories"][str(parent)] = {
                        "identity": identity(parent),
                        "state": "retained",
                    }
        checkpoint()
        self._save(state)

    def cleanup(self):
        """调用方已证明主包/验证完成；失败及身份不符保留现场，不重建。"""
        state = self.state()
        state["state"] = "cleaning"
        self._save(state)
        stage = "validate_file"
        try:
            for name, entry in state["files"].items():
                if entry["state"] == "deleted":
                    continue
                stage = "validate_file"
                path = contained(self.root, Path(name))
                if entry["state"] == "deleting" and not path.exists():
                    entry["state"] = "deleted"
                    stage = "record_file_deleted"
                    self._save(state)
                    continue
                if (
                    identity(path) != entry["identity"]
                    or path.stat().st_size != entry["size"]
                    or digest(path) != entry["sha256"]
                ):
                    raise ValueError("temporary_identity_mismatch")
                entry["state"] = "deleting"
                stage = "record_file_deleting"
                self._save(state)
                stage = "delete_file"
                path.unlink()
                entry["state"] = "deleted"
                stage = "record_file_deleted"
                self._save(state)
            for name in sorted(
                state["directories"], key=lambda p: len(Path(p).parts), reverse=True
            ):
                entry = state["directories"][name]
                if entry["state"] == "deleted":
                    continue
                stage = "validate_directory"
                path = contained(self.root, Path(name))
                if entry["state"] == "deleting" and not path.exists():
                    entry["state"] = "deleted"
                    stage = "record_directory_deleted"
                    self._save(state)
                    continue
                if identity(path) != entry["identity"]:
                    raise ValueError("temporary_identity_mismatch")
                entry["state"] = "deleting"
                stage = "record_directory_deleting"
                self._save(state)
                stage = "delete_directory"
                path.rmdir()  # 未登记新文件使 rmdir 失败，绝不扫描后删除。
                entry["state"] = "deleted"
                stage = "record_directory_deleted"
                self._save(state)
            state["state"] = "cleaned"
        except (OSError, ValueError) as exc:
            state["state"] = "cleanup_blocked"
            state.setdefault("failures", []).append(
                {
                    "code": "temporary_io_error"
                    if isinstance(exc, OSError)
                    else "temporary_validation_failed",
                    "stage": stage,
                    "errno": getattr(exc, "errno", None),
                    "winerror": getattr(exc, "winerror", None),
                }
            )
        self._save(state)
        return state["state"]


def temporary_status(home):
    root = contained(Path(home), Path(home) / "supervisor/backup-temporary")
    values = [read(p, {}) for p in root.glob("*.json")] if root.exists() else []
    return {
        "retained_operations": sum(s.get("state") != "cleaned" for s in values),
        "registered_bytes": sum(
            e["size"]
            for s in values
            for e in s.get("files", {}).values()
            if e["state"] != "deleted"
        ),
        "unverified_operations": sum(s.get("state") == "retained" for s in values),
    }
