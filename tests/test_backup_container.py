import hashlib
import tracemalloc

import pytest

from clytie.backup.container import ContainerError, pack, unpack


def sample(tmp_path):
    staging = tmp_path / "source"
    staging.mkdir()
    (staging / "data").write_bytes(b"fictional-data" * 200_000)
    (staging / "empty").write_bytes(b"")
    return staging


def test_roundtrip_and_encrypted_metadata(tmp_path):
    staging = sample(tmp_path)
    package = tmp_path / "archive"
    digest = pack(staging, package, b"k" * 32, {"scope": "instance"})
    assert digest == hashlib.sha256(package.read_bytes()).hexdigest()
    assert b"fictional-data" not in package.read_bytes()
    target = tmp_path / "target"
    target.mkdir()
    result = unpack(package, target, b"k" * 32)
    assert result["manifest"] == {"scope": "instance"}
    assert (target / "data").read_bytes() == (staging / "data").read_bytes()
    assert (target / "empty").read_bytes() == b""


@pytest.mark.parametrize("damage", ["truncate", "append", "tamper", "key"])
def test_corruption_rejected(tmp_path, damage):
    package = tmp_path / "archive"
    pack(sample(tmp_path), package, b"k" * 32, {})
    data = package.read_bytes()
    if damage == "truncate":
        package.write_bytes(data[:-20])
    elif damage == "append":
        package.write_bytes(data + b"extra")
    elif damage == "tamper":
        package.write_bytes(data[:-30] + bytes([data[-30] ^ 1]) + data[-29:])
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(ContainerError):
        unpack(package, target, (b"x" if damage == "key" else b"k") * 32)
    assert not (target / "ready").exists()


def test_nonempty_output_rejected(tmp_path):
    with pytest.raises(ContainerError, match="restore_target_not_empty"):
        unpack(tmp_path / "missing", sample(tmp_path), b"k" * 32)


def test_large_file_memory_bound_and_cancel(tmp_path):
    staging = tmp_path / "large"
    staging.mkdir()
    block = bytes(range(256)) * 4096
    with (staging / "data").open("wb") as output:
        for _ in range(64):
            output.write(block)
    package = tmp_path / "archive"
    target = tmp_path / "out"
    target.mkdir()
    tracemalloc.start()
    pack(staging, package, b"k" * 32, {})
    unpack(package, target, b"k" * 32)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 24 * 1024 * 1024
    assert (target / "data").stat().st_size == 64 * 1024 * 1024

    def cancel():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        pack(staging, tmp_path / "cancel", b"k" * 32, {}, check=cancel)


@pytest.mark.parametrize(
    "names",
    [["../escape"], ["A/data", "a/other"], ["same", "same"], ["CON"], ["a", "a/b"], ["x:ads"]],
)
def test_authenticated_unsafe_members_rejected(tmp_path, names):
    _malicious(tmp_path / "archive", [], names=names)
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(ContainerError):
        unpack(tmp_path / "archive", target, b"k" * 32)


def _malicious(path, frames, names=None):
    import json
    import struct

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from clytie.backup.container import MAGIC, _json

    content = b"c" * 32
    header = dict(
        version=1,
        package_id="fictional-package",
        algorithm="AES-256-GCM",
        fingerprint=hashlib.sha256(b"k" * 32).hexdigest(),
        wrap_nonce=(b"n" * 12).hex(),
    )
    header["wrapped_key"] = AESGCM(b"k" * 32).encrypt(b"n" * 12, content, _json(header)).hex()
    encoded = _json(header)
    if names is not None:
        frames = []
        files = []
        for name in names:
            frames.extend([(b"F", json.dumps(name).encode()), (b"E", b"")])
            files.append(dict(path=name, size=0, sha256=hashlib.sha256(b"").hexdigest()))
        frames += [(b"M", _json(dict(manifest={}, files=files))), (b"Z", b"")]
    with path.open("wb") as output:
        output.write(MAGIC + struct.pack(">I", len(encoded)) + encoded)
        for i, (kind, body) in enumerate(frames):
            counter = i.to_bytes(8, "big")
            block = AESGCM(content).encrypt(b"\0" * 4 + counter, kind + body, encoded + counter)
            output.write(struct.pack(">I", len(block)) + block)


def test_authenticated_decompression_bomb_rejected(tmp_path):
    import zlib

    from clytie.backup.container import CHUNK

    package = tmp_path / "archive"
    _malicious(package, [(b"F", b'"data"'), (b"D", zlib.compress(b"a" * (CHUNK + 1)))])
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(ContainerError, match="container_limit"):
        unpack(package, target, b"k" * 32)


def test_member_manifest_mismatch_rejected(tmp_path):
    package = tmp_path / "archive"
    _malicious(
        package,
        [(b"F", b'"data"'), (b"E", b""), (b"M", b'{"manifest":{},"files":[]}'), (b"Z", b"")],
    )
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(ContainerError):
        unpack(package, target, b"k" * 32)


@pytest.mark.parametrize("change", ["reorder", "duplicate", "remove"])
def test_frame_sequence_authentication(tmp_path, change):
    import struct

    package = tmp_path / "archive"
    pack(sample(tmp_path), package, b"k" * 32, {})
    data = package.read_bytes()
    offset = 12 + struct.unpack(">I", data[8:12])[0]
    prefix, frames = data[:offset], []
    while offset < len(data):
        end = offset + 4 + struct.unpack(">I", data[offset : offset + 4])[0]
        frames.append(data[offset:end])
        offset = end
    if change == "reorder":
        frames[0], frames[1] = frames[1], frames[0]
    elif change == "duplicate":
        frames.insert(1, frames[0])
    else:
        del frames[1]
    package.write_bytes(prefix + b"".join(frames))
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(ContainerError):
        unpack(package, target, b"k" * 32)


def test_pack_rejects_target_within_staging(tmp_path):
    staging = sample(tmp_path)
    with pytest.raises(ContainerError, match="container_path"):
        pack(staging, staging / "archive", b"k" * 32, {})


def test_authenticated_invalid_manifest_type(tmp_path):
    _malicious(tmp_path / "archive", [(b"M", b"[]"), (b"Z", b"")])
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(ContainerError):
        unpack(tmp_path / "archive", target, b"k" * 32)


@pytest.mark.parametrize("limit", ["MAX_FILES", "MAX_MANIFEST"])
def test_configured_format_limits_reject_before_success(tmp_path, monkeypatch, limit):
    import clytie.backup.container as container

    monkeypatch.setattr(container, limit, 1)
    with pytest.raises(ContainerError, match="container_limit"):
        pack(sample(tmp_path), tmp_path / "archive", b"k" * 32, {})


def test_callback_valueerror_preserved(tmp_path):
    package = tmp_path / "archive"
    pack(sample(tmp_path), package, b"k" * 32, {})
    target = tmp_path / "out"
    target.mkdir()

    def check():
        raise ValueError("caller_cancelled")

    with pytest.raises(ValueError, match="caller_cancelled"):
        unpack(package, target, b"k" * 32, check=check)
