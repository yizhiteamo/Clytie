"""插件升级快照：认证加密、写后校验、仅恢复本插件目录。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SnapshotError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
        raise ValueError("invalid companion or plugin identifier")
    return value


def contained(root, path):
    root, path = Path(root).resolve(), Path(path)
    if not path.resolve().is_relative_to(root):
        raise SnapshotError("path escapes permitted directory")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise SnapshotError("symlinks are not supported in private state")
    return path


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class UpgradeSnapshots:
    def __init__(self, home, clock, log):
        self.home = Path(home).resolve()
        self.clock, self.log = clock, log

    def _key(self, *, create=False):
        path = contained(self.home, self.home / "secrets" / "backup.key")
        if not path.exists() and create:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(AESGCM.generate_key(bit_length=256))
                    output.flush()
                    os.fsync(output.fileno())
                self.log.warning(
                    "backup.key_created_save_separately_loss_prevents_recovery",
                    fingerprint=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
        try:
            value = path.read_bytes()
        except OSError as exc:
            raise SnapshotError("backup key unavailable") from exc
        if len(value) != 32:
            raise SnapshotError("backup key must contain 32 bytes; it was not replaced")
        return value

    def state_path(self, companion_id, plugin_id):
        return contained(
            self.home,
            self.home
            / "companions"
            / identifier(companion_id)
            / "plugin-data"
            / identifier(plugin_id),
        )

    def _tree(self, directory):
        files, directories = {}, []
        for path in sorted(directory.rglob("*")):
            contained(directory, path)
            name = path.relative_to(directory).as_posix()
            if path.is_dir():
                directories.append(name)
            elif path.is_file():
                content = path.read_bytes()
                files[name] = {
                    "data": base64.b64encode(content).decode("ascii"),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            else:
                raise SnapshotError("unsupported private state entry")
        return {"files": files, "directories": directories}

    def create(self, companion_id, plugin_id, old_version, new_version):
        directory = self.state_path(companion_id, plugin_id)
        directory.mkdir(parents=True, exist_ok=True)
        value = self._key(create=True)
        header = {
            "format_version": 1,
            "algorithm": "AES-256-GCM",
            "fingerprint": hashlib.sha256(value).hexdigest(),
            "companion_id": companion_id,
            "plugin_id": plugin_id,
            "from": old_version,
            "to": new_version,
        }
        payload = self._tree(directory)
        nonce = os.urandom(12)
        encrypted = AESGCM(value).encrypt(nonce, canonical(payload), canonical(header))
        envelope = {
            "header": header,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(encrypted).decode("ascii"),
        }
        timestamp = self.clock.now().strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{old_version}-to-{new_version}-{timestamp}.enc"
        if not re.fullmatch(r"[A-Za-z0-9.+_-]+\.enc", filename):
            raise SnapshotError("invalid snapshot version")
        path = contained(
            self.home,
            self.home / "backups" / "plugin-upgrades" / companion_id / plugin_id / filename,
        )
        if path.exists():
            raise SnapshotError("snapshot timestamp collision; existing snapshot preserved")
        atomic_write(path, canonical(envelope))
        if self.read(path, companion_id, plugin_id) != payload:
            raise SnapshotError("snapshot verification failed")
        self.log.info("plugin.snapshot_verified", plugin_id=plugin_id, companion_id=companion_id)
        snapshots = sorted(path.parent.glob("*.enc"), key=lambda p: p.name.rsplit("-", 1)[-1])
        for previous in snapshots[:-3]:
            contained(path.parent, previous).unlink()
            self.log.info("plugin.snapshot_removed", path=str(previous.relative_to(self.home)))
        return path

    def read(self, path, companion_id, plugin_id):
        value = self._key()
        try:
            envelope = json.loads(contained(self.home, path).read_bytes())
            header = envelope["header"]
            if header["fingerprint"] != hashlib.sha256(value).hexdigest():
                raise SnapshotError("wrong backup key fingerprint")
            if header["format_version"] != 1 or header["algorithm"] != "AES-256-GCM":
                raise SnapshotError("unsupported encrypted snapshot format")
            if header["companion_id"] != companion_id or header["plugin_id"] != plugin_id:
                raise SnapshotError("snapshot belongs to another companion or plugin")
            payload = json.loads(
                AESGCM(value).decrypt(
                    base64.b64decode(envelope["nonce"], validate=True),
                    base64.b64decode(envelope["ciphertext"], validate=True),
                    canonical(header),
                )
            )
            for name in [*payload["files"], *payload["directories"]]:
                part = PurePosixPath(name)
                if part.is_absolute() or ".." in part.parts or "\\" in name or ":" in name:
                    raise SnapshotError("invalid path inside snapshot")
            for entry in payload["files"].values():
                content = base64.b64decode(entry["data"], validate=True)
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise SnapshotError("snapshot content hash mismatch")
            return payload
        except InvalidTag as exc:
            raise SnapshotError(
                "snapshot authentication failed; wrong key or corrupt data"
            ) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, SnapshotError):
                raise
            raise SnapshotError("invalid or unreadable encrypted snapshot") from exc

    def restore(self, path, companion_id, plugin_id):
        # 完整解密和校验成功之前不改动目标；每项删除都限制到本插件目录并记录。
        payload = self.read(path, companion_id, plugin_id)
        directory = self.state_path(companion_id, plugin_id)
        current = list(directory.rglob("*"))
        for file in current:
            contained(directory, file)
        for file in sorted(current, key=lambda p: len(p.parts), reverse=True):
            name = file.relative_to(directory).as_posix()
            if file.is_file() and (name not in payload["files"]):
                file.unlink()
                self.log.info("plugin.restore_removed_file", path=name)
            elif file.is_dir() and name not in payload["directories"]:
                file.rmdir()
                self.log.info("plugin.restore_removed_directory", path=name)
        for name in payload["directories"]:
            contained(directory, directory / name).mkdir(parents=True, exist_ok=True)
        for name, entry in payload["files"].items():
            atomic_write(contained(directory, directory / name), base64.b64decode(entry["data"]))
        if self._tree(directory) != payload:
            raise SnapshotError("restored private state verification failed")
        self.log.info("plugin.snapshot_restored", plugin_id=plugin_id, companion_id=companion_id)
