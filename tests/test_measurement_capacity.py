"""Small bounded stand-ins for raw stream limits; no performance execution."""

import json

import pytest
from tests.measurement_tools import load
from tests.test_measurement_manifest import manifest_v4, save_manifest

evidence = load("measurement_evidence")
recording = load("measurement_recording")
load("measurement_io_observation")
analysis = load("measurement_analysis_index")


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(evidence, "MAX_FILE", 8 * 1024)
    monkeypatch.setattr(evidence, "STORAGE_MAX_FILE", 64 * 1024, raising=False)


def prepared(directory, version=6):
    run = manifest_v4(directory)
    if version >= 5:
        run.update(observer_sequences=[0], scenario="normal", execution_id="ac" * 32)
    run["format"] = version
    save_manifest(directory, run)
    rows = [
        dict(
            id=(1 << 40) + index + 1,
            request_id=1,
            phase="fsync",
            started_s=0,
            finished_s=1,
            outcome="ok",
        )
        for index in range(100)
    ]
    raw = "".join(json.dumps(row) + "\r\n" for row in rows).encode()
    (directory / "storage-phases.jsonl").write_bytes(raw)
    return run, raw


def test_format_six_real_encryption_preserves_extended_storage_stream(tmp_path, limits):
    source = tmp_path / "source"
    run, raw = prepared(source)
    identity = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    result = evidence.open_bundle(
        tmp_path / "sealed/measurements.age",
        tmp_path / "local",
        identity,
        run,
        private_scan=lambda paths: "pass",
    )
    assert result["accepted"]
    assert (tmp_path / "local/raw/storage-phases.jsonl").read_bytes() == raw
    index = analysis.Index(tmp_path / "local/raw", tmp_path / "analysis", run)
    try:
        assert index.counts["storage-phases.jsonl"] == 100
        assert (
            index.hashes["storage-phases.jsonl"]
            == result["files"]["storage-phases.jsonl"]["sha256"]
        )
    finally:
        index.db.close()


@pytest.mark.parametrize("version", [3, 4, 5])
def test_older_formats_keep_original_limit(tmp_path, limits, version):
    source = tmp_path / "source"
    run, _ = prepared(source, version)
    identity = evidence.native_identity().generate()
    with pytest.raises(evidence.EvidenceError):
        evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    assert not (tmp_path / "sealed/receipt.json").exists()


def test_other_streams_and_child_merge_keep_exact_limits(tmp_path, limits):
    _, raw = prepared(tmp_path / "source")
    recorder = recording.Recorder(format_version=6)
    recorder.append_bytes("storage-phases.jsonl", raw)
    assert bytes(recorder.rows["storage-phases.jsonl"]) == raw
    with pytest.raises(evidence.EvidenceError, match="recording_capacity"):
        recorder.append_bytes("storage-phases.jsonl", raw * 6)
    assert recorder.failed
    assert bytes(recorder.rows["storage-phases.jsonl"]) == raw
    other = recording.Recorder(format_version=6)
    row = dict(id=1, at_s=0, resource="view", event="acquired")
    with pytest.raises(evidence.EvidenceError, match="recording_capacity"):
        other.append_bytes("release-events.jsonl", (json.dumps(row) + "\n").encode() * 200)
    assert other.failed


def test_total_plaintext_bound_is_checked_before_encryption(tmp_path, limits, monkeypatch):
    source = tmp_path / "source"
    run, _ = prepared(source)
    monkeypatch.setattr(evidence, "MAX_TOTAL", 4096)
    identity = evidence.native_identity().generate()
    with pytest.raises(evidence.EvidenceError, match="archive_size_limit"):
        evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    assert not (tmp_path / "sealed/receipt.json").exists()
    assert not (tmp_path / "sealed/measurements.age.partial").exists()


def test_extended_file_limit_does_not_allow_unknown_filename():
    with pytest.raises(evidence.EvidenceError):
        evidence.file_limit("unapproved.jsonl", 6)


def test_declared_capacity_keeps_total_and_historical_bounds():
    assert evidence.file_limit("storage-phases.jsonl", 6) == 1024**3
    assert evidence.file_limit("storage-phases.jsonl", 5) == 256 * 1024**2
    assert evidence.file_limit("requests.jsonl", 6) == 256 * 1024**2
    assert evidence.file_limit("storage-phases.jsonl", 2) == 64 * 1024**2
    assert evidence.MAX_TOTAL == 1281 * 1024**2


@pytest.mark.parametrize("child", [False, True])
def test_collector_rejects_aggregate_overflow_without_partial_append(monkeypatch, child):
    recorder = recording.Recorder(format_version=6)
    row = dict(id=1, at_s=0, resource="view", event="acquired")
    raw = (json.dumps(row, separators=(",", ":")) + "\n").encode()
    monkeypatch.setattr(evidence, "MAX_TOTAL", len(raw) * 2 - 1)
    recorder.append("release-events.jsonl", row)
    before = bytes(recorder.rows["release-events.jsonl"])
    with pytest.raises(evidence.EvidenceError, match="recording_capacity"):
        if child:
            recorder.append_bytes("release-events.jsonl", raw)
        else:
            recorder.append("release-events.jsonl", row)
    assert recorder.failed
    assert bytes(recorder.rows["release-events.jsonl"]) == before
