"""异盘副本：明确仓库身份、引用保护、核验发布与不重放恢复。"""

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.locking import file_lock
from clytie.plugin_host.snapshots import atomic_write, canonical, contained

from .io_client import heavy_io, io_read, io_write
from .retention import BackupRecord, plan_mirror_retention


class MirrorError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StorageIdentity:
    path: str
    directory_id: str
    volume_id: str
    physical_ids: tuple[str, ...]

    def document(self):
        value = asdict(self)
        value["physical_ids"] = sorted(self.physical_ids)
        return value


def _linux_physical(device):
    node = Path(f"/sys/dev/block/{os.major(device)}:{os.minor(device)}").resolve(strict=True)

    def physical(path, seen):
        if path in seen:
            raise MirrorError("mirror_device_unverified")
        seen = seen | {path}
        slaves = path / "slaves"
        if slaves.is_dir() and list(slaves.iterdir()):
            result = set()
            for slave in slaves.iterdir():
                result.update(physical(slave.resolve(strict=True), seen))
            return result
        if (path / "partition").exists():
            return physical(path.parent, seen)
        if "/virtual/" in str(path) or "virtio" in str(path):
            raise MirrorError("mirror_device_unverified")
        for relative in ("wwid", "device/wwid", "device/serial"):
            identity = path / relative
            if identity.is_file():
                value = identity.read_text().strip()
                if value:
                    return {hashlib.sha256(value.encode()).hexdigest()}
        raise MirrorError("mirror_device_unverified")

    return str(device), tuple(sorted(physical(node, set())))


