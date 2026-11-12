"""Transport gates do not grant plaintext upload or invent artifact retention."""

import base64
import json
import platform
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from scripts import measurement_evidence as evidence
from scripts import measurement_transport as transport
from tests.test_measurement_evidence import fixture_files


@pytest.mark.skipif(transport.os.name != "nt", reason="Windows ACL command encoding")
def test_acl_check_uses_constant_encoded_script_and_path_variable(tmp_path, monkeypatch):
    def run(arguments, **kwargs):
        assert "-EncodedCommand" in arguments
        script = base64.b64decode(arguments[-1]).decode("utf-16-le")
        assert "Get-Acl -LiteralPath $env:CLYTIE_EVIDENCE_KEY_DIRECTORY" in script
        assert str(tmp_path) not in script
        assert kwargs["env"]["CLYTIE_EVIDENCE_KEY_DIRECTORY"] == str(tmp_path)
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(
                {
                    "current": "fictional",
                    "entries": [{"sid": "fictional", "kind": "Allow", "rights": 1}],
                }
            ).encode(),
            b"",
        )

    monkeypatch.setattr(transport.subprocess, "run", run)
    transport._check_key_directory(tmp_path)


def test_ci_production_entry_binds_actual_run_and_never_calls_private(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run = fixture_files(source)
    head = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=transport.ROOT, capture_output=True, check=True
        )
        .stdout.decode()
        .strip()
    )
    run.update(candidate=head, platform=platform.system())
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(run), encoding="utf-8")
    (source / "run-manifest.json").write_bytes(expected.read_bytes())
    identity = evidence.native_identity().generate()
    recipient = str(identity.to_public())
    public = tmp_path / "public.json"
    public.write_text(
        json.dumps(
            {
                "format": 1,
                "recipient": recipient,
                "fingerprint": evidence.fingerprint(recipient),
                "tool": "pyrage",
                "version": "1.4.0",
            }
        )
    )
    output = tmp_path / "outputs"
    for key, value in {
        "EVIDENCE_ID": "first-arm",
        "EVIDENCE_SOURCE": source,
        "EVIDENCE_PUBLIC": public,
        "EVIDENCE_EXPECTED": expected,
        "RUNNER_TEMP": tmp_path,
        "GITHUB_OUTPUT": output,
        "GITHUB_REPOSITORY": "yizhiteamo/Clytie",
        "GITHUB_RUN_ID": run["run_id"],
        "GITHUB_RUN_ATTEMPT": run["attempt"],
        "GITHUB_JOB": run["job_id"],
    }.items():
        monkeypatch.setenv(key, str(value))
    original_scan = evidence._scanner

    def only_public(private=False):
        assert not private
        return original_scan()

    monkeypatch.setattr(evidence, "_scanner", only_public)
    monkeypatch.setattr(
        transport, "_github", lambda endpoint: {"full_name": "yizhiteamo/Clytie", "private": True}
    )
    assert transport.main(["ci-seal"]) == 0
    paths = [Path(line.split("=", 1)[1]) for line in output.read_text().splitlines()]
    assert [p.name for p in paths] == ["measurements.age", "receipt.json"]
    assert all(p.is_file() for p in paths)
    assert run["candidate"] not in paths[1].read_text()
    # CI attests the job key, OS, run and attempt. Numeric job API metadata is
    # additionally checked and retained during the authorized local download.
    monkeypatch.setenv("GITHUB_JOB", "different_job")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "mismatch-outputs"))
    assert transport.main(["ci-seal"]) == 1
    assert not (tmp_path / "mismatch-outputs").exists()


@pytest.fixture
def sealed(tmp_path):
    source = tmp_path / "raw"
    run = fixture_files(source)
    ident = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "sealed", str(ident.to_public()), run)
    return tmp_path / "sealed"


def test_upload_gate_rechecks_private_exact_cipher_and_receipt(sealed):
    calls = []

    def repo():
        calls.append("live_check")
        return {"full_name": "yizhiteamo/Clytie", "private": True}

    result = transport.upload_gate(sealed, repository=repo)
    assert calls == ["live_check"]
    assert [Path(p).name for p in result] == ["measurements.age", "receipt.json"]


@pytest.mark.parametrize(
    "repo",
    [
        {"full_name": "yizhiteamo/Clytie", "private": False},
        {"full_name": "yizhiteamo/Clytie", "private": "true"},
        {"full_name": "unexpected/repo", "private": True},
        {},
    ],
)
def test_public_unknown_or_other_repository_stops_upload(sealed, repo):
    with pytest.raises(evidence.EvidenceError):
        transport.upload_gate(sealed, repository=lambda: repo)


def test_changed_ciphertext_and_unexpected_plaintext_stop_upload(sealed):
    def repo():
        return {"full_name": "yizhiteamo/Clytie", "private": True}

    (sealed / "plain.json").write_text("{}")
    with pytest.raises(evidence.EvidenceError):
        transport.upload_gate(sealed, repository=repo)
    (sealed / "plain.json").unlink()
    with (sealed / "measurements.age").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(evidence.EvidenceError):
        transport.upload_gate(sealed, repository=repo)


