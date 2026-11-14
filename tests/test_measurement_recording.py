import json

import pytest
from tests.test_measurement_evidence import evidence, fixture_files, load_tool

recording = load_tool("measurement_recording")


def test_recorder_preserves_every_sample_and_seals_fixed_schema(tmp_path):
    source = tmp_path / "fixture"
    expected = fixture_files(source)
    recorder = recording.Recorder()
    for name in evidence.FILES[:-1]:
        for raw in (source / name).read_text().splitlines():
            recorder.append(name, json.loads(raw))
    target = tmp_path / "raw"
    recorder.finish(target, expected)
    for name in evidence.FILES[:-1]:
        assert [json.loads(row) for row in (target / name).read_text().splitlines()] == [
            json.loads(row) for row in (source / name).read_text().splitlines()
        ]
    assert set(path.name for path in target.iterdir()) == set(evidence.FILES)
    contents = {name: (target / name).read_bytes() for name in evidence.FILES}
    evidence.validate_files(contents, expected)
    with pytest.raises(evidence.EvidenceError, match="recording_closed"):
        recorder.append("release-events.jsonl", {})


@pytest.mark.parametrize("failure", ["unknown_file", "unknown_field", "capacity"])
def test_recorder_never_drops_invalid_or_excess_samples_and_claims_success(tmp_path, failure):
    expected = fixture_files(tmp_path / "fixture")
    recorder = recording.Recorder(max_file_bytes=32 if failure == "capacity" else None)
    row = {"id": 1, "at_s": 0, "resource": "view", "event": "released"}
    name = "release-events.jsonl"
    if failure == "unknown_file":
        name = "unexpected.jsonl"
    if failure == "unknown_field":
        row["unexpected"] = "fictional"
    with pytest.raises(evidence.EvidenceError):
        recorder.append(name, row)
    with pytest.raises(evidence.EvidenceError, match="recording_invalid"):
        recorder.finish(tmp_path / "raw", expected)
    assert not (tmp_path / "raw").exists()


def test_recorder_refuses_existing_destination_and_keeps_prior_evidence(tmp_path):
    expected = fixture_files(tmp_path / "fixture")
    recorder = recording.Recorder()
    target = tmp_path / "raw"
    target.mkdir()
    (target / "prior").write_bytes(b"fictional prior evidence")
    with pytest.raises(evidence.EvidenceError):
        recorder.finish(target, expected)
    assert (target / "prior").read_bytes() == b"fictional prior evidence"


def test_child_rows_keep_exact_bytes_when_joined_to_parent_evidence():
    recorder = recording.Recorder()
    raw = b'{ "id": 8, "request_id": 0, "phase": "fsync", "started_s": 1.00, "finished_s": 2.00, "outcome": "ok" }\r\n'
    recorder.append_bytes("storage-phases.jsonl", raw)
    assert bytes(recorder.rows["storage-phases.jsonl"]) == raw


@pytest.mark.parametrize("raw", [b"", b"{}\n", b'{"id": 1}', b"{}\ntruncated"])
def test_child_incomplete_or_invalid_rows_invalidate_recording(raw):
    recorder = recording.Recorder()
    with pytest.raises(evidence.EvidenceError):
        recorder.append_bytes("storage-phases.jsonl", raw)
    assert recorder.failed
    assert not recorder.rows["storage-phases.jsonl"]
