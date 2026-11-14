"""Fictional gate inputs; no private key files, network, or formal executions."""

import copy
import json
import zipfile
from types import SimpleNamespace

import pytest
from tests.measurement_tools import load
from tests.test_measurement_evidence import fixture_files
from tests.test_measurement_mechanism_plan import materials

load("measurement_plan_ingress")
load("measurement_mechanism_validation")
load("measurement_cancel_validation")
gate = load("measurement_mechanism_acceptance")
REAL_OPEN_BUNDLE = gate.evidence.open_bundle
REAL_READ_SOURCES = gate.evidence._read_sources
REAL_AGGREGATE = gate.validation.aggregate
REAL_VALIDATE_SEALED = gate.transport.validate_sealed


def save(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value).encode())
    return dict(path=name, sha256=gate.evidence._hash(path))


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    frozen, source, tools, runtimes, workflow = materials()
    workflow["request_commit"] = "a" * 40
    for name in ("repository", "version", "tools"):
        (tmp_path / name).mkdir()
    source_path = tmp_path / "version/source/src/clytie/__init__.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# fictional source\n# second line\n")
    monkeypatch.setattr(gate.mechanism, "verify_materials", lambda *a: (source, tools))
    monkeypatch.setattr(gate, "_provenance", lambda *a: workflow)
    monkeypatch.setattr(gate.ingress, "public_scan", lambda raw: None)
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"fictional test source"),
    )
    reports = dict(
        mechanism_acceptance="not_evaluated",
        requirements={"borrower_seriality": dict(status="unverified", reasons=["handoff_unknown"])},
    )
    monkeypatch.setattr(gate.validation, "aggregate", lambda *a: copy.deepcopy(reports))
    monkeypatch.setattr(gate.transport, "validate_sealed", lambda *a: None)
    opened = []

    def open_bundle(cipher, target, identity, expected, *, private_scan):
        assert identity == "fictional_injected_identity"
        opened.append(expected)
        target.mkdir()
        (target / "raw").mkdir()
        assert private_scan([]) == "pass"
        return dict(
            accepted=True,
            measurement_result=("cancelled" if expected["scenario"] == "cancel" else "pass"),
        )

    monkeypatch.setattr(gate.evidence, "open_bundle", open_bundle)
    monkeypatch.setattr(gate.evidence, "_read_sources", lambda *a, **k: {})
    monkeypatch.setattr(
        gate.cancellation, "verify_cancel_evidence", lambda *a: {"planned_cancel_verified": True}
    )
    tests = {}
    modules = {module for group in gate.COMBINATIONS.values() for module in group}
    for platform in gate.plan.PLATFORMS:
        junit = tmp_path / (platform + ".xml")
        junit.write_text(
            "<testsuite>"
            + "".join(
                '<testcase classname="tests.' + module + '" name="test_real_path"/>'
                for module in sorted(modules)
            )
            + '<testcase classname="tests.test_backup_io_restart_http" name="'
            'test_real_daemon_restart_blocks_orphan_io_but_other_companion_chats"/>'
            + "</testsuite>"
        )
        tests[platform] = dict(
            candidate=source["candidate"],
            command=["python", "-m", "pytest"],
            environment=save(tmp_path, platform + "-env.json", dict(platform=platform)),
            results=dict(path=junit.name, sha256=gate.evidence._hash(junit)),
            result_format="junit",
            result_source=None,
            job=save(
                tmp_path,
                platform + "-job.json",
                dict(
                    id=123_456_789_012,
                    run_id=12_345_678_901,
                    head_sha=source["candidate"],
                    status="completed",
                    conclusion="success",
                    steps=[dict(name="Offline tests", conclusion="success")],
                ),
            ),
            ci=save(
                tmp_path,
                platform + "-ci.json",
                dict(
                    head_sha=source["candidate"],
                    status="completed",
                    conclusion="success",
                    id=12_345_678_901,
                    repository=dict(full_name=gate.transport.REPOSITORY),
                ),
            ),
            sources={
                "tests/" + module + ".py": gate.ingress.sha(b"fictional test source")
                for module in modules
            },
        )
    coverage = dict(
        candidate=source["candidate"],
        trust_boundary=gate.TRUST,
        reviewer="owner",
        requirements=dict(
            borrower_seriality=dict(
                reasons=["handoff_unknown"],
                judgment="covered",
                rationale="Actual combination and source path inspected.",
                tests={
                    p: ["tests.test_management_operation_slot::test_real_path"]
                    for p in gate.plan.PLATFORMS
                },
                sources=[dict(path="src/clytie/__init__.py", sha256="1" * 64, lines=[1, 2])],
            )
        ),
    )
    coverage["requirements"]["requirements.borrower_seriality"] = coverage["requirements"].pop(
        "borrower_seriality"
    )
    runs = {}
    for i, row in enumerate(frozen["runs"]):
        cipher = tmp_path / str(i) / "sealed" / "measurements.age"
        cipher.parent.mkdir(parents=True)
        cipher.write_bytes(b"fictional ciphertext " + str(i).encode())
        runs[row["execution_id"]] = dict(
            runtime=save(tmp_path, f"{i}/runtime.json", runtimes[row["platform"]]),
            ciphertext=dict(
                path=cipher.relative_to(tmp_path).as_posix(), sha256=gate.evidence._hash(cipher)
            ),
            receipt=save(tmp_path, f"{i}/sealed/receipt.json", {}),
            download=save(
                tmp_path,
                f"{i}/download.json",
                dict(
                    run_id=workflow["run_id"],
                    ciphertext_sha256=gate.evidence._hash(cipher),
                    requested_days=14,
                    accepted_days=14,
                    artifact_id=i + 1,
                    created_at="2026-01-01",
                    expires_at="2026-01-15",
                ),
            ),
        )
        entry = runs[row["execution_id"]]
        step_name = f"Execute frozen platform slot {i % 4 + 1} once and seal stable output"
        log_raw = (
            "2026-11-14T01:02:03.000Z ##[group]Run uv run --no-sync python -B -m "
            f"scripts.measurement_mechanism_workflow run-slot --slot {i % 4}\n"
            "2026-11-14T01:02:04.000Z mechanism_slot_pending_local_verification\n"
            "2026-11-14T01:02:05.000Z mechanism_stage=sealed_pending_local_verification "
            "driver_exit_confirmed=true ciphertext_saved=true "
            "artifact_upload=not_observed worker_exit=not_evaluated_here\n"
        ).encode()
        log_path = tmp_path / str(i) / "slot.txt"
        log_path.write_bytes(log_raw)
        log_zip = tmp_path / str(i) / "logs.zip"
        log_entry = row["platform"] + "/7_" + step_name + ".txt"
        with zipfile.ZipFile(log_zip, "w") as logs:
            logs.writestr(log_entry, log_raw)
        entry["execution"] = dict(
            job=save(
                tmp_path,
                f"{i}/job.json",
                dict(
                    id=i + 1,
                    run_id=workflow["run_id"],
                    head_sha=workflow["request_commit"],
                    name=workflow["jobs"][row["platform"]],
                    status="completed",
                    conclusion="success",
                    steps=[
                        dict(name=step_name, number=7, status="completed", conclusion="success")
                    ],
                ),
            ),
            log=dict(path=f"{i}/slot.txt", sha256=gate.evidence._hash(log_path)),
            archive=dict(path=f"{i}/logs.zip", sha256=gate.evidence._hash(log_zip)),
            entry=log_entry,
        )
        archive = tmp_path / str(i) / "artifact.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name in ("measurements.age", "receipt.json"):
                output.write(cipher.parent / name, name)
        entry["downloaded_zip"] = dict(
            path=f"{i}/artifact.zip", sha256=gate.evidence._hash(archive)
        )
        artifact_name = (
            f"a43-encrypted-{workflow['run_id']}-1-{row['platform'].lower()}-slot{i % 4}"
        )
        entry["artifact_metadata"] = save(
            tmp_path,
            f"{i}/metadata.json",
            dict(
                id=i + 1,
                name=artifact_name,
                expired=False,
                workflow_run=dict(id=workflow["run_id"]),
                created_at="2026-01-01",
                expires_at="2026-01-15",
            ),
        )
        download = json.loads((tmp_path / str(i) / "download.json").read_bytes())
        download.update(artifact_name=artifact_name, downloaded_sha256=gate.evidence._hash(archive))
        entry["download"] = save(tmp_path, f"{i}/download.json", download)
    record = dict(
        format=1,
        plan=save(tmp_path, "plan.json", frozen),
        repository="repository",
        version_directory="version",
        tool_root="tools",
        admission_commit="a" * 40,
        ci_run=save(tmp_path, "ci.json", {}),
        tests=tests,
        runs=runs,
        coverage=save(tmp_path, "coverage.json", coverage),
    )
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record))
    return path, record, reports, opened


