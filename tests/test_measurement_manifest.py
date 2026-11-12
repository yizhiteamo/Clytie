"""Versioned fictional run identities; no deployment data or private scanner access."""

import copy
import json

import pytest
from tests.test_measurement_evidence import evidence, fixture_files


def manifest_v2(directory):
    run = fixture_files(directory)
    ledger = dict.fromkeys(
        ("inbound", "record", "invocation", "intent", "attempt", "dedupe", "audit"), 0
    )
    run.update(
        format=2,
        method="legal_fixture_comparison_v1",
        reference="b40bbbc31cfb2ff012a151d0e0e9c79acf07d903",
        baseline_reference="05ec93c7c4ae972b7475911b0aec9db57bb00390",
        source_commit=run["candidate"],
        fixture_version="legal_memory_v1",
        fixture_sha256="bd" * 32,
        tool_sha256="ce" * 32,
        environment={
            "cpu_count": 4,
            "os": "Windows",
            "release_sha256": "df" * 32,
            "machine_sha256": "ef" * 32,
        },
        execution={
            "order": ["baseline", "none", "original", "candidate"],
            "position": 3,
            "warmup_requests": 2,
        },
        metrics={
            "logical": {
                "records": 8,
                "revisions": 8,
                "content_bytes": 8 * 4096,
                "content_bytes_per_record": 4096,
                "content_length_distribution": {
                    "min_bytes": 4096,
                    "max_bytes": 4096,
                    "total_bytes": 8 * 4096,
                    "records": 8,
                },
                "file_count": 2,
                "file_bytes": 64,
                "provider_files": 1,
                "provider_bytes": 32,
                "asset_registrations": 1,
                "asset_bytes": 32,
                "ledger_rows": ledger.copy(),
            },
            "ledger_rows": ledger.copy(),
            "memory_database_bytes": 64 * 1024,
            "ledger_database_bytes": 16 * 1024,
            "asset_file_bytes": 32,
            "plugin_file_bytes": 32,
            "backup_bytes": 0,
        },
    )
    run["load"]["backup_start_seconds"] = 0.1
    save_manifest(directory, run)
    return run


def manifest_v3(directory):
    run = manifest_v2(directory)
    run.update(format=3, fixture_version="legal_memory_v2")
    resource_path = directory / "resources.jsonl"
    resource = json.loads(resource_path.read_text())
    resource.update(logical_io_bytes=0, backup_phase="not_started")
    resource_path.write_text(json.dumps(resource) + "\n", encoding="utf-8")
    save_manifest(directory, run)
    return run


def manifest_v4(directory):
    run = manifest_v3(directory)
    run["format"] = 4
    save_manifest(directory, run)
    return run


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_all_manifest_versions_preserve_raw_bytes_and_archive_version(tmp_path, version):
    import io
    import zipfile

    import pyrage

    source = tmp_path / "source"
    run = {1: fixture_files, 2: manifest_v2, 3: manifest_v3, 4: manifest_v4}[version](source)
    original = contents(source)
    identity = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    ciphertext = tmp_path / "sealed/measurements.age"
    with zipfile.ZipFile(
        io.BytesIO(pyrage.decrypt(ciphertext.read_bytes(), [identity]))
    ) as archive:
        inner = json.loads(archive.read("archive-manifest.json"))
        assert inner["format"] == (2 if version in {3, 4} else 1)
    evidence.open_bundle(
        ciphertext, tmp_path / "opened", identity, run, private_scan=lambda _: "pass"
    )
    assert contents(tmp_path / "opened/raw") == original


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_native_event_requires_format_four_and_unknown_internal_bytes(version):
    row = dict(
        id=1,
        request_id=0,
        phase="native_validation",
        validation_id=1,
        connection_id=0,
        event="started",
        at_s=0,
        outcome="ok",
        internal_read_bytes=None,
        boundary="native_checks",
    )
    if version == 4:
        evidence.validate_record("storage-phases.jsonl", row, format_version=version)
        for value in (0, 4096, "unknown"):
            with pytest.raises(evidence.EvidenceError, match="native_bytes_unknown_required"):
                evidence.validate_record(
                    "storage-phases.jsonl",
                    {**row, "internal_read_bytes": value},
                    format_version=version,
                )
    else:
        with pytest.raises(evidence.EvidenceError, match="unsupported_native_event"):
            evidence.validate_record("storage-phases.jsonl", row, format_version=version)


