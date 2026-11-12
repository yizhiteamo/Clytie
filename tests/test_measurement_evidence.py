"""Only fictional files; real age encryption, fixed schemas and repository scanner."""

import hashlib
import importlib.util
import io
import json
import stat
import sys
import zipfile
from pathlib import Path

import pyrage
import pytest


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    if name in sys.modules:
        existing = sys.modules[name]
        if Path(existing.__file__).resolve() != path.resolve():
            raise ValueError("measurement_test_tool_origin_mismatch")
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evidence = load_tool("measurement_evidence")


def fixture_files(directory):
    directory.mkdir()
    run = {
        "format": 1,
        "source": "generated_fictional_only",
        "method": "encryption_fixture_v1",
        "candidate": "ab" * 20,
        "reference": "cd" * 20,
        "run_id": 123,
        "job_id": "bounded_io",
        "attempt": 1,
        "platform": "Windows",
        "python": "3.10.6",
        "sqlite": "3.37.2",
        "seed": 73,
        "scale": "large_database",
        "group": 1,
        "arm": "candidate",
        "phase": "fixture",
        "plan_sha256": "ac" * 32,
        "parameters": {
            "concurrency": 1,
            "waiting": 8,
            "chunk_bytes": 256 * 1024,
            "normal_bytes_s": 16 * 1024 * 1024,
            "busy_bytes_s": 4 * 1024 * 1024,
            "burst_bytes": 512 * 1024,
            "signal_seconds": 0.1,
            "stale_seconds": 1,
            "view_seconds": 300,
            "wal_bytes": 256 * 1024 * 1024,
        },
        "load": {
            "window_seconds": 120,
            "interval_seconds": 0.1,
            "request_count": 1200,
            "maximum_seconds": 240,
        },
        "result": "incomplete",
    }
    records = {
        "requests.jsonl": {
            "id": 1,
            "kind": "business",
            "scheduled_s": 0,
            "started_s": 0.01,
            "finished_s": 0.02,
            "outcome": "ok",
        },
        "storage-phases.jsonl": {
            "id": 1,
            "request_id": 1,
            "phase": "fsync",
            "started_s": 0.01,
            "finished_s": 0.02,
            "outcome": "ok",
        },
        "resources.jsonl": {
            "at_s": 0,
            "rss_bytes": 64 * 1024 * 1024,
            "wal_bytes": 1024,
            "temp_bytes": 0,
            "queue": 0,
            "finalizers": 0,
            "views": 1,
            "backup_bytes": 0,
            "loop_delay_ms": 0.1,
            "rate": "busy",
        },
        "release-events.jsonl": {"id": 1, "at_s": 0, "resource": "view", "event": "acquired"},
    }
    for name, record in records.items():
        (directory / name).write_bytes((json.dumps(record) + "\n").encode())
    (directory / "run-manifest.json").write_bytes(json.dumps(run, indent=2).encode())
    return run


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "source"
    run = fixture_files(source)
    ident = evidence.native_identity().generate()
    recipient = str(ident.to_public())
    return source, run, ident, recipient


def test_real_age_roundtrip_preserves_bytes_and_requires_private_scan(prepared, tmp_path):
    source, run, ident, recipient = prepared
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    sealed = evidence.seal(source, tmp_path / "sealed", recipient, run)
    assert sealed["state"] == "encrypted_pending_verification"
    assert {p.name for p in (tmp_path / "sealed").iterdir()} == {"measurements.age", "receipt.json"}
    assert "files" not in sealed and "run_id" not in sealed
    scanned = []

    def private_scan(files):
        scanned.extend(files)
        return "pass"

    result = evidence.open_bundle(
        tmp_path / "sealed/measurements.age",
        tmp_path / "local",
        ident,
        run,
        private_scan=private_scan,
    )
    assert result["state"] == "verified_private_pass_generic_exception"
    assert set(p.name for p in scanned) == set(original)
    for name, raw in original.items():
        assert (tmp_path / "local/raw" / name).read_bytes() == raw
        assert result["files"][name]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["generic_exceptions"]


@pytest.mark.parametrize("mode", ["wrong_key", "tamper", "truncate", "wrong_run"])
def test_crypto_or_identity_failure_never_extracts(prepared, tmp_path, mode):
    source, run, ident, recipient = prepared
    evidence.seal(source, tmp_path / "sealed", recipient, run)
    ciphertext = tmp_path / "sealed/measurements.age"
    if mode == "wrong_key":
        ident = evidence.native_identity().generate()
    elif mode == "tamper":
        data = bytearray(ciphertext.read_bytes())
        data[-1] ^= 1
        ciphertext.write_bytes(data)
    elif mode == "truncate":
        ciphertext.write_bytes(ciphertext.read_bytes()[:-1])
    else:
        run = {**run, "run_id": run["run_id"] + 1}
    with pytest.raises(evidence.EvidenceError):
        evidence.open_bundle(
            ciphertext, tmp_path / "local", ident, run, private_scan=lambda files: "pass"
        )
    assert not (tmp_path / "local/raw").exists()
    state = json.loads((tmp_path / "local/state.json").read_text())
    assert state["state"] == "verification_failed"