def verify(path, **kwargs):
    return gate.verify_record(
        path, identity="fictional_injected_identity", private_scan=lambda paths: "pass", **kwargs
    )


def test_complete_fixed_eight_gate_preserves_aggregate_and_cancel(bundle, tmp_path):
    path, record, reports, opened = bundle
    result = verify(path, target=tmp_path / "checked")
    assert result["accepted"] is True
    assert len(opened) == 8
    assert sum(run["scenario"] == "cancel" for run in opened) == 4
    assert json.loads((tmp_path / "checked/aggregate-0.json").read_bytes()) == reports
    assert gate.verify_public_record(result) == result
    assert result["trust_boundary"] == gate.TRUST


@pytest.mark.parametrize(
    "change",
    [
        "missing_run",
        "tamper",
        "wrong_candidate",
        "failed",
        "coverage_missing",
        "skipped",
        "source_tamper",
    ],
)
def test_gate_rejects_incomplete_or_failed_evidence(bundle, change):
    path, record, reports, opened = bundle
    if change == "missing_run":
        record["runs"].pop(next(iter(record["runs"])))
    elif change == "tamper":
        (path.parent / "coverage.json").write_bytes(b"{}")
    elif change == "wrong_candidate":
        record["tests"]["Linux"]["candidate"] = "f" * 40
    elif change == "failed":
        reports["requirements"]["borrower_seriality"]["status"] = "failed"
    elif change == "coverage_missing":
        coverage = json.loads((path.parent / "coverage.json").read_bytes())
        coverage["requirements"] = {}
        record["coverage"] = save(path.parent, "coverage.json", coverage)
    elif change == "skipped":
        target = path.parent / "Linux.xml"
        target.write_text(
            '<testsuite><testcase name="test_real_path"><skipped/></testcase></testsuite>'
        )
        record["tests"]["Linux"]["results"]["sha256"] = gate.evidence._hash(target)
    else:
        record["tests"]["Linux"]["sources"]["tests/test_combination.py"] = "d" * 64
    path.write_text(json.dumps(record))
    with pytest.raises((ValueError, AssertionError)):
        verify(path)


