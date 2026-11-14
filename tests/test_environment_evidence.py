import copy

import pytest
from scripts import environment_evidence as env
from scripts import measurement_evidence as evidence


def data():
    runtime = dict(
        environment=dict(
            cpu_count=2, os="Windows", release_sha256="a" * 64, machine_sha256="b" * 64
        ),
        python="3.10.11",
        sqlite="3.40.1",
    )
    binding = dict(
        product_commit="c" * 40,
        tool_commit="d" * 40,
        platform="Windows",
        run_id=123,
        job_id="environment_windows",
        attempt=1,
    )
    return runtime, binding


def test_roundtrip_exact_container_and_pending(tmp_path):
    runtime, binding = data()
    ident = evidence.native_identity().generate()
    receipt = env.seal(runtime, binding, tmp_path / "sealed", str(ident.to_public()))
    assert set(p.name for p in (tmp_path / "sealed").iterdir()) == {
        "environment.age",
        "environment-receipt.json",
    }
    assert receipt["state"] == "encrypted_pending_verification"
    result = env.verify(
        tmp_path / "sealed", tmp_path / "verified", ident, binding, private_scan=lambda _: "pass"
    )
    assert result["accepted"] and result["runtime"] == runtime
    assert result["measurement_acceptance"] == "not_applicable"


@pytest.mark.parametrize("fault", ["field", "version", "platform", "attempt", "job"])
def test_schema_or_binding_rejected(tmp_path, fault):
    runtime, binding = data()
    if fault == "field":
        runtime["environment"]["hostname"] = "fictional"
    elif fault == "version":
        runtime["python"] = "fictional command"
    elif fault == "platform":
        binding["platform"] = "Linux"
    elif fault == "attempt":
        binding["attempt"] = 2
    else:
        binding["job_id"] = "other"
    with pytest.raises(ValueError):
        env.seal(
            runtime,
            binding,
            tmp_path / "sealed",
            str(evidence.native_identity().generate().to_public()),
        )
    assert not (tmp_path / "sealed").exists()


@pytest.mark.parametrize("fault", ["key", "tamper", "truncated", "identity", "private", "extra"])
def test_verify_never_accepts_bad_evidence(tmp_path, fault):
    runtime, binding = data()
    ident = evidence.native_identity().generate()
    env.seal(runtime, binding, tmp_path / "sealed", str(ident.to_public()))
    cipher = tmp_path / "sealed/environment.age"
    expected = copy.deepcopy(binding)
    if fault == "key":
        ident = evidence.native_identity().generate()
    elif fault == "tamper":
        raw = bytearray(cipher.read_bytes())
        raw[-1] ^= 1
        cipher.write_bytes(raw)
    elif fault == "truncated":
        cipher.write_bytes(cipher.read_bytes()[:40])
    elif fault == "identity":
        expected["run_id"] += 1
    elif fault == "extra":
        (tmp_path / "sealed/plain.json").write_text("{}")
    result = env.verify(
        tmp_path / "sealed",
        tmp_path / "verified",
        ident,
        expected,
        private_scan=lambda _: "fail" if fault == "private" else "pass",
    )
    assert result["accepted"] is False


def test_no_upload_or_collection_cli():
    assert not hasattr(env, "upload")
    assert not hasattr(env, "collect")


def test_target_never_overwritten(tmp_path):
    runtime, binding = data()
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("preserved")
    with pytest.raises(FileExistsError):
        env.seal(runtime, binding, target, str(evidence.native_identity().generate().to_public()))
    assert marker.read_text() == "preserved"
    assert list(target.iterdir()) == [marker]


