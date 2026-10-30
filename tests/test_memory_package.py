"""临时实例里的双解密路径、无种子初始化与包校验。"""

import json

import pytest

from clytie.memory.crypto import PackageError, b64, decrypt, encrypt, new_recovery, un64
from clytie.memory.package import Exports, payload, unpack
from clytie.memory.store import MemoryStore, canonical
from clytie.sdk.testing import MockClock

PASSPHRASE = "fictional-passphrase-only"


@pytest.fixture
def export_case(tmp_path):
    store = MemoryStore(
        tmp_path / "companions/companion_a/memory/memory.sqlite3",
        clock=MockClock(),
        config={},
        instance={},
    )
    yield store, Exports(store, tmp_path, "companion_a"), tmp_path
    store.close()


def seed(store):
    return store.write(
        {"kind": "fact", "content": "一起看过虚构的蓝色月亮", "visibility": "private"},
        scene="owner_private",
        conversation_id="owner_chat",
        owner="owner",
        operator="owner",
    )


def test_empty_export_has_no_files_or_identity(export_case):
    store, exports, home = export_case
    for format in ("encrypted", "jsonl"):
        with pytest.raises(ValueError, match="还没有记忆"):
            exports.export(home / "exports/empty.enc", passphrase=PASSPHRASE, format=format)
    assert store.uid is None
    assert not (home / "exports").exists()
    assert not (home / "secrets").exists()


def test_password_and_recovery_open_same_package_without_private_key_on_disk(export_case):
    store, exports, home = export_case
    record = seed(store)
    store.db.execute("INSERT INTO vectors VALUES(?,?,?,?)", (record.record_id, 1, "fake", "[1,0]"))
    result = exports.export(home / "first.enc", passphrase=PASSPHRASE)
    encrypted = (home / "first.enc").read_bytes()
    raw = decrypt(encrypted, passphrase=PASSPHRASE)
    assert raw == decrypt(encrypted, recovery_code=result["recovery_code"])
    manifest, db = unpack(raw, home, "companion_a")
    try:
        assert manifest["companion_uid"] == store.uid
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        assert not names & {"vectors", "keywords", "ledger", "chat"}
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 1
    finally:
        db.close()
    assert [p.name for p in (home / "secrets").iterdir()] == ["memory-recovery.pub"]
    for path in home.rglob("*"):
        if path.is_file():
            assert result["recovery_code"].encode() not in path.read_bytes()
    with pytest.raises(PackageError):
        decrypt(encrypted, passphrase="wrong-fictional-passphrase")
    wrong_code, _ = new_recovery()
    with pytest.raises(PackageError):
        decrypt(encrypted, recovery_code=wrong_code)


def test_rotation_preserves_old_package_and_new_export_does_not_repeat_code(export_case):
    store, exports, home = export_case
    seed(store)
    old = exports.export(home / "old.enc", passphrase=PASSPHRASE)["recovery_code"]
    assert exports.export(home / "again.enc", passphrase=PASSPHRASE)["recovery_code"] is None
    with pytest.raises(ValueError, match="确认"):
        exports.rotate_recovery()
    new = exports.rotate_recovery(confirmed=True)
    exports.export(home / "new.enc", passphrase=PASSPHRASE)
    decrypt((home / "old.enc").read_bytes(), recovery_code=old)
    decrypt((home / "new.enc").read_bytes(), recovery_code=new)
    with pytest.raises(PackageError):
        decrypt((home / "old.enc").read_bytes(), recovery_code=new)


def test_jsonl_is_stable_sorted_and_confined_to_exports(export_case):
    store, exports, home = export_case
    seed(store)
    store.write(
        {"kind": "fact", "content": "另一个虚构事实", "visibility": "public"},
        scene="owner_private",
        conversation_id="owner_chat",
    )
    for name in ("one", "two"):
        exports.export(home / f"exports/{name}.jsonl", format="jsonl")
    first = (home / "exports/one.jsonl").read_bytes()
    assert first == (home / "exports/two.jsonl").read_bytes()
    rows = [json.loads(row) for row in first.splitlines()]
    assert [r["record_id"] for r in rows] == sorted(r["record_id"] for r in rows)
    assert all(r["revision"] == 1 for r in rows)
    with pytest.raises(ValueError):
        exports.export(home / "outside.jsonl", format="jsonl")
    assert not (home / "outside.jsonl").exists()