def test_missing_identity_cannot_trust_accepted_flag(bundle):
    with pytest.raises(ValueError, match="mechanism_identity_required"):
        gate.verify_record(bundle[0])


def test_scanner_failure_leaves_incomplete_checkpoint(bundle, tmp_path):
    with pytest.raises((ValueError, AssertionError)):
        gate.verify_record(
            bundle[0],
            identity="fictional_injected_identity",
            private_scan=lambda paths: "fail",
            target=tmp_path / "failed",
        )
    assert json.loads((tmp_path / "failed/checkpoint.json").read_bytes())["accepted"] is False


def test_public_receipt_does_not_accept_wrong_binding(bundle):
    value = verify(bundle[0])
    expected = dict(value, candidate="f" * 40)
    with pytest.raises(ValueError, match="binding_mismatch"):
        gate.verify_public_record(value, expected)


def test_bundle_rejects_path_escape(tmp_path):
    record = tmp_path / "record.json"
    record.write_text("{}")
    with pytest.raises(ValueError, match="path_invalid"):
        gate.Bundle(record).file(dict(path="../private.json", sha256="f" * 64))


def test_real_archive_authentication_is_not_replaced_by_receipt(bundle, monkeypatch, tmp_path):
    # Keep the gate's real archive reader: the fake ciphertext used by the unit
    # fixture must fail age authentication even though every receipt says pass.
    monkeypatch.setattr(gate.evidence, "open_bundle", REAL_OPEN_BUNDLE)
    with pytest.raises(ValueError, match="verification_failed"):
        gate.verify_record(
            bundle[0],
            identity=gate.evidence.native_identity().generate(),
            private_scan=lambda paths: "pass",
            target=tmp_path / "auth-failed",
        )
    assert json.loads((tmp_path / "auth-failed/checkpoint.json").read_bytes())["accepted"] is False


