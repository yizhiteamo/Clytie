"""内容先发布、归属后提交；哈希从来不是授权凭证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from clytie.backup.coordination import CaptureCoordinator, companion_coordinator
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained, identifier
from clytie.sdk.types import AssetRef


class AssetError(ValueError):
    """稳定错误码；不返回来源文本或私有路径。"""


_model_grant = ContextVar("selected_inbound_images", default=None)


@contextmanager
def model_image_access(cid, pid, selected, *, appearance=None):
    # Clear the shared set on exit: child tasks inherit the ContextVar value.
    ids = {ref.asset_id: ref for ref in selected}
    pinned = {
        ref.asset_id: appearance[ref.asset_id]
        for ref in selected
        if appearance and ref.origin == "appearance" and ref.asset_id in appearance
    }
    token = _model_grant.set((cid, pid, ids, pinned))
    try:
        yield
    finally:
        ids.clear()
        pinned.clear()
        _model_grant.reset(token)


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _regular(path):
    value = path.lstat()
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or (
            getattr(value, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    ):
        raise AssetError("asset_unsafe_file")
    return value


@dataclass(frozen=True)
class AssetRegistration:
    companion_id: str
    asset_id: str
    size: int
    media_type: str
    origin: str
    path: str
    shared_with: tuple[str, ...] = ()
    inbound_only: bool = False
    appearance_only: bool = False

    @property
    def restricted(self):
        return self.inbound_only or self.appearance_only

    def as_dict(self):
        value = {**self.__dict__, "shared_with": list(self.shared_with)}
        if not self.inbound_only:
            del value["inbound_only"]
        if not self.appearance_only:
            del value["appearance_only"]
        return value

    def ref(self):
        return AssetRef(
            asset_id=self.asset_id, size=self.size, media_type=self.media_type, origin=self.origin
        )


@dataclass(frozen=True)
class AssetSnapshot:
    revision: str
    registrations: tuple[AssetRegistration, ...]

    def as_dict(self):
        return {
            "format": 1,
            "revision": self.revision,
            "registrations": [row.as_dict() for row in self.registrations],
        }


@dataclass(frozen=True)
class ResolvedAsset:
    revision: str
    companion_id: str
    registration: AssetRegistration
    path: Path
    identity: tuple[int, ...]


def validate_registry(value):
    """校验持久格式及授权/路径；不读取文件，供捕获及恢复使用。"""
    try:
        if set(value) != {"format", "revision", "registrations"} or value["format"] != 1:
            raise ValueError
        revision = value["revision"]
        if not isinstance(revision, str) or not re.fullmatch(r"0|[0-9a-f]{32}", revision):
            raise ValueError
        if not isinstance(value["registrations"], list):
            raise ValueError
        rows, keys, access = [], set(), {}
        for raw in value["registrations"]:
            if set(raw) - {"inbound_only", "appearance_only"} != {
                "companion_id",
                "asset_id",
                "size",
                "media_type",
                "origin",
                "path",
                "shared_with",
            }:
                raise ValueError
            cid = identifier(raw["companion_id"])
            ref = AssetRef.model_validate(
                {key: raw[key] for key in ("asset_id", "size", "media_type", "origin")}
            )
            path = raw["path"]
            if not isinstance(path, str) or "\\" in path or ":" in path:
                raise ValueError
            parts = PurePosixPath(path).parts
            if not parts or any(part in {".", ".."} for part in path.split("/")):
                raise ValueError
            if str(PurePosixPath(path)) != path or PurePosixPath(path).is_absolute():
                raise ValueError
            shared_path = f"shared/assets/objects/{ref.asset_id}"
            if path != shared_path and not (
                len(parts) >= 4 and parts[:3] == ("companions", cid, "assets")
            ):
                raise ValueError
            shares = raw["shared_with"]
            if not isinstance(shares, list) or len(set(shares)) != len(shares):
                raise ValueError
            shares = tuple(sorted(identifier(other) for other in shares))
            if cid in shares:
                raise ValueError
            key = (cid, ref.asset_id)
            if key in keys:
                raise ValueError
            keys.add(key)
            restricted = raw.get("inbound_only", False)
            appearance = raw.get("appearance_only", False)
            if (
                type(restricted) is not bool
                or type(appearance) is not bool
                or (restricted or appearance)
                and shares
                or restricted
                and appearance
            ):
                raise ValueError
            row = AssetRegistration(
                cid,
                ref.asset_id,
                ref.size,
                ref.media_type,
                ref.origin,
                path,
                shares,
                restricted,
                appearance,
            )
            # 同一接收者不能遇到两个不同来源候选，连相同字节也不猜来源。
            for recipient in (cid, *shares):
                access_key = recipient, ref.asset_id
                if access_key in access:
                    raise ValueError
                access[access_key] = row
            rows.append(row)
        # 大小写不敏感平台也不能出现两个不同恢复目标。
        paths = {}
        for row in rows:
            key = row.path.casefold()
            if key in paths and (
                paths[key].path != row.path or paths[key].asset_id != row.asset_id
            ):
                raise ValueError
            paths[key] = row
        return AssetSnapshot(revision, tuple(rows))
    except (KeyError, TypeError, ValueError) as exc:
        raise AssetError("asset_registry_invalid") from exc


class AssetStore:
    def __init__(self, home, clock):
        self.home, self.clock = Path(home).absolute(), clock
        self.root = self._safe("shared/assets")
        self.registry_path = self._safe("shared/assets/registry.json")

    def _safe(self, relative):
        try:
            return contained(self.home, self.home / relative)
        except (ValueError, OSError) as exc:
            raise AssetError("asset_unsafe_path") from exc

    def _lock(self, name):
        return CaptureCoordinator(self._safe(f"shared/assets/{name}.lock"), self.clock)

    def bind(self, companion_id, plugin_id=None):
        return BoundAssets(self, identifier(companion_id), plugin_id)

    def snapshot(self):
        path = self._safe("shared/assets/registry.json")
        if not path.exists():
            return AssetSnapshot("0", ())
        try:
            _regular(path)
            return validate_registry(json.loads(atomic_read(path)))
        except (ValueError, OSError) as exc:
            raise AssetError("asset_registry_invalid") from exc

    def resolve(self, snapshot, companion_id, asset_id):
        identifier(companion_id)
        rows = [
            row
            for row in snapshot.registrations
            if row.asset_id == asset_id
            and (row.companion_id == companion_id or companion_id in row.shared_with)
        ]
        if not rows:
            raise KeyError(asset_id)
        if len(rows) != 1:
            raise AssetError("asset_ambiguous")
        row = rows[0]
        try:
            path = self._safe(row.path)
            identity = _identity(_regular(path))
        except OSError as exc:
            raise AssetError("asset_missing") from exc
        return ResolvedAsset(snapshot.revision, companion_id, row, path, identity)

    def read_resolved(self, resolved):
        return b"".join(self.iter_resolved(resolved))

    def iter_resolved(self, resolved, *, check=None, reader=None):
        """固定授权、路径及身份；捕获后无需查最新登记，也不按哈希找替代文件。"""
        row = resolved.registration
        if resolved.companion_id not in (row.companion_id, *row.shared_with):
            raise AssetError("asset_unauthorized")
        try:
            path = self._safe(row.path)
            if path != resolved.path or _identity(_regular(path)) != resolved.identity:
                raise AssetError("asset_identity_changed")
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(descriptor, "rb") as stream:
                if _identity(os.fstat(stream.fileno())) != resolved.identity:
                    raise AssetError("asset_identity_changed")
                digest, size = hashlib.sha256(), 0
                read = reader or (lambda source, size: source.read(size))
                while chunk := read(stream, 1024 * 1024):
                    if check is not None:
                        check()
                    digest.update(chunk)
                    size += len(chunk)
                    yield chunk
                if _identity(os.fstat(stream.fileno())) != resolved.identity:
                    raise AssetError("asset_identity_changed")
            if _identity(_regular(self._safe(row.path))) != resolved.identity:
                raise AssetError("asset_identity_changed")
            if size != row.size or digest.hexdigest() != row.asset_id:
                raise AssetError("asset_corrupt")
        except OSError as exc:
            raise AssetError("asset_unavailable") from exc

    def verify_resolved(self, resolved):
        for _ in self.iter_resolved(resolved):
            pass

    def copy_resolved(self, resolved, target, *, check=None, reader=None, writer=None):
        """流式写独占目标；调用方在失败时保留/登记未完成文件，不把它当成功成员。"""
        with Path(target).open("xb") as stream:
            write = writer or (lambda destination, data: destination.write(data))
            for chunk in self.iter_resolved(resolved, check=check, reader=reader):
                write(stream, chunk)
            stream.flush()
            os.fsync(stream.fileno())

    def check_identity(self, resolved):
        try:
            if _identity(_regular(self._safe(resolved.registration.path))) != resolved.identity:
                raise AssetError("asset_identity_changed")
        except OSError as exc:
            raise AssetError("asset_unavailable") from exc

    def _save(self, rows):
        value = AssetSnapshot(uuid.uuid4().hex, tuple(rows)).as_dict()
        validate_registry(value)
        atomic_write(
            self._safe("shared/assets/registry.json"), canonical(value), retry_sharing=True
        )

    def _prepare_registration(self, row):
        snapshot = self.snapshot()
        existing = next(
            (
                old
                for old in snapshot.registrations
                if (old.companion_id, old.asset_id) == (row.companion_id, row.asset_id)
            ),
            None,
        )
        row = existing or row
        resolved = self.resolve(
            AssetSnapshot(snapshot.revision, (row,)), row.companion_id, row.asset_id
        )
        self.verify_resolved(resolved)
        return resolved, existing

    def _register(self, resolved, expected):
        row = resolved.registration
        with self._lock("registry").commit():
            snapshot = self.snapshot()
            previous = next(
                (
                    old
                    for old in snapshot.registrations
                    if (old.companion_id, old.asset_id) == (row.companion_id, row.asset_id)
                ),
                None,
            )
            if previous != expected:
                raise AssetError("asset_registration_changed")
            self.check_identity(resolved)
            if previous is not None:
                return previous.ref()
            self._save((*snapshot.registrations, row))
        return row.ref()

    def release_appearance(self, cid, asset_id):
        # Independent inbound and product registrations survive removal from the list.
        with self._lock("registry").commit():
            snapshot = self.snapshot()
            keep = tuple(
                r
                for r in snapshot.registrations
                if not (r.companion_id == cid and r.asset_id == asset_id and r.appearance_only)
            )
            if len(keep) != len(snapshot.registrations):
                self._save(keep)

    def register_private(self, companion_id, ref, relative_path):
        """只供可信本体登记已知私有位置；不发现文件，也不搬动文件。"""
        cid = identifier(companion_id)
        ref = AssetRef.model_validate(ref)
        row = AssetRegistration(
            cid, ref.asset_id, ref.size, ref.media_type, ref.origin, relative_path
        )
        snapshot = validate_registry(AssetSnapshot("0", (row,)).as_dict())
        if not relative_path.startswith(f"companions/{cid}/assets/"):
            raise AssetError("asset_private_path_required")
        self.verify_resolved(self.resolve(snapshot, cid, ref.asset_id))
        resolved, expected = self._prepare_registration(row)
        with companion_coordinator(self.home, cid, self.clock).commit():
            return self._register(resolved, expected)

    def share(self, owner_id, asset_id, recipients):
        """内部明确授权入口，永不暴露给插件。"""
        owner_id = identifier(owner_id)
        recipients = tuple(sorted(set(identifier(cid) for cid in recipients)))
        snapshot = self.snapshot()
        row = next(
            (
                row
                for row in snapshot.registrations
                if row.companion_id == owner_id and row.asset_id == asset_id
            ),
            None,
        )
        if row is None:
            raise KeyError(asset_id)
        if row.restricted:
            raise AssetError("asset_unauthorized")
        resolved = self.resolve(snapshot, owner_id, asset_id)
        self.verify_resolved(resolved)
        with ExitStack() as stack:
            for cid in sorted(set((owner_id, *recipients))):
                stack.enter_context(companion_coordinator(self.home, cid, self.clock).commit())
            stack.enter_context(self._lock("registry").commit())
            snapshot = self.snapshot()
            current = next(
                (
                    row
                    for row in snapshot.registrations
                    if row.companion_id == owner_id and row.asset_id == asset_id
                ),
                None,
            )
            if current != row:
                raise AssetError("asset_registration_changed")
            self.check_identity(resolved)
            updated = replace(row, shared_with=tuple(sorted(set(row.shared_with + recipients))))
            self._save(tuple(updated if old == row else old for old in snapshot.registrations))

    def _publish(self, data, ref):
        path = self._safe(f"shared/assets/objects/{ref.asset_id}")
        row = AssetRegistration(
            "owner",
            ref.asset_id,
            ref.size,
            ref.media_type,
            ref.origin,
            path.relative_to(self.home).as_posix(),
        )
        with self._lock(f"object-{ref.asset_id}").commit():
            if path.exists():
                self.verify_resolved(
                    self.resolve(AssetSnapshot("0", (row,)), "owner", ref.asset_id)
                )
                return row.path
            pending = self._safe(f"shared/assets/pending/{uuid.uuid4().hex}.pending")
            pending.parent.mkdir(parents=True, exist_ok=True)
            with pending.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # 受控文件重新读验；中断留下的 pending 与 object 都可明确定位。
            digest, size = hashlib.sha256(), 0
            with pending.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != ref.asset_id or size != ref.size:
                raise AssetError("asset_corrupt")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._safe(row.path)
            # link+unlink 保证目标不存在才发布；成功之后唯一 inode 引用为对象路径。
            os.link(pending, path)
            pending.unlink()
            path.chmod(0o444)
            return row.path

    def _stage_put(
        self, companion_id, data, media_type, origin, *, inbound_only=False, appearance_only=False
    ):
        cid = identifier(companion_id)
        data = bytes(data)
        ref = AssetRef(
            asset_id=hashlib.sha256(data).hexdigest(),
            size=len(data),
            media_type=media_type,
            origin=origin,
        )
        path = self._publish(data, ref)
        row = AssetRegistration(
            cid,
            ref.asset_id,
            ref.size,
            ref.media_type,
            ref.origin,
            path,
            inbound_only=inbound_only,
            appearance_only=appearance_only,
        )
        return self._prepare_registration(row)

    async def put(self, companion_id, data, media_type, origin, *, inbound_only=False):
        cid = identifier(companion_id)
        resolved, expected = await asyncio.to_thread(
            self._stage_put, cid, data, media_type, origin, inbound_only=inbound_only
        )
        async with companion_coordinator(self.home, cid, self.clock).admit():
            if (
                expected is not None
                and expected.restricted
                and (expected.appearance_only or not inbound_only)
            ):
                # A trusted tool writer supplied the actual bytes, independently
                # establishing a product source. A hash alone cannot do this.
                with self._lock("registry").commit():
                    snapshot = self.snapshot()
                    if expected not in snapshot.registrations:
                        raise AssetError("asset_registration_changed")
                    self.check_identity(resolved)
                    product = replace(
                        expected,
                        origin=origin,
                        media_type=media_type,
                        inbound_only=inbound_only,
                        appearance_only=False,
                    )
                    self._save(
                        tuple(product if row == expected else row for row in snapshot.registrations)
                    )
                    return product.ref()
            return self._register(resolved, expected)


class BoundAssets:
    def __init__(self, store, companion_id, plugin_id=None):
        self._store, self._companion_id = store, companion_id
        self._plugin_id = plugin_id

    def _resolve(self, asset_id):
        grant = _model_grant.get()
        if grant and grant[:2] == (self._companion_id, self._plugin_id) and asset_id in grant[3]:
            return grant[3][asset_id]
        resolved = self._store.resolve(self._store.snapshot(), self._companion_id, asset_id)
        if resolved.registration.appearance_only:
            raise KeyError(asset_id)
        if resolved.registration.restricted:
            if (
                not grant
                or grant[:2] != (self._companion_id, self._plugin_id)
                or asset_id not in grant[2]
            ):
                raise KeyError(asset_id)
        return resolved

    async def info(self, asset_id):
        resolved = self._resolve(asset_id)
        await asyncio.to_thread(self._store.verify_resolved, resolved)
        grant = _model_grant.get()
        if grant and grant[:2] == (self._companion_id, self._plugin_id) and asset_id in grant[2]:
            return grant[2][asset_id]
        return resolved.registration.ref()

    async def read(self, asset_id):
        return await asyncio.to_thread(self._store.read_resolved, self._resolve(asset_id))

    async def path(self, asset_id):
        resolved = self._resolve(asset_id)
        grant = _model_grant.get()
        if resolved.registration.restricted or (
            grant and grant[:2] == (self._companion_id, self._plugin_id) and asset_id in grant[3]
        ):
            raise AssetError("asset_unauthorized")
        await asyncio.to_thread(self._store.verify_resolved, resolved)
        if resolved.path.stat().st_mode & 0o222:
            raise AssetError("asset_path_not_readonly")
        return resolved.path

    async def put(self, data, media_type, origin):
        return await self._store.put(self._companion_id, data, media_type, origin)