def test_key_creation_refuses_ci_repo_and_overwrite(tmp_path, monkeypatch):
    private_file = tmp_path / "keys/identity.txt"
    private_file.parent.mkdir()
    # No actual persistent evidence key generated by this test.
    monkeypatch.setattr(transport, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(transport, "_check_key_directory", lambda path: None)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(evidence.EvidenceError):
        transport.generate_key(private_file)
    assert not private_file.exists()
    monkeypatch.delenv("GITHUB_ACTIONS")
    monkeypatch.delenv("CI", raising=False)
    public = transport.generate_key(private_file)
    assert set(public) == {"format", "recipient", "fingerprint", "tool", "version"}
    assert private_file.read_text().strip() not in json.dumps(public)
    with pytest.raises(FileExistsError):
        transport.generate_key(private_file)
    repo_key = tmp_path / "repo/key.txt"
    repo_key.parent.mkdir()
    with pytest.raises(evidence.EvidenceError):
        transport.generate_key(repo_key)


def test_retention_is_observed_not_assumed():
    meta = {
        "id": 9,
        "expired": False,
        "created_at": "2026-11-12T00:00:00Z",
        "expires_at": "2026-11-26T00:00:00Z",
        "workflow_run": {"id": 7},
    }
    observed = transport.retention(meta, run_id=7)
    assert observed["accepted_days"] == 14
    assert observed["requested_days"] == 14
    assert (
        transport.retention({**meta, "expires_at": "2026-11-19T00:00:00Z"}, run_id=7)[
            "accepted_days"
        ]
        == 7
    )
    with pytest.raises(evidence.EvidenceError):
        transport.retention(meta, run_id=8)
    with pytest.raises(evidence.EvidenceError):
        transport.retention({**meta, "expires_at": None}, run_id=7)


def test_download_keeps_actual_retention_and_cipher_hashes(sealed, tmp_path, monkeypatch):
    def github(endpoint):
        if endpoint == "repos/yizhiteamo/Clytie":
            return {"full_name": "yizhiteamo/Clytie", "private": True}
        return {
            "id": 9,
            "name": "a43-encrypted-fixture",
            "expired": False,
            "created_at": "2026-11-12T00:00:00Z",
            "expires_at": "2026-11-26T00:00:00Z",
            "workflow_run": {"id": 7},
        }

    def download(endpoint, target):
        with zipfile.ZipFile(target, "w") as archive:
            for name in ("measurements.age", "receipt.json"):
                archive.write(sealed / name, name)

    monkeypatch.setattr(transport, "_github", github)
    monkeypatch.setattr(transport, "_download_archive", download)
    monkeypatch.setattr(transport, "_local_only", lambda: None)
    result = transport.download(9, 7, "a43-encrypted-fixture", tmp_path / "download")
    assert result["accepted_days"] == 14 and not result["local_archive_verified"]
    assert result["state"] == "encrypted_pending_verification"
    assert (tmp_path / "download/sealed/measurements.age").read_bytes() == (
        sealed / "measurements.age"
    ).read_bytes()
    assert (tmp_path / "download/download.json").exists()


def test_download_rejects_unsafe_outer_archive_without_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(
        transport, "repository", lambda: {"full_name": "yizhiteamo/Clytie", "private": True}
    )
    monkeypatch.setattr(
        transport,
        "_github",
        lambda endpoint: {
            "id": 9,
            "name": "a43-encrypted-fixture",
            "expired": False,
            "created_at": "2026-11-12T00:00:00Z",
            "expires_at": "2026-11-26T00:00:00Z",
            "workflow_run": {"id": 7},
        },
    )
    monkeypatch.setattr(transport, "_local_only", lambda: None)

    def download(endpoint, target):
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("../escape", "not evidence")

    monkeypatch.setattr(transport, "_download_archive", download)
    with pytest.raises(evidence.EvidenceError):
        transport.download(9, 7, "a43-encrypted-fixture", tmp_path / "download")
    assert not (tmp_path / "download/sealed").exists()
    assert not (tmp_path / "escape").exists()


def test_action_uploads_only_successful_exact_ciphertext_paths():
    root = Path(__file__).resolve().parents[1]
    action = yaml.safe_load(
        (root / ".github/actions/encrypted-measurement-artifact/action.yml").read_text(
            encoding="utf-8"
        )
    )
    seal, upload = action["runs"]["steps"]
    assert seal["run"] == "uv run --no-sync python -m scripts.measurement_transport ci-seal"
    assert "success()" in upload["if"] and "seal.outcome == 'success'" in upload["if"]
    assert "always()" not in str(action)
    assert upload["with"]["path"].splitlines() == [
        "${{ steps.seal.outputs.ciphertext }}",
        "${{ steps.seal.outputs.receipt }}",
    ]
    assert upload["with"]["retention-days"] == 14
    assert upload["with"]["if-no-files-found"] == "error"
    assert "identity" not in seal["env"] and "CLYTIE_HOME" not in seal["env"]


def test_ci_entry_emits_no_paths_when_encryption_fails(tmp_path, monkeypatch):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_REPOSITORY", "yizhiteamo/Clytie")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("EVIDENCE_ID", "first-arm")
    for name in ("SOURCE", "EXPECTED", "PUBLIC"):
        monkeypatch.setenv(f"EVIDENCE_{name}", str(tmp_path / "missing"))
    assert transport.main(["ci-seal"]) == 1
    assert not output.exists()


def test_seal_never_loads_local_identity_or_private_scanner(sealed, monkeypatch):
    # Existing ciphertext was produced without any private callback; confirm CI
    # upload eligibility inspects only ciphertext and public metadata.
    def prohibited(*args, **kwargs):
        raise AssertionError("private access in CI")

    monkeypatch.setattr(transport, "load_identity", prohibited)
    monkeypatch.setattr(evidence, "private_scan", prohibited)
    assert (
        len(
            transport.upload_gate(
                sealed, repository=lambda: {"full_name": "yizhiteamo/Clytie", "private": True}
            )
        )
        == 2
    )
