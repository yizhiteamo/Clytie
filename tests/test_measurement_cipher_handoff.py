"""Fictional encrypted files and GitHub responses; never contacts GitHub."""

import json

import pytest
from tests.measurement_tools import load

m = load("measurement_cipher_handoff")


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "id",
        "name",
        "run",
        "attempt",
        "head",
        "repository",
        "expired",
        "digest",
        "binding",
        "bytes",
        "unknown",
    ],
)
def test_exact_upload_confirmation_or_no_success_receipt(tmp_path, monkeypatch, damage):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    for name, data in (("measurements.age", b"fictional ciphertext"), ("receipt.json", b"{}")):
        (sealed / name).write_bytes(data)
    monkeypatch.setattr(
        m.transport,
        "validate_sealed",
        lambda path: [str(path / name) for name in ("measurements.age", "receipt.json")],
    )
    digest = "b" * 64
    env = dict(GITHUB_RUN_ID="123")
    meta = dict(
        id=456,
        name="fictional-encrypted-slot",
        digest="sha256:" + digest,
        expired=False,
        workflow_run=dict(id=123),
        created_at="2026-01-01T00:00:00Z",
        expires_at="2026-01-15T00:00:00Z",
    )
    run = dict(
        id=123,
        run_attempt=1,
        head_sha="a" * 40,
        path=".github/workflows/measurement.yml",
        repository=dict(full_name=m.transport.REPOSITORY),
    )
    if damage in {"id", "name"}:
        meta[damage] = 999 if damage == "id" else "wrong"
    if damage == "run":
        meta["workflow_run"]["id"] = 999
    if damage == "attempt":
        run["run_attempt"] = 2
    if damage == "head":
        run["head_sha"] = "b" * 40
    if damage == "repository":
        run["repository"]["full_name"] = "fictional/other"
    if damage == "expired":
        meta["expired"] = True
    if damage == "digest":
        meta["digest"] = "sha256:" + "c" * 64

    def github(endpoint):
        if damage == "unknown":
            raise OSError("response unavailable")
        return meta if "artifacts" in endpoint else run

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "sealed-binding.json").write_text(json.dumps(m.sealed_binding(sealed)))
    if damage == "bytes":
        (sealed / "measurements.age").write_bytes(b"different")
    if damage == "binding":
        (trial / "sealed-binding.json").write_text("{}")
    kwargs = dict(
        name="fictional-encrypted-slot",
        workflow_path=run["path"],
        request_commit="a" * 40,
        artifact_id=456,
        artifact_digest=digest,
        github=github,
    )
    if damage:
        with pytest.raises((ValueError, OSError)):
            m.confirm(trial, sealed, env, **kwargs)
        assert not (trial / "upload-confirmed.json").exists()
    else:
        m.confirm(trial, sealed, env, **kwargs)
        result = json.loads((trial / "upload-confirmed.json").read_bytes())
        assert result["artifact_id"] == 456 and result["attempt"] == 1
        assert result["ciphertext_sha256"] == m.evidence._hash(sealed / "measurements.age")
        with pytest.raises(FileExistsError):
            m.confirm(trial, sealed, env, **kwargs)