def test_environment_sources_are_only_approved_fields(monkeypatch):
    from scripts import measurement_launch as launch

    monkeypatch.setattr(launch.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(launch.platform, "system", lambda: "Linux")
    monkeypatch.setattr(launch.platform, "release", lambda: "fictional-release")
    monkeypatch.setattr(launch.platform, "machine", lambda: "fictional-architecture")
    monkeypatch.setattr(launch.platform, "node", lambda: pytest.fail("hostname forbidden"))
    assert launch.environment() == dict(
        cpu_count=2,
        os="Linux",
        release_sha256=env.prep.digest(b"fictional-release"),
        machine_sha256=env.prep.digest(b"fictional-architecture"),
    )


def test_decrypted_path_traversal_rejected(tmp_path):
    import io
    import zipfile

    import pyrage

    _, binding = data()
    ident = evidence.native_identity().generate()
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr("../runtime.json", "{}")
        archive.writestr("archive-manifest.json", "{}")
    cipher = pyrage.encrypt(plain.getvalue(), [ident.to_public()])
    source = tmp_path / "sealed"
    source.mkdir()
    (source / env.CIPHER).write_bytes(cipher)
    receipt = dict(
        format=env.FORMAT,
        purpose=env.PURPOSE,
        state="encrypted_pending_verification",
        recipient_sha256=evidence.fingerprint(str(ident.to_public())),
        ciphertext_sha256=env.prep.digest(cipher),
        ciphertext_bytes=len(cipher),
        file=env.CIPHER,
    )
    (source / env.RECEIPT).write_bytes(env.prep.canonical(receipt))
    result = env.verify(
        source, tmp_path / "verified", ident, binding, private_scan=lambda _: "pass"
    )
    assert not result["accepted"]
    assert not (tmp_path / "verified").exists()
    assert not (tmp_path / "runtime.json").exists()


def test_capture_missing_admission_before_probe(monkeypatch):
    from scripts import environment_capture as capture

    monkeypatch.setattr(capture.os, "environ", {})
    monkeypatch.setattr(capture.launch, "environment", lambda: pytest.fail("unapproved probe"))
    with pytest.raises(ValueError, match="identity_invalid"):
        capture.main(["--product", capture.PRODUCT, "--tool", "b" * 40])


def test_claim_is_exclusive_and_retained(tmp_path):
    from scripts import environment_capture as capture

    _, expected = data()
    capture.claim(tmp_path, expected)
    original = (tmp_path / "a43-environment-claims/environment_windows.json").read_bytes()
    with pytest.raises(FileExistsError):
        capture.claim(tmp_path, expected)
    assert (tmp_path / "a43-environment-claims/environment_windows.json").read_bytes() == original


@pytest.mark.parametrize(
    "damage", [None, "attempt", "job", "head", "dirty", "event", "second", "ref", "product"]
)
def test_capture_actual_ci_binding(damage):
    from scripts import environment_capture as capture

    values = dict(
        GITHUB_ACTIONS="true",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REPOSITORY="yizhiteamo/Clytie",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_RUN_ID="123",
        GITHUB_RUN_NUMBER="1",
        GITHUB_REF="refs/heads/main",
        GITHUB_WORKFLOW_REF="yizhiteamo/Clytie/.github/workflows/environment.yml@refs/heads/main",
        GITHUB_JOB="environment_windows",
    )
    if damage == "attempt":
        values["GITHUB_RUN_ATTEMPT"] = "2"
    if damage == "job":
        values["GITHUB_JOB"] = "other"
    if damage == "event":
        values["GITHUB_EVENT_NAME"] = "push"
    if damage == "second":
        values["GITHUB_RUN_NUMBER"] = "2"
    if damage == "ref":
        values["GITHUB_REF"] = "refs/heads/m4-3"

    def git(*args):
        if args[0] == "rev-parse":
            return "c" * 40 if damage == "head" else "b" * 40
        if args[0] == "diff":
            return "dirty" if damage == "dirty" else ""
        if args[0] == "ls-files":
            return ""
        return "commit"

    if damage:
        with pytest.raises(ValueError):
            capture.binding(
                values,
                "a" * 40 if damage == "product" else capture.PRODUCT,
                "b" * 40,
                git=git,
                system="Windows",
            )
    else:
        assert (
            capture.binding(values, capture.PRODUCT, "b" * 40, git=git, system="Windows")["job_id"]
            == "environment_windows"
        )
