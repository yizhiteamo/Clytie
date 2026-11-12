"""Explicit local/CI evidence commands. Never runs measurements or uploads plaintext."""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import platform
import stat
import subprocess
import zipfile
from importlib.metadata import version
from pathlib import Path

import pyrage

try:
    from . import measurement_evidence as evidence
except ImportError:
    import measurement_evidence as evidence

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "yizhiteamo/Clytie"


def _local_only():
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
        raise evidence.EvidenceError("local_only_operation")


def _check_key_directory(path):
    path = evidence._safe_path(path)
    if os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise evidence.EvidenceError("key_directory_not_private")
        return
    # Constant script, path through a per-process variable; no interpolated shell command.
    script = """
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:CLYTIE_EVIDENCE_KEY_DIRECTORY
$entries = @($acl.Access | ForEach-Object {
    @{ sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;
       kind=$_.AccessControlType.ToString(); rights=[int]$_.FileSystemRights }
})
@{ current=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
   entries=$entries } | ConvertTo-Json -Compress -Depth 4
"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
        ],
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}
            },
            "CLYTIE_EVIDENCE_KEY_DIRECTORY": str(path),
        },
        capture_output=True,
        check=False,
    )
    try:
        if result.returncode:
            raise ValueError()
        acl = json.loads(result.stdout)
        allowed = {acl["current"], "S-1-5-18", "S-1-5-32-544"}
        if not acl["entries"] or any(
            row["kind"] == "Allow" and row["rights"] and row["sid"] not in allowed
            for row in acl["entries"]
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise evidence.EvidenceError("key_directory_not_private") from None


def _identity_path(path):
    _local_only()
    path = Path(path).absolute()
    if path.is_relative_to(ROOT.resolve()):
        raise evidence.EvidenceError("identity_inside_repository")
    _check_key_directory(path.parent)
    return path


def generate_key(path):
    path = _identity_path(path)
    identity = evidence.native_identity().generate()
    # Existing key is never rotated or overwritten; private value is never returned.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as output:
        output.write(str(identity) + "\n")
        output.flush()
        os.fsync(output.fileno())
    public = str(identity.to_public())
    return {
        "format": 1,
        "recipient": public,
        "fingerprint": evidence.fingerprint(public),
        "tool": "pyrage",
        "version": version("pyrage"),
    }


def load_identity(path):
    path = evidence._safe_path(_identity_path(path))
    _check_key_directory(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
        raise evidence.EvidenceError("invalid_identity_file")
    if os.name != "nt" and info.st_mode & 0o077:
        raise evidence.EvidenceError("identity_not_private")
    return evidence.native_identity().from_str(path.read_text(encoding="ascii").strip())


def _github(endpoint):
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, check=False)
    if result.returncode:
        raise evidence.EvidenceError("github_metadata_unavailable")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise evidence.EvidenceError("github_metadata_unavailable") from None


def repository():
    value = _github(f"repos/{REPOSITORY}")
    # Do not persist the rest of the API response or client environment.
    return {"full_name": value.get("full_name"), "private": value.get("private")}


def upload_gate(directory, *, repository=repository):
    value = repository()
    if value.get("full_name") != REPOSITORY or value.get("private") is not True:
        raise evidence.EvidenceError("repository_not_confirmed_private")
    return validate_sealed(directory)


def validate_sealed(directory):
    directory = evidence._safe_path(directory)
    if {item.name for item in directory.iterdir()} != {"measurements.age", "receipt.json"}:
        raise evidence.EvidenceError("unexpected_upload_entry")
    paths = [evidence._safe_path(directory / name) for name in ("measurements.age", "receipt.json")]
    if any(not stat.S_ISREG(path.stat().st_mode) for path in paths):
        raise evidence.EvidenceError("invalid_upload_entry")
    receipt = evidence._json(paths[1].read_bytes())
    evidence._fields(
        receipt,
        ("state", "format", "recipient_sha256", "ciphertext_sha256", "ciphertext_bytes", "file"),
    )
    for field in ("recipient_sha256", "ciphertext_sha256"):
        evidence._pattern(receipt[field], r"[a-f0-9]{64}")
    if (
        receipt["state"] != "encrypted_pending_verification"
        or receipt["format"] != 1
        or receipt["file"] != "measurements.age"
        or receipt["ciphertext_bytes"] != paths[0].stat().st_size
        or receipt["ciphertext_sha256"] != evidence._hash(paths[0])
    ):
        raise evidence.EvidenceError("upload_receipt_mismatch")
    return [str(path) for path in paths]


def retention(meta, *, run_id):
    try:
        if meta["expired"] is not False or meta["workflow_run"]["id"] != run_id:
            raise ValueError()
        created = datetime.datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
        expires = datetime.datetime.fromisoformat(meta["expires_at"].replace("Z", "+00:00"))
        duration = (expires - created).total_seconds() / (24 * 60 * 60)
        if duration <= 0:
            raise ValueError()
        return {
            "artifact_id": meta["id"],
            "run_id": run_id,
            "requested_days": 14,
            "accepted_days": duration,
            "created_at": meta["created_at"],
            "expires_at": meta["expires_at"],
            "local_archive_verified": False,
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        raise evidence.EvidenceError("artifact_retention_unverified") from None


def _download_archive(endpoint, target):
    with Path(target).open("xb") as output:
        process = subprocess.Popen(
            ["gh", "api", endpoint], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            total = 0
            while block := process.stdout.read(256 * 1024):
                total += len(block)
                if total > evidence.MAX_TOTAL + 2 * 1024 * 1024:
                    raise evidence.EvidenceError("download_size_limit")
                output.write(block)
            if process.wait() != 0:
                raise evidence.EvidenceError("artifact_download_failed")
            output.flush()
            os.fsync(output.fileno())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
            process.stdout.close()


def download(artifact_id, run_id, name, target):
    _local_only()
    evidence._number(artifact_id, integer=True, positive=True)
    evidence._number(run_id, integer=True, positive=True)
    evidence._pattern(name, r"a43-encrypted-[a-zA-Z0-9_-]{1,120}")
    repo = repository()
    if repo.get("private") is not True or repo.get("full_name") != REPOSITORY:
        raise evidence.EvidenceError("repository_not_confirmed_private")
    endpoint = f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}"
    meta = _github(endpoint)
    if meta.get("id") != artifact_id or meta.get("name") != name:
        raise evidence.EvidenceError("artifact_identity_mismatch")
    observed = retention(meta, run_id=run_id)
    target = evidence._new_directory(target)
    downloaded = target / "artifact.zip"
    _download_archive(endpoint + "/zip", downloaded)
    with zipfile.ZipFile(downloaded) as archive:
        entries = archive.infolist()
        if len(entries) != 2 or {entry.filename for entry in entries} != {
            "measurements.age",
            "receipt.json",
        }:
            raise evidence.EvidenceError("unexpected_download_entries")
        for entry in entries:
            limit = (
                16 * 1024 if entry.filename == "receipt.json" else evidence.MAX_TOTAL + 1024 * 1024
            )
            if (
                entry.is_dir()
                or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                or entry.file_size > limit
                or entry.flag_bits & 1
            ):
                raise evidence.EvidenceError("invalid_download_entry")
        sealed = evidence._new_directory(target / "sealed")
        for entry in entries:
            with archive.open(entry) as source, (sealed / entry.filename).open("xb") as output:
                while block := source.read(256 * 1024):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
    validate_sealed(sealed)
    observed.update(
        state="encrypted_pending_verification",
        artifact_name=name,
        downloaded_sha256=evidence._hash(downloaded),
        ciphertext_sha256=evidence._hash(sealed / "measurements.age"),
    )
    evidence._save(target / "download.json", observed)
    return observed


def _seal_inputs(source, target, public_path, expected_path):
    public = evidence._json(Path(public_path).read_bytes())
    evidence._fields(public, ("format", "recipient", "fingerprint", "tool", "version"))
    if (
        public["format"] != 1
        or public["tool"] != "pyrage"
        or public["version"] != "1.4.0"
        or public["fingerprint"] != evidence.fingerprint(public["recipient"])
    ):
        raise evidence.EvidenceError("public_identity_mismatch")
    expected = evidence._json(Path(expected_path).read_bytes())
    evidence.seal(source, target, public["recipient"], expected)


def _ci_seal():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise evidence.EvidenceError("unexpected_repository")
    identifier = os.environ["EVIDENCE_ID"]
    evidence._pattern(identifier, r"[a-z][a-z0-9_-]{0,60}")
    expected = evidence._json(Path(os.environ["EVIDENCE_EXPECTED"]).read_bytes())
    evidence.validate_run(expected)
    head = (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True)
        .stdout.decode("ascii")
        .strip()
    )
    if (
        expected["candidate"] != head
        or expected["run_id"] != int(os.environ["GITHUB_RUN_ID"])
        or expected["attempt"] != int(os.environ["GITHUB_RUN_ATTEMPT"])
        or expected["job_id"] != os.environ["GITHUB_JOB"]
        or expected["platform"] != platform.system()
    ):
        raise evidence.EvidenceError("ci_identity_mismatch")
    target = Path(os.environ["RUNNER_TEMP"]) / ("a43-sealed-" + identifier)
    _seal_inputs(
        os.environ["EVIDENCE_SOURCE"],
        target,
        os.environ["EVIDENCE_PUBLIC"],
        os.environ["EVIDENCE_EXPECTED"],
    )
    cipher, receipt = upload_gate(target)
    if any("\n" in value or "\r" in value for value in (cipher, receipt)):
        raise evidence.EvidenceError("invalid_output_path")
    # No output path is exposed before encryption AND live visibility checks succeed.
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"ciphertext={cipher}\nreceipt={receipt}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ci-seal")
    fetch = sub.add_parser("download")
    fetch.add_argument("--artifact-id", type=int, required=True)
    fetch.add_argument("--run-id", type=int, required=True)
    fetch.add_argument("--name", required=True)
    fetch.add_argument("--target", type=Path, required=True)
    keygen = sub.add_parser("keygen")
    keygen.add_argument("--identity", type=Path, required=True)
    keygen.add_argument("--public", type=Path, required=True)
    seal = sub.add_parser("seal")
    seal.add_argument("--source", type=Path, required=True)
    seal.add_argument("--target", type=Path, required=True)
    seal.add_argument("--public", type=Path, required=True)
    seal.add_argument("--expected", type=Path, required=True)
    gate = sub.add_parser("upload-gate")
    gate.add_argument("--sealed", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--sealed", type=Path, required=True)
    verify.add_argument("--target", type=Path, required=True)
    verify.add_argument("--identity", type=Path, required=True)
    verify.add_argument("--expected", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            download(args.artifact_id, args.run_id, args.name, args.target)
            print("encrypted_pending_verification")
        elif args.command == "ci-seal":
            _ci_seal()
            print("encrypted_pending_verification")
        elif args.command == "keygen":
            if args.public.exists():
                raise evidence.EvidenceError("public_file_already_exists")
            evidence._save(args.public, generate_key(args.identity))
            print("dedicated_key_created")
        elif args.command == "seal":
            _seal_inputs(args.source, args.target, args.public, args.expected)
            print("encrypted_pending_verification")
        elif args.command == "upload-gate":
            upload_gate(args.sealed)
            print("ciphertext_upload_eligible_private_repository")
        else:
            ident = load_identity(args.identity)
            # Local check uses downloaded receipt, does not query network or auto-upload.
            validate_sealed(args.sealed)
            expected = evidence._json(args.expected.read_bytes())
            result = evidence.open_bundle(
                args.sealed / "measurements.age", args.target, ident, expected
            )
            print(result["state"])
            return 0 if result["accepted"] else 1
        return 0
    except (
        evidence.EvidenceError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
        pyrage.IdentityError,
        pyrage.RecipientError,
        pyrage.EncryptError,
        pyrage.DecryptError,
    ):
        # Do not print paths, decrypted strings, key parser diagnostics or API responses.
        print("evidence_operation_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