def save_manifest(directory, run):
    (directory / "run-manifest.json").write_text(json.dumps(run, indent=2), encoding="utf-8")


def contents(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def test_v2_age_roundtrip_preserves_container_v1_and_frozen_identity(tmp_path):
    source = tmp_path / "source"
    run = manifest_v2(source)
    identity = evidence.native_identity().generate()
    receipt = evidence.seal(source, tmp_path / "encrypted", str(identity.to_public()), run)
    assert receipt["format"] == 1
    result = evidence.open_bundle(
        tmp_path / "encrypted/measurements.age",
        tmp_path / "opened",
        identity,
        run,
        private_scan=lambda files: "pass",
    )
    assert result["accepted"] is True
    assert contents(tmp_path / "opened/raw") == contents(source)


def test_v2_result_and_observed_metrics_can_change_after_freezing(tmp_path):
    source = tmp_path / "source"
    expected = manifest_v2(source)
    actual = copy.deepcopy(expected)
    actual["result"] = "pass"
    actual["metrics"]["memory_database_bytes"] += 4096
    save_manifest(source, actual)
    evidence.validate_files(contents(source), expected)


def test_v2_logical_workload_cannot_change_after_freezing(tmp_path):
    source = tmp_path / "source"
    expected = manifest_v2(source)
    actual = copy.deepcopy(expected)
    actual["metrics"]["logical"]["records"] += 1
    actual["metrics"]["logical"]["revisions"] += 1
    actual["metrics"]["logical"]["content_bytes"] += 4096
    distribution = actual["metrics"]["logical"]["content_length_distribution"]
    distribution["records"] += 1
    distribution["total_bytes"] += 4096
    save_manifest(source, actual)
    with pytest.raises(evidence.EvidenceError, match="run_workload_mismatch"):
        evidence.validate_files(contents(source), expected)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate", "de" * 20),
        ("source_commit", "de" * 20),
        ("reference", "de" * 20),
        ("baseline_reference", "de" * 20),
        ("fixture_sha256", "de" * 32),
        ("tool_sha256", "de" * 32),
        ("fixture_version", "other"),
        ("method", "legal_storage_v1"),
        ("plan_sha256", "de" * 32),
        ("python", "3.11.1"),
        ("sqlite", "3.40.1"),
        ("seed", 74),
        ("run_id", 124),
        ("job_id", "other_job"),
        ("attempt", 2),
        ("platform", "Linux"),
        ("scale", "small_text"),
        ("group", 2),
        ("arm", "original"),
        ("phase", "matrix"),
        ("source", "other"),
        ("format", 1),
        ("unexpected", 1),
    ],
)
def test_v2_changed_identity_is_rejected(tmp_path, field, value):
    source = tmp_path / "source"
    expected = manifest_v2(source)
    evidence.validate_files(contents(source), expected)
    actual = copy.deepcopy(expected)
    actual[field] = value
    save_manifest(source, actual)
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(contents(source), expected)


@pytest.mark.parametrize(
    ("field", "key", "value"),
    [
        ("parameters", "concurrency", 2),
        ("load", "window_seconds", 121),
        ("load", "interval_seconds", 0.2),
        ("load", "request_count", 1201),
        ("load", "maximum_seconds", 241),
        ("load", "backup_start_seconds", 0.2),
        ("execution", "warmup_requests", 3),
        ("execution", "order", ["none", "baseline", "original", "candidate"]),
        ("environment", "cpu_count", 8),
        ("environment", "release_sha256", "aa" * 32),
        ("environment", "machine_sha256", "bb" * 32),
    ],
)
def test_v2_nested_frozen_identity_is_compared(tmp_path, field, key, value):
    source = tmp_path / "source"
    expected = manifest_v2(source)
    evidence.validate_files(contents(source), expected)
    actual = copy.deepcopy(expected)
    actual[field][key] = value
    save_manifest(source, actual)
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(contents(source), expected)


