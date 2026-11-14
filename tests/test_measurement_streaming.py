"""Complete fictional line scans with bounded new-format scan summaries."""

import io
import json
import tracemalloc
import zipfile

import pyrage
import pytest
from tests.test_measurement_evidence import evidence, fixture_files
from tests.test_measurement_manifest import contents, manifest_v2, manifest_v3


def legacy_report(files, scanner):
    return [
        {"file": name, "line": number, "category": "schema_validated_fictional_numeric"}
        for name in evidence.FILES
        for number, line in enumerate(files[name].decode("utf-8").splitlines(), 1)
        if scanner(line)
    ]


def summarize(report):
    result = []
    for name in evidence.FILES:
        lines = [row["line"] for row in report if row["file"] == name]
        if lines:
            result.append(
                {
                    "file": name,
                    "category": "schema_validated_fictional_numeric",
                    "count": len(lines),
                    "first_line": lines[0],
                    "last_line": lines[-1],
                }
            )
    return result


@pytest.mark.parametrize("factory", [fixture_files, manifest_v2])
def test_historical_formats_keep_exact_line_reports(tmp_path, factory):
    source = tmp_path / "source"
    run = factory(source)
    files = contents(source)
    files["resources.jsonl"] = files["resources.jsonl"].replace(b"\n", b"\r\n") * 4
    assert evidence.validate_files(files, run) == legacy_report(files, evidence.public_scanner())


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_streaming_preserves_legacy_line_splitting_across_chunks(tmp_path, separator):
    source = tmp_path / "source"
    run = manifest_v2(source)
    files = contents(source)
    row = files["resources.jsonl"].rstrip(b"\r\n")
    files["resources.jsonl"] = (row + separator.encode()) * 200
    assert evidence.validate_files(files, run) == legacy_report(files, evidence.public_scanner())


