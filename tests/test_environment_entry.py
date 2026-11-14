"""Real entry/sealing path, fictional platform fields and in-memory test keys."""

import pytest
from tests.measurement_tools import load

capture = load("environment_capture")
archive = load("environment_evidence")


def setup_entry(tmp_path, monkeypatch):
    identity = archive.evidence.native_identity().generate()
    recipient = str(identity.to_public())
    fingerprint = archive.evidence.fingerprint(recipient)
    variables = dict(
        GITHUB_ACTIONS="true",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REPOSITORY="yizhiteamo/Clytie",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_RUN_NUMBER="2",
        GITHUB_REF="refs/heads/main",
        GITHUB_WORKFLOW_REF=capture.WORKFLOW,
        GITHUB_RUN_ID="123",
        GITHUB_JOB="environment_windows",
        RUNNER_TEMP=str(tmp_path),
    )
    monkeypatch.setattr(capture.os, "environ", variables)
    monkeypatch.setattr(capture.platform, "system", lambda: "Windows")
    monkeypatch.setattr(capture.platform, "python_version", lambda: "3.10.11")
    monkeypatch.setattr(capture.platform, "release", lambda: "fictional-release")
    monkeypatch.setattr(capture.platform, "machine", lambda: "fictional-architecture")
    monkeypatch.setattr(capture.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(capture, "PUBLIC_FINGERPRINT", fingerprint)
    monkeypatch.setattr(
        capture.workflow,
        "public_identity",
        lambda _: dict(recipient=recipient, fingerprint=fingerprint),
    )

    def git(command, **kwargs):
        if command[1] == "rev-parse":
            return "b" * 40
        if command[1] == "cat-file":
            return "commit"
        return ""

    monkeypatch.setattr(capture.subprocess, "check_output", git)
    expected = dict(
        product_commit=capture.PRODUCT,
        tool_commit="b" * 40,
        platform="Windows",
        run_id=123,
        job_id="environment_windows",
        attempt=1,
    )
    return identity, expected


@pytest.mark.parametrize("existing_parent", [False, True])
def test_entry_from_empty_root_real_encryption_and_exact_pair(
    tmp_path, monkeypatch, existing_parent
):
    identity, expected = setup_entry(tmp_path, monkeypatch)
    parent = tmp_path / "a43-environment"
    if existing_parent:
        parent.mkdir()
        (parent / "prior-evidence").write_text("preserve")
    capture.main(["--product", capture.PRODUCT, "--tool", "b" * 40])
    source = parent / "Windows"
    assert {p.name for p in source.iterdir()} == {archive.CIPHER, archive.RECEIPT}
    result = archive.verify(
        source, tmp_path / "verified", identity, expected, private_scan=lambda _: "pass"
    )
    assert result["accepted"]
    assert result["runtime"]["environment"]["cpu_count"] == 2
    if existing_parent:
        assert (parent / "prior-evidence").read_text() == "preserve"


@pytest.mark.parametrize(
    "fault", ["existing_target", "file_parent", "encrypt", "linked_parent", "traversal"]
)
def test_entry_refuses_unsafe_or_failed_output_without_plaintext(tmp_path, monkeypatch, fault):
    setup_entry(tmp_path, monkeypatch)
    parent = tmp_path / "a43-environment"
    if fault == "existing_target":
        (parent / "Windows").mkdir(parents=True)
        (parent / "Windows/retained").write_text("preserve")
    elif fault == "file_parent":
        parent.write_text("preserve")
    elif fault == "encrypt":

        def fail(*args, **kwargs):
            raise OSError("fictional encryption failure")

        monkeypatch.setattr(archive.pyrage, "encrypt_io", fail)
    elif fault == "linked_parent":
        parent.mkdir()
        real = archive.prep._safe

        def checked(path):
            if path == parent:
                raise ValueError("linked_path")
            return real(path)

        monkeypatch.setattr(archive.prep, "_safe", checked)
    else:
        capture.os.environ["RUNNER_TEMP"] = str(tmp_path / ".." / tmp_path.name)
    with pytest.raises((ValueError, OSError)):
        capture.main(["--product", capture.PRODUCT, "--tool", "b" * 40])
    assert not (parent / "Windows/environment-receipt.json").exists()
    assert not (parent / "Windows/runtime.json").exists()
    assert not (parent / "Windows/archive-manifest.json").exists()
    if fault == "existing_target":
        assert (parent / "Windows/retained").read_text() == "preserve"