@pytest.mark.parametrize("phase", ["baseline", "fixture", "matrix", "mechanism"])
def test_v2_old_source_is_only_accepted_for_baseline_or_fixture(tmp_path, phase):
    run = manifest_v2(tmp_path / "source")
    run.update(arm="baseline", phase=phase, source_commit=run["baseline_reference"])
    run["execution"]["position"] = 0
    if phase != "fixture":
        run["method"] = "legal_storage_v1"
    if phase in {"baseline", "fixture"}:
        evidence.validate_run(run)
    else:
        with pytest.raises(evidence.EvidenceError):
            evidence.validate_run(run)


@pytest.mark.parametrize("field", ["environment", "execution", "metrics", "load", "parameters"])
def test_v2_unknown_nested_fields_are_never_numeric_exceptions(tmp_path, field):
    run = manifest_v2(tmp_path / "source")
    run[field]["unexpected"] = 123
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_run(run)


@pytest.mark.parametrize("value", [True, -1, float("inf"), "123", None])
def test_v2_metric_leaves_must_be_nonnegative_finite_integers(tmp_path, value):
    run = manifest_v2(tmp_path / "source")
    run["metrics"]["memory_database_bytes"] = value
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_run(run)


@pytest.mark.parametrize(
    "keys",
    [
        ("metrics", "logical"),
        ("metrics", "ledger_rows"),
        ("metrics", "logical", "ledger_rows"),
        ("metrics", "logical", "content_length_distribution"),
    ],
)
def test_v2_nested_metric_objects_are_closed(tmp_path, keys):
    run = manifest_v2(tmp_path / "source")
    evidence.validate_run(run)
    obj = run
    for key in keys:
        obj = obj[key]
    obj["unapproved"] = 1
    with pytest.raises(evidence.EvidenceError, match="invalid_fields"):
        evidence.validate_run(run)


def test_v2_valid_candidate_and_environment_substitution_fails_before_extraction(tmp_path):
    source = tmp_path / "source"
    run = manifest_v2(source)
    identity = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "encrypted", str(identity.to_public()), run)
    expected = copy.deepcopy(run)
    expected["candidate"] = expected["source_commit"] = "ed" * 20
    expected["environment"]["cpu_count"] = 8
    evidence.validate_run(expected)
    with pytest.raises(evidence.EvidenceError, match="verification_failed"):
        evidence.open_bundle(
            tmp_path / "encrypted/measurements.age",
            tmp_path / "opened",
            identity,
            expected,
            private_scan=lambda files: "pass",
        )
    assert not (tmp_path / "opened/raw").exists()


def test_cross_version_identity_never_uses_the_historical_field_subset(tmp_path):
    source = tmp_path / "source"
    current = manifest_v2(source)
    expected = fixture_files(tmp_path / "historical")
    for key in evidence.IDENTITY_FIELDS:
        if key != "load":
            expected[key] = copy.deepcopy(current[key])
    evidence.validate_run(expected)
    with pytest.raises(evidence.EvidenceError, match="run_identity_mismatch"):
        evidence.validate_files(contents(source), expected)


@pytest.mark.parametrize("arm", ["baseline", "none", "original", "candidate"])
def test_each_arm_requires_its_own_source_commit(tmp_path, arm):
    run = manifest_v2(tmp_path / "source")
    run["arm"] = arm
    run["execution"]["position"] = run["execution"]["order"].index(arm)
    run["source_commit"] = {
        "baseline": run["baseline_reference"],
        "original": run["reference"],
        "none": run["candidate"],
        "candidate": run["candidate"],
    }[arm]
    evidence.validate_run(run)
    run["source_commit"] = "dd" * 20
    with pytest.raises(evidence.EvidenceError, match="source_commit_mismatch"):
        evidence.validate_run(run)


@pytest.mark.parametrize("field", ["python", "sqlite", "source_commit", "fixture_sha256"])
def test_expected_manifest_itself_cannot_contain_unknown_or_invalid_identity(tmp_path, field):
    source = tmp_path / "source"
    expected = manifest_v2(source)
    expected[field] = "unapproved"
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(contents(source), expected)


