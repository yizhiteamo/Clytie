"""Bounded-memory authenticated regular-backup container (ADR 0014)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import uuid
import zlib
from collections.abc import Callable
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"CLYTIEB1"
CHUNK = 1024 * 1024
MAX_MANIFEST = 16 * CHUNK
MAX_FILES = 65_536
Check = Callable[[], None] | None


class ContainerError(ValueError):
    def __init__(self, code: str = "container_invalid"):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _safe(path: Path):
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ContainerError("container_path")


def _name(name: str, seen: dict[str, tuple[str, bool]]):
    if not isinstance(name, str) or len(name.encode()) > 1024:
        raise ContainerError("container_path")
    parts = name.split("/")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if any(
        not p
        or p in {".", ".."}
        or p.endswith((".", " "))
        or any(c in p for c in '\\:<>"|?*\x00')
        or p.split(".")[0].upper() in reserved
        for p in parts
    ):
        raise ContainerError("container_path")
    for index in range(1, len(parts) + 1):
        prefix = "/".join(parts[:index])
        folded = prefix.casefold()
        final = index == len(parts)
        prior = seen.get(folded)
        if prior is not None:
            if prior[0] != prefix or prior[1] or final:
                raise ContainerError("container_path")
        else:
            seen[folded] = (prefix, final)


def _tick(check: Check):
    if check is not None:
        check()


def _read(stream, count):
    value = stream.read(count)
    if len(value) != count:
        raise ContainerError()
    return value


def pack(staging: Path, target: Path, key: bytes, manifest: dict, *, check: Check = None) -> str:
    """Write an exclusive unpublished package; caller owns cleanup/publication."""
    staging, target = Path(staging), Path(target)
    _safe(staging)
    _safe(target)
    if target.resolve().is_relative_to(staging.resolve()):
        raise ContainerError("container_path")
    if len(key) != 32 or not staging.is_dir() or not isinstance(manifest, dict):
        raise ContainerError()
    content_key = AESGCM.generate_key(bit_length=256)
    header = {
        "version": 1,
        "package_id": str(uuid.uuid4()),
        "algorithm": "AES-256-GCM",
        "fingerprint": hashlib.sha256(key).hexdigest(),
        "wrap_nonce": os.urandom(12).hex(),
    }
    header["wrapped_key"] = (
        AESGCM(key).encrypt(bytes.fromhex(header["wrap_nonce"]), content_key, _json(header)).hex()
    )
    encoded = _json(header)
    cipher = AESGCM(content_key)
    digest = hashlib.sha256()
    sequence = 0
    files, seen = [], {}
    with target.open("xb") as output:

        def write(data):
            output.write(data)
            digest.update(data)

        def frame(kind, payload=b""):
            nonlocal sequence
            _tick(check)
            counter = sequence.to_bytes(8, "big")
            encrypted = cipher.encrypt(b"\0" * 4 + counter, kind + payload, encoded + counter)
            write(struct.pack(">I", len(encrypted)) + encrypted)
            sequence += 1

        write(MAGIC + struct.pack(">I", len(encoded)) + encoded)

        def walk_error(error):
            raise error

        for root, dirs, names in os.walk(staging, followlinks=False, onerror=walk_error):
            dirs.sort()
            for directory in dirs:
                _safe(Path(root) / directory)
            for name in sorted(names):
                path = Path(root) / name
                _safe(path)
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ContainerError("container_path")
                relative = path.relative_to(staging).as_posix()
                _name(relative, seen)
                if len(files) >= MAX_FILES:
                    raise ContainerError("container_limit")
                frame(b"F", _json(relative))
                size, hashed = 0, hashlib.sha256()
                with path.open("rb") as source:
                    while block := source.read(CHUNK):
                        hashed.update(block)
                        size += len(block)
                        frame(b"D", zlib.compress(block))
                frame(b"E")
                files.append({"path": relative, "size": size, "sha256": hashed.hexdigest()})
        listing = _json({"manifest": manifest, "files": files})
        if len(listing) > MAX_MANIFEST:
            raise ContainerError("container_limit")
        for offset in range(0, len(listing), CHUNK):
            frame(b"M", listing[offset : offset + CHUNK])
        frame(b"Z")
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


def owned_marker(target: Path, marker: Path) -> dict:
    """Bind one internally created marker, never inferred from an existing tree."""
    target, marker = Path(target).resolve(), Path(marker)
    _safe(marker)
    relative = marker.relative_to(target).as_posix()
    _name(relative, {})
    info = marker.stat()
    return {
        "path": relative,
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
    }


def _empty_target(target, marker):
    if not target.is_dir():
        raise ContainerError("restore_target_not_empty")
    if marker is None:
        if any(target.iterdir()):
            raise ContainerError("restore_target_not_empty")
        return
    path = target / marker["path"]
    if owned_marker(target, path) != marker:
        raise ContainerError("restore_target_not_empty")
    allowed = {path, *path.parents}
    for member in target.rglob("*"):
        _safe(member)
        if member not in allowed:
            raise ContainerError("restore_target_not_empty")


def unpack(
    source: Path, target: Path, key: bytes, *, check: Check = None, owned_marker: dict | None = None
) -> dict:
    """Authenticate into a caller-controlled empty temporary directory; never mark ready."""
    source, target = Path(source), Path(target)
    _safe(target)
    _empty_target(target, owned_marker)
    _safe(source)
    current = None
    callback_failed = False
    try:
        with source.open("rb") as stream:
            if _read(stream, len(MAGIC)) != MAGIC:
                raise ContainerError()
            length = struct.unpack(">I", _read(stream, 4))[0]
            if length > 4096:
                raise ContainerError("container_limit")
            encoded = _read(stream, length)
            header = json.loads(encoded)
            if (
                header["version"] != 1
                or header["algorithm"] != "AES-256-GCM"
                or len(key) != 32
                or header["fingerprint"] != hashlib.sha256(key).hexdigest()
            ):
                raise ContainerError()
            wrapped = bytes.fromhex(header.pop("wrapped_key"))
            cipher = AESGCM(
                AESGCM(key).decrypt(bytes.fromhex(header["wrap_nonce"]), wrapped, _json(header))
            )
            sequence, listing = 0, bytearray()
            files, seen = [], {}
            in_manifest = False
            while True:
                try:
                    _tick(check)
                except Exception:
                    callback_failed = True
                    raise
                length = struct.unpack(">I", _read(stream, 4))[0]
                if not 17 <= length <= CHUNK + 4096:
                    raise ContainerError("container_limit")
                counter = sequence.to_bytes(8, "big")
                clear = cipher.decrypt(
                    b"\0" * 4 + counter, _read(stream, length), encoded + counter
                )
                sequence += 1
                kind, body = clear[:1], clear[1:]
                if kind == b"F" and current is None and not in_manifest:
                    name = json.loads(body)
                    _name(name, seen)
                    if len(files) >= MAX_FILES:
                        raise ContainerError("container_limit")
                    path = target / name
                    if (
                        owned_marker is not None
                        and name.casefold() == owned_marker["path"].casefold()
                    ):
                        raise ContainerError("container_path")
                    _safe(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    current = path.open("xb")
                    size, hashed = 0, hashlib.sha256()
                elif kind == b"D" and current is not None:
                    decoder = zlib.decompressobj()
                    block = decoder.decompress(body, CHUNK + 1)
                    if (
                        len(block) > CHUNK
                        or decoder.unused_data
                        or decoder.unconsumed_tail
                        or not decoder.eof
                    ):
                        raise ContainerError("container_limit")
                    current.write(block)
                    size += len(block)
                    hashed.update(block)
                elif kind == b"E" and current is not None and not body:
                    current.close()
                    current = None
                    files.append({"path": name, "size": size, "sha256": hashed.hexdigest()})
                elif kind == b"M" and current is None:
                    in_manifest = True
                    listing.extend(body)
                    if len(listing) > MAX_MANIFEST:
                        raise ContainerError("container_limit")
                elif kind == b"Z" and current is None and in_manifest and not body:
                    result = json.loads(listing)
                    if (
                        not isinstance(result, dict)
                        or result.get("files") != files
                        or not isinstance(result.get("manifest"), dict)
                        or stream.read(1)
                    ):
                        raise ContainerError()
                    return result
                else:
                    raise ContainerError()
    except ContainerError:
        raise
    except (
        InvalidTag,
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        zlib.error,
        OverflowError,
    ) as exc:
        if callback_failed:
            raise
        raise ContainerError() from exc
    finally:
        if current is not None:
            current.close()