@pytest.mark.parametrize(
    "mode",
    [
        "extra_file",
        "unknown_field",
        "bad_type",
        "nan",
        "sensitive",
        "wrong_source",
        "duplicate_key",
    ],
)
def test_refuse_unapproved_plaintext_before_encryption(prepared, tmp_path, mode):
    source, run, _, recipient = prepared
    if mode == "extra_file":
        (source / "unexpected.txt").write_text("not measurement")
    else:
        path = source / "run-manifest.json"
        data = json.loads(path.read_text())
        if mode == "unknown_field":
            data["details"] = "not allowed"
        elif mode == "bad_type":
            data["run_id"] = True
        elif mode == "nan":
            data["load"]["window_seconds"] = float("nan")
        elif mode == "sensitive":
            data["python"] = "ghp_" + "a" * 30
        elif mode == "wrong_source":
            data["source"] = "not_fictional"
        path.write_text(json.dumps(data), encoding="utf-8")
        if mode == "duplicate_key":
            path.write_text(path.read_text().replace('"format": 1', '"format": 1, "format": 1'))
    with pytest.raises(evidence.EvidenceError):
        evidence.seal(source, tmp_path / "sealed", recipient, run)
    assert not (tmp_path / "sealed/receipt.json").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape.json",
        "/absolute.json",
        "C:relative.json",
        "requests.jsonl/extra",
        "requests.jsonl",
    ],
)
def test_authenticated_archive_rejects_unexpected_paths_or_link_entries(prepared, tmp_path, name):
    _, run, ident, recipient = prepared
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"not a measurement")
    encrypted = tmp_path / "invalid.age"
    encrypted.write_bytes(pyrage.encrypt(stream.getvalue(), [ident.to_public()]))
    with pytest.raises(evidence.EvidenceError):
        evidence.open_bundle(
            encrypted, tmp_path / "local", ident, run, private_scan=lambda files: "pass"
        )
    assert not (tmp_path / "local/raw").exists()


@pytest.mark.parametrize(
    "result,state",
    [("fail", "private_scan_failed"), ("unavailable", "encrypted_pending_verification")],
)
def test_local_scan_does_not_turn_failure_into_pass(prepared, tmp_path, result, state):
    source, run, ident, recipient = prepared
    evidence.seal(source, tmp_path / "sealed", recipient, run)
    report = evidence.open_bundle(
        tmp_path / "sealed/measurements.age",
        tmp_path / "local",
        ident,
        run,
        private_scan=lambda files: result,
    )
    assert report["state"] == state
    assert not report["accepted"]


@pytest.mark.parametrize("exc", [OSError("synthetic"), KeyboardInterrupt()])
def test_encrypt_failure_or_cancel_has_no_upload_receipt(prepared, tmp_path, monkeypatch, exc):
    source, run, _, recipient = prepared

    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(pyrage, "encrypt_io", fail)
    with pytest.raises(type(exc)):
        evidence.seal(source, tmp_path / "sealed", recipient, run)
    assert not (tmp_path / "sealed/receipt.json").exists()
    assert not (tmp_path / "sealed/measurements.age").exists()


def test_no_overwrite_and_scanner_failure_closed(prepared, tmp_path, monkeypatch):
    source, run, _, recipient = prepared
    target = tmp_path / "sealed"
    target.mkdir()
    (target / "untouched").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        evidence.seal(source, target, recipient, run)
    assert (target / "untouched").read_bytes() == b"keep"
    monkeypatch.setattr(evidence, "public_scanner", lambda: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(evidence.EvidenceError):
        evidence.seal(source, tmp_path / "another", recipient, run)
    assert not (tmp_path / "another/receipt.json").exists()


def test_input_link_refused(prepared, tmp_path):
    source, run, _, recipient = prepared
    target = source / "resources.jsonl"
    original = tmp_path / "resources.jsonl"
    target.rename(original)
    try:
        target.symlink_to(original)
    except OSError:
        pytest.skip("OS does not authorize creating a test symlink")
    with pytest.raises(evidence.EvidenceError):
        evidence.seal(source, tmp_path / "sealed", recipient, run)


def test_encrypted_payload_file_hash_is_independently_checked(prepared, tmp_path):
    source, run, ident, recipient = prepared
    evidence.seal(source, tmp_path / "sealed", recipient, run)
    raw = pyrage.decrypt((tmp_path / "sealed/measurements.age").read_bytes(), [ident])
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(output, "w") as new:
        for entry in old.infolist():
            data = old.read(entry)
            if entry.filename == "requests.jsonl":
                data += b"\n"
            new.writestr(entry, data)
    path = tmp_path / "wrong-digest.age"
    path.write_bytes(pyrage.encrypt(output.getvalue(), [ident.to_public()]))
    with pytest.raises(evidence.EvidenceError):
        evidence.open_bundle(
            path, tmp_path / "local", ident, run, private_scan=lambda files: "pass"
        )
    assert not (tmp_path / "local/raw").exists()


def test_private_scanner_uses_local_rules_without_leaking_terms(prepared, tmp_path, monkeypatch):
    source, run, ident, recipient = prepared
    evidence.seal(source, tmp_path / "sealed", recipient, run)
    original = evidence._scanner

    def scanner(private=False):
        if private:
            return {"normalized": str.casefold, "denylist": ["generated_fictional_only"]}
        return original()

    monkeypatch.setattr(evidence, "_scanner", scanner)
    report = evidence.open_bundle(
        tmp_path / "sealed/measurements.age", tmp_path / "local", ident, run
    )
    assert report["state"] == "private_scan_failed"
    assert "generated_fictional_only" not in (tmp_path / "local/state.json").read_text()