def _windows_physical(path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    mount, volume = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(1024)
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeNameForVolumeMountPointW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    if not kernel.GetVolumePathNameW(str(path), mount, len(mount)):
        raise MirrorError("mirror_device_unverified")
    if not kernel.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume)):
        raise MirrorError("mirror_device_unverified")
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]

    def query(name, code, data=b""):
        handle = kernel.CreateFileW(name, 0, 7, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            raise MirrorError("mirror_device_unverified")
        try:
            output = ctypes.create_string_buffer(64 * 1024)
            incoming = ctypes.create_string_buffer(data) if data else None
            returned = wintypes.DWORD()
            if not kernel.DeviceIoControl(
                handle, code, incoming, len(data), output, len(output), ctypes.byref(returned), None
            ):
                raise MirrorError("mirror_device_unverified")
            return output.raw[: returned.value]
        finally:
            kernel.CloseHandle(handle)

    extents = query(volume.value.rstrip("\\"), 0x0056_0000)
    count = int.from_bytes(extents[:4], "little")
    if not 0 < count <= 100 or len(extents) < 8 + count * 24:
        raise MirrorError("mirror_device_unverified")
    devices = set()
    for index in range(count):
        number = int.from_bytes(extents[8 + index * 24 : 12 + index * 24], "little")
        descriptor = query(r"\\.\PhysicalDrive" + str(number), 0x002D1400, bytes(12))
        if len(descriptor) < 36:
            raise MirrorError("mirror_device_unverified")
        bus = int.from_bytes(descriptor[28:32], "little")
        if bus not in {1, 3, 7, 11, 17}:
            raise MirrorError("mirror_device_unverified")
        offset = int.from_bytes(descriptor[24:28], "little")
        serial = (
            descriptor[offset:].split(b"\0", 1)[0].strip()
            if 36 <= offset < len(descriptor)
            else b""
        )
        if not serial:
            raise MirrorError("mirror_device_unverified")
        devices.add(hashlib.sha256(serial).hexdigest())
    return volume.value, tuple(sorted(devices))


def probe_storage(path):
    path = Path(path).absolute()
    try:
        contained(Path(path.anchor), path)
        if not path.is_dir():
            raise MirrorError("mirror_unavailable")
        path = path.resolve()
        info = path.stat()
        if sys.platform == "win32":
            volume, devices = _windows_physical(path)
        elif sys.platform.startswith("linux"):
            volume, devices = _linux_physical(info.st_dev)
        else:
            raise MirrorError("mirror_device_unverified")
        return StorageIdentity(str(path), f"{info.st_dev}:{info.st_ino}", volume, devices)
    except MirrorError:
        raise
    except (OSError, ValueError):
        raise MirrorError("mirror_device_unverified") from None


def latest_per_group(records):
    latest = {}
    for record in sorted(records, key=lambda row: (row.completed_at, row.backup_id)):
        if record.verified and record.completeness == "complete":
            latest[record.scope, record.group] = record
    return [latest[key] for key in sorted(latest)]


def _digest(path, check=None, *, reader=None):
    digest, size = hashlib.sha256(), 0
    read = reader or (lambda source, size: source.read(size))
    with Path(path).open("rb") as stream:
        while chunk := read(stream, 1024 * 1024):
            if check:
                check()
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


@heavy_io
def _budget_digest(path, check=None):
    return _digest(path, check, reader=io_read)


@heavy_io
def _copy_pending(source, temporary, *, check, validate, reserve_bytes):
    with source.open("rb") as incoming, temporary.open("xb") as outgoing:
        while chunk := io_read(incoming, 1024 * 1024):
            if check:
                check()
            validate()
            if shutil.disk_usage(temporary.parent).free < len(chunk) + reserve_bytes:
                raise MirrorError("space_insufficient")
            io_write(outgoing, chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def _file_id(path):
    info = Path(path).stat()
    return f"{info.st_dev}:{info.st_ino}"


def publish_no_replace(source, target):
    """原生原子发布，不覆盖已有路径；不依赖目标支持硬链接。"""
    if sys.platform == "win32":
        os.rename(source, target)
        return
    if not sys.platform.startswith("linux"):
        raise MirrorError("mirror_publish_unsupported")
    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise MirrorError("mirror_publish_unsupported")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise MirrorError("mirror_publish_unsupported")
        raise OSError(error, os.strerror(error))
    descriptor = os.open(Path(target).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class MirrorManager:
    """同步 worker 内部服务；主源 verify/reference 均由备份管理层注入。"""

    def __init__(
        self,
        state_dir,
        primary_root,
        clock,
        *,
        owner_id,
        forbidden_roots=(),
        probe=probe_storage,
        reserve_bytes=1024**3,
    ):
        self.directory, self.primary = Path(state_dir).resolve(), Path(primary_root).resolve()
        self.clock, self.owner_id, self.probe = clock, owner_id, probe
        self.forbidden = (
            self.primary,
            self.directory,
            *(Path(p).resolve() for p in forbidden_roots),
        )
        self.reserve_bytes = reserve_bytes
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "mirror.json"

    def _lock(self):
        return file_lock(self.directory / "mirror.lock", {"operation": "backup_mirror"})

    def state(self):
        return (
            json.loads(self.path.read_bytes())
            if self.path.exists()
            else {
                "plans": {},
                "candidate": None,
                "active": None,
                "history": [],
                "operations": {},
                "receipts": {},
            }
        )

    def _save(self, value):
        atomic_write(self.path, canonical(value))

    def _probe(self, path):
        identity = self.probe(Path(path))
        if not identity.physical_ids or not identity.volume_id or not identity.directory_id:
            raise MirrorError("mirror_device_unverified")
        return identity

    def _validate(self, repository):
        path = Path(repository["path"])
        contained(Path(path.anchor), path)
        current, source = self._probe(repository["path"]), self._probe(self.primary)
        if current.document() != repository["identity"]:
            raise MirrorError("mirror_identity_mismatch")
        if set(current.physical_ids) & set(source.physical_ids):
            raise MirrorError("mirror_same_device")
        marker = Path(repository["path"]) / ".clytie-mirror.json"
        if not marker.is_file() or marker.is_symlink():
            raise MirrorError("mirror_identity_mismatch")
        value = json.loads(marker.read_bytes())
        if value != {"version": 1, "repo_id": repository["repo_id"], "owner_id": self.owner_id}:
            raise MirrorError("mirror_identity_mismatch")
        return Path(repository["path"])

    def plan(self, target, records):
        target = Path(target).absolute()
        contained(Path(target.anchor), target)
        target = target.resolve()
        if any(
            target.is_relative_to(root) or root.is_relative_to(target) for root in self.forbidden
        ):
            raise MirrorError("mirror_path_forbidden")
        identity, source = self._probe(target), self._probe(self.primary)
        if set(identity.physical_ids) & set(source.physical_ids):
            raise MirrorError("mirror_same_device")
        marker = target / ".clytie-mirror.json"
        repo_id = uuid4().hex
        if marker.exists():
            if marker.is_symlink():
                raise MirrorError("mirror_identity_mismatch")
            prior = json.loads(marker.read_bytes())
            if (
                prior.get("owner_id") != self.owner_id
                or prior.get("version") != 1
                or not re.fullmatch(r"[a-f0-9]{32}", prior.get("repo_id", ""))
            ):
                raise MirrorError("mirror_identity_mismatch")
            repo_id = prior["repo_id"]
        elif any(target.iterdir()):
            raise MirrorError("mirror_repository_not_empty")
        result = {
            "plan_id": uuid4().hex,
            "path": str(target),
            "identity": identity.document(),
            "repo_id": repo_id,
            "required": [r.backup_id for r in latest_per_group(records)],
        }
        with self._lock():
            state = self.state()
            state["plans"][result["plan_id"]] = result
            self._save(state)
        return result

    def confirm(self, plan_id):
        with self._lock():
            state = self.state()
            plan = state["plans"][plan_id]
            if self._probe(plan["path"]).document() != plan["identity"]:
                raise MirrorError("mirror_identity_mismatch")
            if set(plan["identity"]["physical_ids"]) & set(self._probe(self.primary).physical_ids):
                raise MirrorError("mirror_same_device")
            marker = Path(plan["path"]) / ".clytie-mirror.json"
            if not marker.exists():
                if any(marker.parent.iterdir()):
                    raise MirrorError("mirror_repository_not_empty")
                with marker.open("xb") as stream:
                    stream.write(
                        canonical(
                            {"version": 1, "repo_id": plan["repo_id"], "owner_id": self.owner_id}
                        )
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            self._validate(plan)
            from .repository import SourceRepository

            SourceRepository.initialize(plan["path"], self.clock, owner_instance_id=self.owner_id)
            state["candidate"] = plan
            self._save(state)
            return {"status": "copy_required", "repo_id": plan["repo_id"]}

    def copy_verified(
        self,
        record,
        sha256,
        *,
        reference,
        verify,
        check=None,
        operation_id=None,
        repository_id=None,
    ):
        if (
            not record.verified
            or record.completeness != "complete"
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record.backup_id)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            raise MirrorError("mirror_source_invalid")
        operation_id = operation_id or uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise MirrorError("mirror_operation_invalid")
        with self._lock(), reference(record):
            state = self.state()
            repository = state["candidate"] or state["active"]
            if repository is None:
                return {"status": "failed", "reason": "mirror_unconfigured"}
            if repository_id is not None and repository["repo_id"] != repository_id:
                raise MirrorError("mirror_identity_mismatch")
            root = Path(repository["path"])
            source = contained(self.primary, Path(record.path))
            final = root / (record.backup_id + ".enc")
            temporary = root / (record.backup_id + "." + operation_id + ".pending")
            operation = {
                "id": operation_id,
                "repo": repository,
                "backup_id": record.backup_id,
                "scope": record.scope,
                "group": record.group,
                "completed_at": record.completed_at.isoformat(),
                "manual": record.manual,
                "size": record.size,
                "sha256": sha256,
                "target": str(final),
                "temporary": str(temporary),
                "state": "pending",
                "at": self.clock.now().isoformat(),
            }
            if operation_id in state["operations"]:
                prior = state["operations"][operation_id]
                if (prior["backup_id"], prior["sha256"], prior["repo"]["repo_id"]) != (
                    record.backup_id,
                    sha256,
                    repository["repo_id"],
                ):
                    raise MirrorError("mirror_operation_conflict")
                self._validate(repository)
                if prior["state"] == "committed":
                    if _file_id(final) != prior.get("file_id") or _budget_digest(final, check) != (
                        record.size,
                        sha256,
                    ):
                        raise MirrorError("mirror_verification_failed")
                    return {
                        "status": "complete",
                        "operation_id": operation_id,
                        "repo_id": repository["repo_id"],
                    }
                return {"status": prior["state"], "operation_id": operation_id}
            state["operations"][operation_id] = operation
            self._save(state)
            try:
                self._validate(repository)
                if not verify(source) or _budget_digest(source, check) != (record.size, sha256):
                    raise MirrorError("mirror_source_invalid")
                if shutil.disk_usage(root).free < record.size + self.reserve_bytes:
                    raise MirrorError("space_insufficient")
                if final.exists():
                    prior = state["receipts"].get(repository["repo_id"] + "/" + record.backup_id)
                    if (
                        prior
                        and _file_id(final) == prior["file_id"]
                        and _budget_digest(final, check) == (record.size, sha256)
                    ):
                        operation["file_id"] = prior["file_id"]
                        self._receipt(state, operation)
                        self._save(state)
                        return {
                            "status": "complete",
                            "operation_id": operation_id,
                            "repo_id": repository["repo_id"],
                        }
                    raise MirrorError("mirror_target_exists")
                _copy_pending(
                    source,
                    temporary,
                    check=check,
                    validate=lambda: self._validate(repository),
                    reserve_bytes=self.reserve_bytes,
                )
                if _budget_digest(temporary, check) != (record.size, sha256):
                    raise MirrorError("mirror_verification_failed")
                self._validate(repository)
                operation["state"] = "publishing"
                operation["file_id"] = _file_id(temporary)
                self._save(state)
                publish_no_replace(temporary, final)
                self._receipt(state, operation)
                self._save(state)
                return {
                    "status": "complete",
                    "operation_id": operation_id,
                    "repo_id": repository["repo_id"],
                }
            except Exception as exc:
                operation["reason"] = (
                    exc.code if isinstance(exc, MirrorError) else "mirror_unavailable"
                )
                self._save(state)
                return {
                    "status": "failed",
                    "reason": operation["reason"],
                    "operation_id": operation_id,
                }

    def _receipt(self, state, operation):
        from .repository import SourceRepository, physical_identity

        repository = SourceRepository(operation["repo"]["path"], self.clock)
        repository.register(
            {
                "backup_id": operation["backup_id"],
                "path": operation["target"],
                "identity": physical_identity(operation["target"]),
                "size": operation["size"],
                "sha256": operation["sha256"],
                "scope": operation["scope"],
                "group": operation["group"],
                "completeness": "complete",
                "completed_at": operation.get("completed_at", operation["at"]),
                "manual": operation.get("manual", True),
            }
        )
        operation["state"] = "committed"
        key = operation["repo"]["repo_id"] + "/" + operation["backup_id"]
        state["receipts"][key] = {
            k: operation[k]
            for k in ("backup_id", "scope", "group", "size", "sha256", "target", "at", "file_id")
        }
        state["receipts"][key].update(
            completed_at=operation.get("completed_at", operation["at"]),
            manual=operation.get("manual", True),
        )

    def synchronize(self, records, *, sha256, reference, verify, check=None):
        """重连只补各组最新完整包；所有新候选组验证后才切换 active。"""
        with file_lock(self.directory / "mirror-sync.lock", {"operation": "mirror_sync"}):
            self.recover_pending()
            selected = latest_per_group(records)
            with self._lock():
                state = self.state()
                repository = state["candidate"] or state["active"]
                if repository is None:
                    return {"status": "failed", "reason": "mirror_unconfigured"}
                self._validate(repository)
                repository_id = repository["repo_id"]
                if state["candidate"]:
                    repository["required"] = [row.backup_id for row in selected]
                    self._save(state)
            for record in selected:
                hashed = sha256(record)
                with self._lock():
                    state = self.state()
                    current = state["candidate"] or state["active"]
                    if current is None or current["repo_id"] != repository_id:
                        raise MirrorError("mirror_identity_mismatch")
                    self._validate(current)
                    prior = state["receipts"].get(repository_id + "/" + record.backup_id)
                    if prior:
                        target = contained(Path(current["path"]), Path(prior["target"]))
                        if _file_id(target) != prior["file_id"] or _digest(target, check) != (
                            record.size,
                            hashed,
                        ):
                            raise MirrorError("mirror_verification_failed")
                        continue
                copied = self.copy_verified(
                    record,
                    hashed,
                    reference=reference,
                    verify=verify,
                    check=check,
                    repository_id=repository_id,
                )
                if copied["status"] != "complete":
                    return copied
            with self._lock():
                state = self.state()
                current = state["candidate"] or state["active"]
                if current is None or current["repo_id"] != repository_id:
                    raise MirrorError("mirror_identity_mismatch")
                candidate = state["candidate"] is not None
            return self.activate() if candidate else {"status": "ready", "repo_id": repository_id}

    def status(self):
        state = self.state()
        current = state["candidate"] or state["active"]
        result = {"status": "unconfigured", "bytes": 0, "history_count": len(state["history"])}
        if current is None:
            return result
        result["bytes"] = sum(
            row["size"]
            for key, row in state["receipts"].items()
            if key.startswith(current["repo_id"] + "/")
        )
        result["status"] = "switch_pending" if state["candidate"] else "ready"
        try:
            self._validate(current)
        except (OSError, ValueError) as exc:
            result.update(status="unavailable", reason=getattr(exc, "code", "mirror_unavailable"))
        return result

    def recover_pending(self):
        results = []
        with self._lock():
            state = self.state()
            for operation in state["operations"].values():
                if operation["state"] not in {"pending", "publishing"}:
                    continue
                try:
                    self._validate(operation["repo"])
                    target = contained(Path(operation["repo"]["path"]), Path(operation["target"]))
                    if operation["state"] != "publishing" or not target.exists():
                        operation["state"] = "interrupted"
                    elif _file_id(target) == operation.get("file_id") and _digest(target) == (
                        operation["size"],
                        operation["sha256"],
                    ):
                        self._receipt(state, operation)
                    else:
                        operation["state"] = "recovery_blocked"
                except (OSError, ValueError):
                    operation["state"] = "recovery_blocked"
                results.append({"operation_id": operation["id"], "status": operation["state"]})
            for deletion in state.get("deletions", []):
                if deletion["state"] != "deleting":
                    continue
                try:
                    from .repository import SourceRepository

                    root = self._validate(deletion["repo"])
                    target = contained(root, Path(deletion["target"]))
                    if target.exists():
                        deletion["state"] = "interrupted"
                    else:
                        SourceRepository(root, self.clock).remove_record(deletion["backup_id"])
                        key = deletion["repo"]["repo_id"] + "/" + deletion["backup_id"]
                        state["receipts"].pop(key, None)
                        deletion["state"] = "deleted"
                except (OSError, ValueError, KeyError):
                    deletion["state"] = "recovery_blocked"
            self._save(state)
        return results

    def activate(self):
        with self._lock():
            state = self.state()
            candidate = state["candidate"]
            if candidate is None:
                raise MirrorError("mirror_unconfigured")
            self._validate(candidate)
            if not candidate["required"] or any(
                candidate["repo_id"] + "/" + key not in state["receipts"]
                for key in candidate["required"]
            ):
                return {"status": "copy_required"}
            for key in candidate["required"]:
                receipt = state["receipts"][candidate["repo_id"] + "/" + key]
                target = contained(Path(candidate["path"]), Path(receipt["target"]))
                if _file_id(target) != receipt["file_id"] or _digest(target) != (
                    receipt["size"],
                    receipt["sha256"],
                ):
                    raise MirrorError("mirror_verification_failed")
            if state["active"] and state["active"]["repo_id"] != candidate["repo_id"]:
                state["history"].append(state["active"])
            state["active"], state["candidate"] = candidate, None
            self._save(state)
            return {"status": "ready", "repo_id": candidate["repo_id"]}

    def retention(self, records, **options):
        state = self.state()
        active = state["active"]
        if active is None:
            raise MirrorError("mirror_unconfigured")
        self._validate(active)
        receipted = []
        for record in records:
            receipt = state["receipts"].get(active["repo_id"] + "/" + record.backup_id)
            if receipt is None:
                continue
            target = contained(Path(active["path"]), Path(receipt["target"]))
            if _file_id(target) != receipt["file_id"] or (record.scope, record.group) != (
                receipt["scope"],
                receipt["group"],
            ):
                raise MirrorError("mirror_identity_mismatch")
            if _digest(target) != (receipt["size"], receipt["sha256"]):
                raise MirrorError("mirror_verification_failed")
            receipted.append(replace(record, path=str(target), size=receipt["size"], owned=True))
        return plan_mirror_retention(receipted, now=self.clock.now(), **options)

    def rotate(self, **options):
        """仅 active 仓库的独立回执；主包已淘汰也不丢副本保留元数据。"""
        from .repository import SourceRepository

        with self._lock():
            state = self.state()
            active = state["active"]
            if active is None or state["candidate"] is not None:
                return "skipped"
            root = self._validate(active)
            repository = SourceRepository(root, self.clock)
            catalog = repository.catalog(self.owner_id)
            receipts = {
                key: row
                for key, row in state["receipts"].items()
                if key.startswith(active["repo_id"] + "/")
            }
            records = [
                BackupRecord(
                    row["backup_id"],
                    row["target"],
                    row["scope"],
                    row["group"],
                    datetime.fromisoformat(row.get("completed_at", row["at"])),
                    row["size"],
                    manual=row.get("manual", True),
                    referenced=bool(catalog.holders(row["target"])),
                )
                for row in receipts.values()
            ]
            plan = plan_mirror_retention(records, now=self.clock.now(), **options)
            unreferenced = plan_mirror_retention(
                [replace(row, referenced=False) for row in records], now=self.clock.now(), **options
            )
            blocked = set(unreferenced.delete) - set(plan.delete)
            for name in plan.delete:
                key, receipt = next((k, r) for k, r in receipts.items() if r["target"] == name)
                with repository.deletion(name) as allowed:
                    if not allowed:
                        blocked.add(name)
                        continue
                    self._validate(active)
                    target = contained(root, Path(name))
                    if _file_id(target) != receipt["file_id"] or _digest(target) != (
                        receipt["size"],
                        receipt["sha256"],
                    ):
                        raise MirrorError("mirror_verification_failed")
                    deletion = {
                        "repo": active,
                        "target": name,
                        "backup_id": receipt["backup_id"],
                        "state": "deleting",
                        "at": self.clock.now().isoformat(),
                    }
                    state.setdefault("deletions", []).append(deletion)
                    self._save(state)
                    target.unlink()
                    repository.remove_record(receipt["backup_id"])
                    deletion["state"] = "deleted"
                    del state["receipts"][key]
                    self._save(state)
            return "retention_protected" if blocked else "ok"