def test_authenticated_header_ciphertext_and_manifest_are_checked(export_case):
    store, _, home = export_case
    seed(store)
    _, public = new_recovery()
    package = encrypt(payload(store, "companion_a", home), PASSPHRASE, public)
    outer = json.loads(package)
    header = json.loads(un64(outer["header"]))
    header["base"]["recovery"]["fingerprint"] = "f" * 64
    outer["header"] = b64(canonical(header).encode())
    with pytest.raises(PackageError):
        decrypt(canonical(outer).encode(), passphrase=PASSPHRASE)
    outer = json.loads(package)
    ciphertext = bytearray(un64(outer["data"]))
    ciphertext[-1] ^= 1
    outer["data"] = b64(ciphertext)
    with pytest.raises(PackageError):
        decrypt(canonical(outer).encode(), passphrase=PASSPHRASE)
    plain = json.loads(payload(store, "companion_a", home))
    plain["manifest"]["record_count"] += 1
    with pytest.raises(PackageError):
        unpack(canonical(plain).encode(), home, "companion_a")


def test_written_package_is_verified_before_publication(export_case, monkeypatch):
    from clytie.memory import package as module

    store, exports, home = export_case
    seed(store)
    original = module.atomic_write

    def corrupted(path, data):
        original(path, b"fictional broken package" if path.suffix == ".pending" else data)

    with monkeypatch.context() as context:
        context.setattr(module, "atomic_write", corrupted)
        with pytest.raises(PackageError):
            exports.export(home / "broken.enc", passphrase=PASSPHRASE)
    assert not (home / "broken.enc").exists()
    assert not exports.recovery_path().exists()
    assert not list(home.glob("*.pending"))
    assert exports.export(home / "valid.enc", passphrase=PASSPHRASE)["recovery_code"]
    plain = json.loads(payload(store, "companion_a", home))
    plain["files"]["../escape"] = plain["files"]["memory.sqlite3"]
    with pytest.raises(PackageError):
        unpack(canonical(plain).encode(), home, "companion_a")


async def test_manifest_software_version_legacy_and_unknown_formats(export_case):
    from importlib.metadata import version

    from clytie.memory.transfer import Transfers

    store, exports, home = export_case
    seed(store)
    exports.export(home / "new.enc", passphrase=PASSPHRASE)
    raw = decrypt((home / "new.enc").read_bytes(), passphrase=PASSPHRASE)
    plain = json.loads(raw)
    assert plain["manifest"]["version"] == 2
    assert plain["manifest"]["clytie_version"] == version("clytie")
    for value in (None, "invalid version", 42):
        altered = json.loads(raw)
        altered["manifest"]["clytie_version"] = value
        with pytest.raises(PackageError):
            unpack(canonical(altered).encode(), home, "companion_a")
    for format_version in (0, 3, True, "2"):
        altered = json.loads(raw)
        altered["manifest"]["version"] = format_version
        with pytest.raises(PackageError, match="版本"):
            unpack(canonical(altered).encode(), home, "companion_a")
    plain["manifest"]["version"] = 1
    with pytest.raises(PackageError, match="字段"):
        unpack(canonical(plain).encode(), home, "companion_a")
    del plain["manifest"]["clytie_version"]
    manifest, db = unpack(canonical(plain).encode(), home, "companion_a")
    db.close()
    assert manifest["version"] == 1 and "clytie_version" not in manifest
    _, public = new_recovery()
    legacy = home / "legacy.enc"
    legacy.write_bytes(encrypt(canonical(plain).encode(), PASSPHRASE, public))
    plan = await Transfers(store, home, "companion_a").plan(
        legacy, mode="merge", passphrase=PASSPHRASE
    )
    assert "版本未知" in plan["warnings"][0]


def test_missing_install_version_refuses_export(export_case, monkeypatch):
    from clytie.memory import package

    store, exports, home = export_case
    seed(store)

    def missing(name):
        raise package.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(package.metadata, "version", missing)
    with pytest.raises(ValueError, match="安装版本"):
        exports.export(home / "refused.enc", passphrase=PASSPHRASE)
    assert not (home / "refused.enc").exists()