@pytest.mark.parametrize("format_version", [1, 2, 3])
def test_resource_record_schema_is_bound_to_manifest_format(tmp_path, format_version):
    source = tmp_path / "source"
    run = {1: fixture_files, 2: manifest_v2, 3: manifest_v3}[format_version](source)
    evidence.validate_files(contents(source), run)
    path = source / "resources.jsonl"
    row = json.loads(path.read_text())
    evidence.validate_record("resources.jsonl", row)
    if format_version in {1, 2}:
        row.update(logical_io_bytes=0, backup_phase="not_started")
    else:
        del row["logical_io_bytes"]
        del row["backup_phase"]
    evidence.validate_record("resources.jsonl", row)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="invalid_fields"):
        evidence.validate_files(contents(source), run)


@pytest.mark.parametrize("field", ["logical_io_bytes", "backup_phase"])
def test_partial_resource_extensions_are_not_optional(tmp_path, field):
    source = tmp_path / "source"
    run = manifest_v3(source)
    path = source / "resources.jsonl"
    row = json.loads(path.read_text())
    del row[field]
    with pytest.raises(evidence.EvidenceError, match="invalid_fields"):
        evidence.validate_record("resources.jsonl", row)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="invalid_fields"):
        evidence.validate_files(contents(source), run)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("logical_io_bytes", True),
        ("logical_io_bytes", -1),
        ("logical_io_bytes", 0.5),
        ("logical_io_bytes", "123"),
        ("logical_io_bytes", float("inf")),
        ("logical_io_bytes", None),
        ("backup_phase", "unknown"),
        ("backup_phase", "private/path"),
        ("backup_phase", 0),
        ("backup_phase", None),
        ("unexpected", 0),
    ],
)
def test_resource_v3_tamper_is_rejected(tmp_path, field, value):
    source = tmp_path / "source"
    run = manifest_v3(source)
    path = source / "resources.jsonl"
    row = json.loads(path.read_text())
    row[field] = value
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_record("resources.jsonl", row)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(contents(source), run)


def test_resource_phase_enum_matches_real_journal_phases(tmp_path):
    from clytie.backup.journal import PHASES

    source = tmp_path / "source"
    run = manifest_v3(source)
    path = source / "resources.jsonl"
    row = json.loads(path.read_text())
    for phase in PHASES | {"not_started", "not_applicable"}:
        row["backup_phase"] = phase
        evidence.validate_record("resources.jsonl", row)
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        evidence.validate_files(contents(source), run)
    assert evidence.BACKUP_PHASES == PHASES | {"not_started", "not_applicable"}


