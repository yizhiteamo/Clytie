"""Fictional transport tests only; keys exist in test memory only."""

import io
import json
import stat
import zipfile
from pathlib import Path

import pyrage
import pytest
from tests.measurement_tools import load
from tests.test_measurement_plan import inputs

prep = load("measurement_preparation_evidence")


def fixture(tmp_path, platform="Windows"):
    _, geometries = inputs()
    binding = dict(
        candidate="a" * 40,
        source_inventory_sha256="b" * 64,
        tool_sha256="c" * 64,
        geometry_tool_sha256="d" * 64,
        preparation_tool_sha256="e" * 64,
        fixture_sha256="b" * 64,
        fixture_version="legal_memory_v2",
        seed=73,
        platform=platform,
        run_id=123,
        job_id="prepare_windows" if platform == "Windows" else "prepare_linux",
        attempt=1,
    )
    runtime = dict(
        python="3.12.9",
        sqlite="3.45.1",
        environment=dict(
            cpu_count=4, os=platform, release_sha256="f" * 64, machine_sha256="a" * 64
        ),
    )
    source = tmp_path / "source"
    source.mkdir()
    contents = {
        "geometry.json": (json.dumps(geometries[platform], indent=2) + "\n").encode(),
        "runtime.json": (json.dumps(runtime, indent=2) + "\r\n").encode(),
    }
    for name, raw in contents.items():
        (source / name).write_bytes(raw)
    ident = prep.evidence.native_identity().generate()
    return source, binding, contents, ident


@pytest.mark.parametrize("platform", ["Windows", "Linux"])
def test_roundtrip_bytes_and_scope(tmp_path, platform):
    source, binding, contents, ident = fixture(tmp_path, platform)
    receipt = prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    seen = []

    def scan(paths):
        seen.extend(path.name for path in paths)
        return "pass"

    state = prep.open_bundle(
        tmp_path / "sealed" / receipt["file"],
        tmp_path / "opened",
        ident,
        binding,
        private_scan=scan,
    )
    assert state["accepted"] is True
    assert state["purpose"] == "preparation_only_not_measurement"
    assert state["measurement_acceptance"] == "not_applicable"
    assert set(seen) == {*prep.FILES, "archive-manifest.json"}
    for name, raw in contents.items():
        assert (tmp_path / "opened" / "raw" / name).read_bytes() == raw
    assert set(receipt) == {
        "state",
        "format",
        "purpose",
        "recipient_sha256",
        "ciphertext_sha256",
        "ciphertext_bytes",
        "file",
    }


@pytest.mark.parametrize("outcome", ["fail", "unavailable", "unknown", None])
def test_private_scan_fail_closed(tmp_path, outcome):
    source, binding, _, ident = fixture(tmp_path)
    prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    state = prep.open_bundle(
        tmp_path / "sealed" / prep.CIPHER,
        tmp_path / "opened",
        ident,
        binding,
        private_scan=lambda paths: outcome,
    )
    assert state["accepted"] is False


@pytest.mark.parametrize("change", ["key", "tamper", "truncate", "candidate", "job", "fixture"])
def test_authentication_and_identity(tmp_path, change):
    source, binding, _, ident = fixture(tmp_path)
    prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    ciphertext = tmp_path / "sealed" / prep.CIPHER
    if change == "key":
        ident = prep.evidence.native_identity().generate()
    elif change in {"tamper", "truncate"}:
        raw = bytearray(ciphertext.read_bytes())
        if change == "tamper":
            raw[-1] ^= 1
        else:
            raw = raw[:-10]
        ciphertext.write_bytes(raw)
    else:
        field = {"candidate": "candidate", "job": "run_id", "fixture": "fixture_sha256"}[change]
        binding[field] = 456 if field == "run_id" else "f" * len(binding[field])
    with pytest.raises(prep.Error, match="verification_failed"):
        prep.open_bundle(
            ciphertext,
            tmp_path / "opened",
            ident,
            binding,
            private_scan=lambda paths: pytest.fail("must not scan"),
        )
    assert json.loads((tmp_path / "opened" / "state.json").read_bytes())["accepted"] is False
    assert not (tmp_path / "opened" / "raw").exists()