def test_streaming_rejects_overlong_line_before_parsing_it(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run = manifest_v3(source)
    files = contents(source)
    monkeypatch.setattr(evidence, "MAX_LINE", 512)
    row = files["storage-phases.jsonl"].rstrip(b"\r\n")
    row += b" " * (evidence.MAX_LINE - len(row))
    files["storage-phases.jsonl"] = row + b"\r\n"
    evidence.validate_files(files, run)
    files["storage-phases.jsonl"] = row + b" \r\n"
    with pytest.raises(evidence.EvidenceError, match="line_size_limit"):
        evidence.validate_files(files, run)


@pytest.mark.parametrize("report_format", [True, 0, 3, "1"])
def test_unknown_report_formats_are_rejected(tmp_path, report_format):
    source = tmp_path / "source"
    run = manifest_v3(source)
    with pytest.raises(evidence.EvidenceError, match="unsupported_report_format"):
        evidence.validate_files(contents(source), run, report_format=report_format)


def test_new_report_counts_every_numeric_exception_and_keeps_first_last(tmp_path):
    source = tmp_path / "source"
    run = manifest_v3(source)
    files = contents(source)
    files["resources.jsonl"] *= 37
    report = evidence.validate_files(files, run)
    assert report == summarize(legacy_report(files, evidence.public_scanner()))
    assert len(report) <= len(evidence.FILES)
    resource = next(item for item in report if item["file"] == "resources.jsonl")
    assert resource["count"] == 37
    assert resource["first_line"] == 1 and resource["last_line"] == 37


@pytest.mark.parametrize("position", [0, 23])
@pytest.mark.parametrize("failure", ["unknown_field", "sensitive_string", "other_category"])
def test_every_line_is_validated_and_scanned_including_last(
    tmp_path, monkeypatch, position, failure
):
    source = tmp_path / "source"
    run = manifest_v3(source)
    files = contents(source)
    row = json.loads(files["storage-phases.jsonl"])
    records = [row.copy() for _ in range(24)]
    if failure == "unknown_field":
        records[position]["unapproved"] = "fictional_secret"
    else:
        records[position]["outcome"] = "failed"

        def scanner(text):
            if failure == "sensitive_string" and text == "failed":
                return {"synthetic-private-string"}
            if failure == "other_category" and '"outcome": "failed"' in text:
                return {"synthetic-secret-category"}
            return set()

        monkeypatch.setattr(evidence, "public_scanner", lambda: scanner)
    files["storage-phases.jsonl"] = b"".join(
        (json.dumps(record) + "\n").encode() for record in records
    )
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(files, run)


def test_new_archive_format_roundtrip_preserves_all_raw_bytes(tmp_path):
    source = tmp_path / "source"
    run = manifest_v3(source)
    path = source / "resources.jsonl"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") * 9)
    original = contents(source)
    identity = evidence.native_identity().generate()
    receipt = evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    assert receipt["format"] == 1
    cipher = tmp_path / "sealed/measurements.age"
    with zipfile.ZipFile(io.BytesIO(pyrage.decrypt(cipher.read_bytes(), [identity]))) as archive:
        manifest = json.loads(archive.read("archive-manifest.json"))
    assert manifest["format"] == 2
    assert manifest["generic_exceptions"] == summarize(
        legacy_report(original, evidence.public_scanner())
    )
    result = evidence.open_bundle(
        cipher, tmp_path / "opened", identity, run, private_scan=lambda paths: "pass"
    )
    assert result["accepted"]
    assert contents(tmp_path / "opened/raw") == original


def archive_with_report(tmp_path, source, run, identity, *, inner_format, report):
    """Simulate an authenticated historical archive, retaining every original file."""
    evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    raw = pyrage.decrypt((tmp_path / "sealed/measurements.age").read_bytes(), [identity])
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(output, "w") as new:
        for name in old.namelist():
            data = old.read(name)
            if name == "archive-manifest.json":
                manifest = json.loads(data)
                manifest.update(format=inner_format, generic_exceptions=report)
                data = json.dumps(manifest).encode()
            new.writestr(name, data)
    path = tmp_path / "rewrapped.age"
    path.write_bytes(pyrage.encrypt(output.getvalue(), [identity.to_public()]))
    return path


def test_historical_inner_format_one_with_run_three_still_verifies(tmp_path):
    source = tmp_path / "source"
    run = manifest_v3(source)
    original = contents(source)
    identity = evidence.native_identity().generate()
    report = legacy_report(original, evidence.public_scanner())
    cipher = archive_with_report(tmp_path, source, run, identity, inner_format=1, report=report)
    result = evidence.open_bundle(
        cipher, tmp_path / "opened", identity, run, private_scan=lambda paths: "pass"
    )
    assert result["accepted"] and result["generic_exceptions"] == report
    assert contents(tmp_path / "opened/raw") == original


@pytest.mark.parametrize("change", ["count", "unknown", "old_report", "format"])
def test_authenticated_summary_tampering_is_rejected_before_extraction(tmp_path, change):
    source = tmp_path / "source"
    run = manifest_v3(source)
    identity = evidence.native_identity().generate()
    legacy = legacy_report(contents(source), evidence.public_scanner())
    report = summarize(legacy)
    inner_format = 2
    if change == "count":
        report[0]["count"] += 1
    elif change == "unknown":
        report[0]["unapproved"] = 1
    elif change == "old_report":
        report = legacy
    else:
        inner_format = 3
    cipher = archive_with_report(
        tmp_path, source, run, identity, inner_format=inner_format, report=report
    )
    with pytest.raises(evidence.EvidenceError):
        evidence.open_bundle(
            cipher, tmp_path / "opened", identity, run, private_scan=lambda paths: "pass"
        )
    assert not (tmp_path / "opened/raw").exists()


def test_v3_validation_does_not_retain_each_parsed_record_or_exception(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run = manifest_v3(source)
    files = contents(source)
    count = 8000
    files["storage-phases.jsonl"] *= count
    # Isolate validation allocations and scanner-call coverage from regex costs.
    scanned_rows = 0

    def scanner(text):
        nonlocal scanned_rows
        if text.startswith('{"id"'):
            scanned_rows += 1
            return {"possible-account-number"}
        return set()

    monkeypatch.setattr(evidence, "public_scanner", lambda: scanner)
    tracemalloc.start()
    try:
        report = evidence.validate_files(files, run)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert scanned_rows == count + 2  # requests + release, besides the storage rows
    storage = next(item for item in report if item["file"] == "storage-phases.jsonl")
    assert storage["count"] == count
    assert storage["first_line"] == 1 and storage["last_line"] == count
    # Allocation regression only, not a latency or formal performance threshold.
    # Source bytes preexist tracing; a line parser and five summaries fit easily.
    assert peak < 2 * 1024 * 1024