def test_real_age_zip_roundtrip_reaches_real_aggregate_and_keeps_incomplete(
    bundle, monkeypatch, tmp_path
):
    path, record, _, _ = bundle
    monkeypatch.setattr(gate.evidence, "open_bundle", REAL_OPEN_BUNDLE)
    monkeypatch.setattr(gate.evidence, "_read_sources", REAL_READ_SOURCES)
    monkeypatch.setattr(gate.validation, "aggregate", REAL_AGGREGATE)
    monkeypatch.setattr(gate.transport, "validate_sealed", REAL_VALIDATE_SEALED)
    # This is an existing explicitly tiny encryption fixture, not formal data.
    # Crypto succeeds; the real mechanism aggregate must refuse its format.
    source = tmp_path / "crypto-fixture"
    run = fixture_files(source)
    run["result"] = "pass"
    (source / "run-manifest.json").write_bytes(json.dumps(run).encode())
    identity = gate.evidence.native_identity().generate()
    sealed = tmp_path / "real-sealed"
    gate.evidence.seal(source, sealed, str(identity.to_public()), run)
    monkeypatch.setattr(gate.mechanism, "prepare_manifest", lambda *a, **k: run)
    first = next(iter(record["runs"].values()))
    for key, name in (("ciphertext", "measurements.age"), ("receipt", "receipt.json")):
        first[key] = dict(path="real-sealed/" + name, sha256=gate.evidence._hash(sealed / name))
    archive = tmp_path / "real-artifact.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in ("measurements.age", "receipt.json"):
            output.write(sealed / name, name)
    first["downloaded_zip"] = dict(path=archive.name, sha256=gate.evidence._hash(archive))
    download = json.loads((tmp_path / first["download"]["path"]).read_bytes())
    download.update(
        ciphertext_sha256=gate.evidence._hash(sealed / "measurements.age"),
        downloaded_sha256=gate.evidence._hash(archive),
    )
    first["download"] = save(tmp_path, "real-download.json", download)
    path.write_text(json.dumps(record))
    with pytest.raises((ValueError, KeyError)):
        gate.verify_record(
            path,
            identity=identity,
            private_scan=lambda paths: "pass",
            target=tmp_path / "real-roundtrip",
        )
    state = json.loads((tmp_path / "real-roundtrip/run-0/state.json").read_bytes())
    assert state["accepted"] is True
    for original in source.iterdir():
        assert (
            tmp_path / "real-roundtrip/run-0/raw" / original.name
        ).read_bytes() == original.read_bytes()
    assert (
        json.loads((tmp_path / "real-roundtrip/checkpoint.json").read_bytes())["accepted"] is False
    )


def test_nested_failed_predicate_cannot_be_overridden(bundle):
    bundle[2]["batch_reservations"] = {"counts": {"status": "failed", "reason": "mismatch"}}
    with pytest.raises(ValueError, match="observed_mechanism_failed"):
        verify(bundle[0])


def transcript(status="PASSED", summary="1 passed", prefix=""):
    return (
        prefix
        + "tests/test_backup_io_files.py::test_chunk "
        + status
        + " [100%]\n"
        + prefix
        + "================ "
        + summary
        + " in 1.00s ================\n"
    ).encode()


