"""Confirm upload metadata and unchanged local pair, not remote byte authenticity.

CI does not download artifacts or gain Actions read permission. The existing local
download, authenticated decryption and scans remain necessary for acceptance.
"""

from pathlib import Path

if __package__:
    from . import measurement_evidence as evidence
    from . import measurement_launch as launch
    from . import measurement_preparation_evidence as prep
    from . import measurement_transport as transport
else:
    import measurement_evidence as evidence
    import measurement_launch as launch
    import measurement_preparation_evidence as prep
    import measurement_transport as transport


def require(condition, code):
    if not condition:
        raise evidence.EvidenceError(code)


def confirm(
    trial,
    sealed,
    env,
    *,
    name,
    workflow_path,
    request_commit,
    artifact_id,
    artifact_digest,
    github,
):
    transport.validate_sealed(sealed)
    evidence._number(artifact_id, integer=True, positive=True)
    evidence._pattern(artifact_digest, r"[a-f0-9]{64}")
    endpoint = f"repos/{transport.REPOSITORY}/actions/artifacts/{artifact_id}"
    meta = github(endpoint)
    require(
        meta.get("id") == artifact_id
        and meta.get("name") == name
        and meta.get("digest") == "sha256:" + artifact_digest,
        "cipher_upload_identity_mismatch",
    )
    retention = transport.retention(meta, run_id=int(env["GITHUB_RUN_ID"]))
    run = github(f"repos/{transport.REPOSITORY}/actions/runs/{env['GITHUB_RUN_ID']}")
    require(
        run.get("id") == int(env["GITHUB_RUN_ID"])
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1
        and run.get("head_sha") == request_commit
        and run.get("path") == workflow_path
        and run.get("repository", {}).get("full_name") == transport.REPOSITORY,
        "cipher_upload_run_mismatch",
    )
    binding = evidence._json(prep._read_file(trial / "sealed-binding.json", 4096))
    require(binding == sealed_binding(sealed), "cipher_upload_local_bytes_changed")
    prep._save_bytes(
        trial / "upload-confirmed.json",
        launch.canonical(
            dict(
                retention,
                artifact_name=name,
                archive_sha256=artifact_digest,
                ciphertext_sha256=evidence._hash(Path(sealed) / "measurements.age"),
                receipt_sha256=evidence._hash(Path(sealed) / "receipt.json"),
                attempt=1,
                local_archive_verified=False,
                state="upload_confirmed_not_authenticated",
            )
        ),
    )


def sealed_binding(sealed):
    transport.validate_sealed(sealed)
    return {
        name: evidence._hash(Path(sealed) / name) for name in ("measurements.age", "receipt.json")
    }
