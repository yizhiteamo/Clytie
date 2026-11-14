"""Legal failed measurements remain exact evidence, never acceptance results."""

import json

import pytest
from tests.test_measurement_evidence import evidence, load_tool
from tests.test_measurement_manifest import contents, manifest_v4, save_manifest

recording = load_tool("measurement_recording")


def native_row(identifier, event, *, connection=0):
    return {
        "id": identifier,
        "request_id": 1,
        "phase": "native_validation",
        "validation_id": 1,
        "connection_id": connection,
        "event": event,
        "at_s": identifier / 100,
        "outcome": "ok",
        "internal_read_bytes": None,
        "boundary": "native_checks",
    }


def failed_files(directory, result, *, premature=False):
    run = manifest_v4(directory)
    run["result"] = result
    save_manifest(directory, run)
    rows = [native_row(1, "started"), native_row(2, "connection_attached", connection=1)]
    if premature:
        rows.append(native_row(3, "finished"))
    raw = b"".join((json.dumps(row) + "\r\n").encode() for row in rows)
    (directory / "storage-phases.jsonl").write_bytes(raw)
    return run


@pytest.mark.parametrize("result", ["fail", "incomplete", "cancelled"])
@pytest.mark.parametrize("premature", [False, True])
def test_failed_native_lifecycle_roundtrips_exactly_without_acceptance(tmp_path, result, premature):
    source = tmp_path / "source"
    run = failed_files(source, result, premature=premature)
    original = contents(source)
    identity = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    state = evidence.open_bundle(
        tmp_path / "sealed/measurements.age",
        tmp_path / "verified",
        identity,
        run,
        private_scan=lambda _: "pass",
    )
    assert state["accepted"] is True  # Evidence authenticated and scanned, not a run verdict.
    assert state["measurement_result"] == result
    assert state["measurement_acceptance"] == "ineligible"
    assert contents(tmp_path / "verified/raw") == original
    assert json.loads((tmp_path / "verified/state.json").read_bytes()) == state


@pytest.mark.parametrize("result", ["fail", "incomplete", "cancelled"])
def test_recorder_saves_legal_incomplete_failure_without_fabricating_endings(tmp_path, result):
    source = tmp_path / "source"
    run = failed_files(source, result)
    recorder = recording.Recorder(format_version=4)
    original = contents(source)
    for name in evidence.FILES[:-1]:
        recorder.append_bytes(name, original[name])
    recorder.finish(tmp_path / "raw", run)
    for name in evidence.FILES[:-1]:
        assert (tmp_path / "raw" / name).read_bytes() == original[name]
    assert json.loads((tmp_path / "raw/run-manifest.json").read_bytes())["result"] == result


@pytest.mark.parametrize("premature", [False, True])
def test_pass_still_requires_actual_complete_native_lifecycle(tmp_path, premature):
    source = tmp_path / "source"
    run = failed_files(source, "pass", premature=premature)
    with pytest.raises(evidence.EvidenceError, match="native_release_unconfirmed"):
        evidence.validate_files(contents(source), run)
    recorder = recording.Recorder(format_version=4)
    for name in evidence.FILES[:-1]:
        recorder.append_bytes(name, (source / name).read_bytes())
    with pytest.raises(evidence.EvidenceError, match="native_release_unconfirmed"):
        recorder.finish(tmp_path / "raw", run)
    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize(
    "damage", ["unknown_field", "wrong_type", "known_bytes", "invalid_json", "empty", "sensitive"]
)
def test_failed_result_does_not_relax_schema_or_public_scanning(tmp_path, damage):
    source = tmp_path / "source"
    run = failed_files(source, "fail")
    files = contents(source)
    row = native_row(3, "driver_finished")
    if damage == "unknown_field":
        row["extra"] = 0
    elif damage == "wrong_type":
        row["at_s"] = "0.03"
    elif damage == "known_bytes":
        row["internal_read_bytes"] = 0
    elif damage == "sensitive":
        run["job_id"] = "sk-proj-" + "a" * 24
        files["run-manifest.json"] = json.dumps(run).encode()
    if damage == "invalid_json":
        files["storage-phases.jsonl"] += b"{\n"
    elif damage == "empty":
        files["release-events.jsonl"] = b""
    elif damage != "sensitive":
        files["storage-phases.jsonl"] += (json.dumps(row) + "\n").encode()
    code = {
        "unknown_field": "invalid_fields",
        "wrong_type": "invalid_number",
        "known_bytes": "native_bytes_unknown_required",
        "invalid_json": "invalid_json",
        "empty": "file_size_limit",
        "sensitive": "sensitive_string",
    }[damage]
    with pytest.raises(evidence.EvidenceError, match=code):
        evidence.validate_files(files, run)