@pytest.mark.parametrize("change", ["extra", "boolean", "duplicate", "oversize", "line", "nan"])
def test_invalid_source_rejected(tmp_path, change):
    source, binding, contents, ident = fixture(tmp_path)
    runtime = json.loads(contents["runtime.json"])
    if change == "extra":
        runtime["exception"] = "forbidden"
    if change == "boolean":
        runtime["environment"]["cpu_count"] = True
    raw = json.dumps(runtime).encode()
    raw = {
        "duplicate": b'{"python":"3.12.9","python":"3.12.9"}',
        "oversize": b" " * (prep.MAX_FILE + 1),
        "line": b" " * (prep.MAX_LINE + 1),
        "nan": b'{"python":NaN}',
    }.get(change, raw)
    (source / "runtime.json").write_bytes(raw)
    with pytest.raises(prep.Error):
        prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    assert not (tmp_path / "sealed" / "receipt.json").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "zip_path",
        "zip_link",
        "zip_duplicate",
        "compressed",
        "zip_comment",
        "entry_comment",
    ],
)
def test_archive_structure_and_hash(tmp_path, fault):
    source, binding, _, ident = fixture(tmp_path)
    prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    ciphertext = tmp_path / "sealed" / prep.CIPHER
    plain = pyrage.decrypt(ciphertext.read_bytes(), [ident])
    with zipfile.ZipFile(io.BytesIO(plain)) as original:
        entries = {name: original.read(name) for name in original.namelist()}
    if fault == "hash":
        manifest = json.loads(entries["archive-manifest.json"])
        manifest["files"]["runtime.json"]["sha256"] = "a" * 64
        entries["archive-manifest.json"] = json.dumps(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if fault == "zip_comment":
            archive.comment = b"forbidden"
        for name, raw in entries.items():
            if fault == "zip_path" and name == "runtime.json":
                name = "../runtime.json"
            info = zipfile.ZipInfo(name)
            if fault == "entry_comment":
                info.comment = b"forbidden"
            if fault == "zip_link":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            if fault == "compressed":
                info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
        if fault == "zip_duplicate":
            archive.writestr("runtime.json", entries["runtime.json"])
    ciphertext.write_bytes(pyrage.encrypt(output.getvalue(), [ident.to_public()]))
    with pytest.raises(prep.Error):
        prep.open_bundle(
            ciphertext,
            tmp_path / "opened",
            ident,
            binding,
            private_scan=lambda paths: pytest.fail("must not scan"),
        )


def test_scanner_sensitive(tmp_path, monkeypatch):
    _, binding, contents, _ = fixture(tmp_path)
    baseline = prep.validate_files(contents, binding)
    assert isinstance(baseline, list)
    monkeypatch.setattr(prep.evidence, "public_scanner", lambda: lambda raw: {"secret"})
    with pytest.raises(prep.Error):
        prep.validate_files(contents, binding)


def test_numeric_exception_is_exact(tmp_path):
    _, binding, contents, _ = fixture(tmp_path)
    binding["run_id"] = 123_456_789
    result = prep.validate_files(contents, binding)
    assert {item["category"] for item in result} == {"schema_validated_fictional_numeric"}
    assert any(item["file"] == "binding" for item in result)


@pytest.mark.parametrize("kind", ["raise", "invalid_return", "extra_category"])
def test_scanner_unavailable(tmp_path, monkeypatch, kind):
    _, binding, contents, _ = fixture(tmp_path)

    def scanner(raw):
        if kind == "raise":
            raise RuntimeError("fictional scanner unavailable")
        if kind == "invalid_return":
            return None
        return {"possible-account-number", "possible-secret"}

    monkeypatch.setattr(prep.evidence, "public_scanner", lambda: scanner)
    with pytest.raises(prep.Error):
        prep.validate_files(contents, binding)


def test_private_scan_exception_no_acceptance(tmp_path):
    source, binding, _, ident = fixture(tmp_path)
    prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)

    def unavailable(paths):
        raise RuntimeError("fictional private scanner unavailable")

    with pytest.raises(prep.Error, match="verification_failed"):
        prep.open_bundle(
            tmp_path / "sealed" / prep.CIPHER,
            tmp_path / "opened",
            ident,
            binding,
            private_scan=unavailable,
        )
    assert json.loads((tmp_path / "opened" / "state.json").read_bytes())["accepted"] is False