def test_resource_mixed_versions_in_one_file_are_rejected(tmp_path):
    source = tmp_path / "source"
    run = manifest_v3(source)
    path = source / "resources.jsonl"
    row = json.loads(path.read_text())
    legacy = {
        key: value for key, value in row.items() if key not in {"logical_io_bytes", "backup_phase"}
    }
    path.write_text(json.dumps(row) + "\n" + json.dumps(legacy) + "\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="invalid_fields"):
        evidence.validate_files(contents(source), run)


def test_resource_unmetered_io_is_null_only_without_a_rate(tmp_path):
    source = tmp_path / "source"
    run = manifest_v3(source)
    path = source / "resources.jsonl"
    row = json.loads(path.read_text())
    row.update(rate="none", logical_io_bytes=None)
    evidence.validate_record("resources.jsonl", row)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    evidence.validate_files(contents(source), run)
    for rate in ("normal", "busy"):
        row["rate"] = rate
        with pytest.raises(evidence.EvidenceError, match="invalid_number"):
            evidence.validate_record("resources.jsonl", row)


@pytest.mark.parametrize("format_version", [2, 3])
def test_fixture_version_is_fixed_for_each_manifest_format(tmp_path, format_version):
    source = tmp_path / "source"
    run = {2: manifest_v2, 3: manifest_v3}[format_version](source)
    evidence.validate_files(contents(source), run)
    run["fixture_version"] = "legal_memory_v2" if format_version == 2 else "legal_memory_v1"
    with pytest.raises(evidence.EvidenceError, match="invalid_enum"):
        evidence.validate_run(run)


def test_v3_real_age_roundtrip_preserves_raw_and_container_format(tmp_path):
    source = tmp_path / "source"
    run = manifest_v3(source)
    identity = evidence.native_identity().generate()
    receipt = evidence.seal(source, tmp_path / "encrypted", str(identity.to_public()), run)
    assert receipt["format"] == 1
    result = evidence.open_bundle(
        tmp_path / "encrypted/measurements.age",
        tmp_path / "opened",
        identity,
        run,
        private_scan=lambda files: "pass",
    )
    assert result["accepted"] is True
    assert contents(tmp_path / "opened/raw") == contents(source)


@pytest.mark.parametrize("field", ["fixture_sha256", "tool_sha256", "python", "execution"])
def test_v3_frozen_fields_remain_identity_checked(tmp_path, field):
    source = tmp_path / "source"
    expected = manifest_v3(source)
    actual = copy.deepcopy(expected)
    if field.endswith("sha256"):
        actual[field] = "dd" * 32
    elif field == "python":
        actual[field] = "3.11.1"
    else:
        actual[field]["warmup_requests"] += 1
    evidence.validate_run(actual)
    save_manifest(source, actual)
    with pytest.raises(evidence.EvidenceError, match="run_identity_mismatch"):
        evidence.validate_files(contents(source), expected)


def test_v3_logical_workload_remains_frozen(tmp_path):
    source = tmp_path / "source"
    expected = manifest_v3(source)
    actual = copy.deepcopy(expected)
    actual["metrics"]["logical"]["revisions"] += 1
    evidence.validate_run(actual)
    save_manifest(source, actual)
    with pytest.raises(evidence.EvidenceError, match="run_workload_mismatch"):
        evidence.validate_files(contents(source), expected)


@pytest.mark.parametrize("format_version", [1, 2, 3])
def test_manifest_format_controls_file_limit_at_exact_boundary(
    tmp_path, monkeypatch, format_version
):
    source = tmp_path / "source"
    run = {1: fixture_files, 2: manifest_v2, 3: manifest_v3}[format_version](source)
    files = contents(source)
    legacy_limit = max(map(len, files.values())) + 64
    monkeypatch.setattr(evidence, "LEGACY_MAX_FILE", legacy_limit)
    monkeypatch.setattr(evidence, "MAX_FILE", legacy_limit * 2)
    selected_limit = legacy_limit * (2 if format_version == 3 else 1)
    raw = files["run-manifest.json"]
    files["run-manifest.json"] = raw + b" " * (selected_limit - len(raw))
    evidence.validate_files(files, run)
    files["run-manifest.json"] += b" "
    with pytest.raises(evidence.EvidenceError, match="file_size_limit"):
        evidence.validate_files(files, run)


@pytest.mark.parametrize("format_version", [1, 2, 3])
def test_resource_file_over_legacy_limit_is_allowed_only_in_v3(
    tmp_path, monkeypatch, format_version
):
    source = tmp_path / "source"
    run = {1: fixture_files, 2: manifest_v2, 3: manifest_v3}[format_version](source)
    files = contents(source)
    legacy_limit = max(map(len, files.values())) + 64
    monkeypatch.setattr(evidence, "LEGACY_MAX_FILE", legacy_limit)
    monkeypatch.setattr(evidence, "MAX_FILE", legacy_limit * 2)
    row = files["resources.jsonl"]
    files["resources.jsonl"] = row * (legacy_limit // len(row) + 1)
    assert legacy_limit < len(files["resources.jsonl"]) <= evidence.MAX_FILE
    if format_version == 3:
        evidence.validate_files(files, run)
    else:
        with pytest.raises(evidence.EvidenceError, match="file_size_limit"):
            evidence.validate_files(files, run)


def test_evidence_capacity_constants_keep_fixed_archive_bound():
    assert evidence.LEGACY_MAX_FILE == 64 * 1024 * 1024
    assert evidence.MAX_FILE == 256 * 1024 * 1024
    assert evidence.MAX_TOTAL == len(evidence.FILES) * evidence.MAX_FILE + 1024 * 1024