def test_failed_result_keeps_size_limit_and_frozen_identity(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run = failed_files(source, "incomplete")
    files = contents(source)
    expected = {**run, "tool_sha256": "ab" * 32}
    with pytest.raises(evidence.EvidenceError, match="run_identity_mismatch"):
        evidence.validate_files(files, expected)
    monkeypatch.setattr(evidence, "MAX_FILE", max(map(len, files.values())) - 1)
    with pytest.raises(evidence.EvidenceError, match="file_size_limit"):
        evidence.validate_files(files, run)


def test_failure_archive_never_bypasses_private_scan(tmp_path):
    source = tmp_path / "source"
    run = failed_files(source, "cancelled")
    identity = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    state = evidence.open_bundle(
        tmp_path / "sealed/measurements.age",
        tmp_path / "verified",
        identity,
        run,
        private_scan=lambda _: "unavailable",
    )
    assert not state["accepted"]
    assert state["state"] == "encrypted_pending_verification"
    assert state["measurement_acceptance"] == "ineligible"


def test_authenticated_pass_is_not_a_final_measurement_acceptance(tmp_path):
    source = tmp_path / "source"
    run = failed_files(source, "pass")
    with (source / "storage-phases.jsonl").open("ab") as stream:
        for row in (
            native_row(3, "driver_finished"),
            native_row(4, "connection_closed", connection=1),
            native_row(5, "finished"),
        ):
            stream.write((json.dumps(row) + "\n").encode())
    identity = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    state = evidence.open_bundle(
        tmp_path / "sealed/measurements.age",
        tmp_path / "verified",
        identity,
        run,
        private_scan=lambda _: "pass",
    )
    assert state["accepted"]
    assert state["measurement_result"] == "pass"
    assert state["measurement_acceptance"] == "not_evaluated"


def formal_files(directory, *, arm="candidate", phase="mechanism", result="pass"):
    run = manifest_v4(directory)
    run.update(method="legal_storage_v1", phase=phase, arm=arm, result=result)
    run["source_commit"] = {
        "baseline": run["baseline_reference"],
        "original": run["reference"],
        "none": run["candidate"],
        "candidate": run["candidate"],
    }[arm]
    run["execution"]["position"] = run["execution"]["order"].index(arm)
    save_manifest(directory, run)
    business = json.loads((directory / "requests.jsonl").read_bytes())
    business["finished_s"] = 10
    backup = {**business, "id": 2, "kind": "backup", "finished_s": 1}
    (directory / "requests.jsonl").write_text(
        json.dumps(business) + "\n" + json.dumps(backup) + "\n", encoding="utf-8"
    )
    (directory / "release-events.jsonl").write_text(
        json.dumps({"id": 1, "at_s": 1, "resource": "process", "event": "exit_verified"}) + "\n",
        encoding="utf-8",
    )
    return run


@pytest.mark.parametrize("phase", ["mechanism", "matrix"])
@pytest.mark.parametrize("arm", ["candidate", "original"])
def test_formal_pass_cannot_omit_all_native_validation(tmp_path, phase, arm):
    source = tmp_path / "source"
    run = formal_files(source, phase=phase, arm=arm)
    with pytest.raises(evidence.EvidenceError, match="native_validation_missing"):
        evidence.validate_files(contents(source), run)


def test_formal_pass_with_complete_native_validation_remains_eligible(tmp_path):
    source = tmp_path / "source"
    run = formal_files(source)
    rows = [
        native_row(1, "started"),
        native_row(2, "connection_attached", connection=1),
        native_row(3, "driver_finished"),
        native_row(4, "connection_closed", connection=1),
        native_row(5, "finished"),
    ]
    (source / "storage-phases.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    evidence.validate_files(contents(source), run)


@pytest.mark.parametrize("result", ["fail", "incomplete", "cancelled"])
def test_formal_failure_can_preserve_absent_native_validation(tmp_path, result):
    source = tmp_path / "source"
    run = formal_files(source, result=result)
    evidence.validate_files(contents(source), run)


@pytest.mark.parametrize("arm", ["baseline", "none"])
def test_nonbackup_formal_pass_does_not_require_native_validation(tmp_path, arm):
    source = tmp_path / "source"
    run = formal_files(source, arm=arm, phase="baseline" if arm == "baseline" else "mechanism")
    evidence.validate_files(contents(source), run)


def test_historical_fixture_pass_does_not_gain_native_coverage_requirement(tmp_path):
    source = tmp_path / "source"
    run = manifest_v4(source)
    run["result"] = "pass"
    save_manifest(source, run)
    evidence.validate_files(contents(source), run)