def test_verbose_transcript_preserves_exact_test_identity_and_timestamp():
    assert gate.verbose_test_results(transcript(prefix="2026-11-14T12:34:56.000Z ")) == {
        "tests.test_backup_io_files::test_chunk"
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"tests/test_backup_io_files.py::test_chunk PASSED [100%]\n",
        transcript(summary="2 passed"),
        transcript(status="FAILED", summary="1 failed"),
        transcript() + transcript(),
        transcript(status="ERROR", summary="1 error"),
    ],
)
def test_verbose_transcript_rejects_missing_failed_or_incomplete_rows(raw):
    with pytest.raises(ValueError):
        gate.verbose_test_results(raw)


def test_documented_combination_cannot_be_replaced_by_any_passing_test(bundle):
    path, record, _, _ = bundle
    for platform in gate.plan.PLATFORMS:
        target = path.parent / (platform + ".xml")
        target.write_text(
            '<testsuite><testcase classname="tests.unrelated" name="test_pass"/></testsuite>'
        )
        record["tests"][platform]["results"]["sha256"] = gate.evidence._hash(target)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="required_combination_not_run"):
        verify(path)


def test_full_gate_uses_exact_offline_step_zip_bytes(bundle):
    path, record, _, _ = bundle
    modules = {module for group in gate.COMBINATIONS.values() for module in group}
    for platform, test in record["tests"].items():
        names = ["tests/" + name + ".py::test_real_path" for name in sorted(modules)]
        names.append(
            "tests/test_backup_io_restart_http.py::"
            "test_real_daemon_restart_blocks_orphan_io_but_other_companion_chats"
        )
        raw = (
            "2026-11-14T01:02:03.000Z ##[group]Run uv run --no-sync pytest -v\n"
            + "".join("2026-11-14T01:02:04.000Z " + name + " PASSED [100%]\n" for name in names)
            + f"2026-11-14T01:02:05.000Z ===== {len(names)} passed in 1.00s =====\n"
        ).encode()
        result = path.parent / (platform + ".txt")
        result.write_bytes(raw)
        archive = path.parent / (platform + "-logs.zip")
        entry = platform + "/5_Offline tests.txt"
        with zipfile.ZipFile(archive, "w") as logs:
            logs.writestr(entry, raw)
        job = json.loads((path.parent / test["job"]["path"]).read_bytes())
        job["steps"][0]["number"] = 5
        test.update(
            job=save(path.parent, platform + "-job.json", job),
            results=dict(path=result.name, sha256=gate.evidence._hash(result)),
            result_format="pytest_verbose",
            command=["uv", "run", "--no-sync", "pytest", "-v"],
            result_source=dict(
                archive=dict(path=archive.name, sha256=gate.evidence._hash(archive)), entry=entry
            ),
        )
    path.write_text(json.dumps(record))
    assert verify(path)["accepted"] is True
    # A hand-edited selected transcript no longer matches its original ZIP.
    changed = path.parent / record["tests"]["Linux"]["results"]["path"]
    changed.write_bytes(changed.read_bytes().replace(b"PASSED", b"FAILED", 1))
    record["tests"]["Linux"]["results"]["sha256"] = gate.evidence._hash(changed)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="test_log_bytes_mismatch"):
        verify(path)


def test_prepare_template_is_explicitly_pending_and_never_loads_identity(bundle, tmp_path):
    target = tmp_path / "pending"
    value = gate.prepare_record(tmp_path / "plan.json", target)
    assert len(value["runs"]) == 8
    assert value["admission_commit"] is None
    assert json.loads((target / "checkpoint.json").read_bytes()) == dict(
        accepted=False,
        state="pending_actual_evidence",
        missing=[
            "authentic_eight_archives",
            "fixed_git_provenance",
            "ci_and_combination_results",
            "human_source_coverage",
            "actual_local_scans",
        ],
    )