@pytest.mark.parametrize("flag", ["reparse", "symlink"])
def test_path_links_rejected(tmp_path, monkeypatch, flag):
    source, binding, _, ident = fixture(tmp_path)
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        original_stat = original(path, *args, **kwargs)
        if path == source / "runtime.json":
            from types import SimpleNamespace

            return SimpleNamespace(
                st_mode=stat.S_IFLNK if flag == "symlink" else stat.S_IFREG,
                st_file_attributes=0x400 if flag == "reparse" else 0,
            )
        return original_stat

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(prep.Error):
        prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)


@pytest.mark.parametrize(
    "field,value",
    [
        ("seed", True),
        ("attempt", True),
        ("candidate", "a" * 39),
        ("fixture_version", "legal_memory_v1"),
        ("job_id", "unapproved"),
    ],
)
def test_binding_schema_strict(tmp_path, field, value):
    source, binding, _, ident = fixture(tmp_path)
    binding[field] = value
    with pytest.raises(prep.Error):
        prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)


def test_utf16_and_traversal_rejected(tmp_path):
    source, binding, contents, ident = fixture(tmp_path)
    (source / "runtime.json").write_bytes(contents["runtime.json"].decode().encode("utf-16"))
    with pytest.raises(prep.Error):
        prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    with pytest.raises(prep.Error):
        prep._read_sources(source / ".." / "source")


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_encrypt_failure_never_receipt(tmp_path, monkeypatch, exception):
    source, binding, _, ident = fixture(tmp_path)

    def fail(*args, **kwargs):
        raise exception("fictional error")

    monkeypatch.setattr(pyrage, "encrypt_io", fail)
    with pytest.raises((prep.Error, KeyboardInterrupt)):
        prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    assert not (tmp_path / "sealed" / "receipt.json").exists()


def test_source_hardlink_and_extra_file(tmp_path, monkeypatch):
    source, binding, _, ident = fixture(tmp_path)
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == source / "runtime.json":
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=result.st_mode, st_nlink=2, st_size=result.st_size)
        return result

    with monkeypatch.context() as context:
        context.setattr(Path, "stat", stat)
        with pytest.raises(prep.Error):
            prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    (source / "unexpected.json").write_bytes(b"{}")
    with pytest.raises(prep.Error):
        prep.seal(source, tmp_path / "sealed2", str(ident.to_public()), binding)


def test_authenticated_archive_rejects_nul_suffix_in_original_member_name(tmp_path):
    source, binding, _, ident = fixture(tmp_path)
    prep.seal(source, tmp_path / "sealed", str(ident.to_public()), binding)
    ciphertext = tmp_path / "sealed" / prep.CIPHER
    with zipfile.ZipFile(io.BytesIO(pyrage.decrypt(ciphertext.read_bytes(), [ident]))) as original:
        entries = {name: original.read(name) for name in original.namelist()}
    changed = io.BytesIO()
    with zipfile.ZipFile(changed, "w") as archive:
        for name, raw in entries.items():
            archive.writestr(name + "XXXX" if name == "runtime.json" else name, raw)
    raw = changed.getvalue().replace(b"runtime.jsonXXXX", b"runtime.json\0abc")
    ciphertext.write_bytes(pyrage.encrypt(raw, [ident.to_public()]))
    with pytest.raises(prep.Error):
        prep.open_bundle(
            ciphertext, tmp_path / "opened", ident, binding, private_scan=lambda _: "pass"
        )
    assert not (tmp_path / "opened" / "raw").exists()
