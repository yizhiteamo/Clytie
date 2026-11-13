"""A4.3 preparation-only transport; never generates geometry or runs measurements.

The outer generator must attest material stability around generation. This
module validates current checkout/material at sealing time, not its history.
"""

import argparse
import io
import os
import platform
import stat
import tarfile
import zipfile
from pathlib import Path

try:
    from . import measurement_evidence as evidence
    from . import measurement_launch as launch
    from . import measurement_preparation_evidence as prep
    from . import measurement_transport as base
    from . import measurement_version as versions
except ImportError:
    import measurement_evidence as evidence
    import measurement_launch as launch
    import measurement_preparation_evidence as prep
    import measurement_transport as base
    import measurement_version as versions

Error = evidence.EvidenceError
MAX_CIPHER = prep.MAX_TOTAL + prep.MAX_FILE
MAX_RECEIPT = 4096
MAX_DOWNLOAD = MAX_CIPHER + MAX_RECEIPT + 4096
SEALED = (prep.CIPHER, "receipt.json")


def validate_sealed(directory):
    directory = prep._safe(directory)
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != set(SEALED):
        raise Error("unexpected_upload_entry")
    cipher = prep._read_file(directory / prep.CIPHER, MAX_CIPHER)
    receipt = prep._bounded_json(prep._read_file(directory / "receipt.json", MAX_RECEIPT))
    evidence._fields(
        receipt,
        (
            "state",
            "format",
            "purpose",
            "recipient_sha256",
            "ciphertext_sha256",
            "ciphertext_bytes",
            "file",
        ),
    )
    for field in ("recipient_sha256", "ciphertext_sha256"):
        evidence._pattern(receipt[field], r"[a-f0-9]{64}")
    evidence._number(receipt["ciphertext_bytes"], integer=True, positive=True)
    if (
        receipt["state"] != "encrypted_pending_verification"
        or receipt["format"] != prep.FORMAT
        or receipt["purpose"] != prep.PURPOSE
        or receipt["file"] != prep.CIPHER
        or receipt["ciphertext_bytes"] != len(cipher)
        or receipt["ciphertext_sha256"] != prep.digest(cipher)
    ):
        raise Error("upload_receipt_mismatch")
    return [str(directory / name) for name in SEALED]


def _private(repository):
    value = repository()
    if (
        type(value) is not dict
        or value.get("full_name") != base.REPOSITORY
        or value.get("private") is not True
    ):
        raise Error("repository_not_confirmed_private")


def upload_gate(directory, *, repository=base.repository):
    _private(repository)
    return validate_sealed(directory)


def _extract(downloaded, target):
    # Keep the received archive unchanged; never invoke extract/extractall.
    raw = prep._read_file(downloaded, MAX_DOWNLOAD)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if (
            archive.comment
            or len(entries) != 2
            or {entry.filename for entry in entries} != set(SEALED)
        ):
            raise Error("unexpected_download_entries")
        for entry in entries:
            limit = MAX_CIPHER if entry.filename == prep.CIPHER else MAX_RECEIPT
            if (
                entry.is_dir()
                or entry.orig_filename != entry.filename
                or entry.comment
                or entry.extra
                or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                or entry.external_attr & 0x410
                or entry.flag_bits & 1
                or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or not 0 < entry.file_size <= limit
                or entry.compress_size > MAX_DOWNLOAD
            ):
                raise Error("invalid_download_entry")
        sealed = prep._new(target)
        for entry in entries:
            limit = MAX_CIPHER if entry.filename == prep.CIPHER else MAX_RECEIPT
            with archive.open(entry) as source:
                contents = source.read(limit + 1)
                if len(contents) != entry.file_size or len(contents) > limit or source.read(1):
                    raise Error("download_size_limit")
            prep._save_bytes(sealed / entry.filename, contents)
    validate_sealed(sealed)
    return prep.digest(raw)


def download(
    artifact_id,
    run_id,
    name,
    target,
    *,
    repository=base.repository,
    github=base._github,
    downloader=base._download_archive,
):
    base._local_only()
    for value in (artifact_id, run_id):
        evidence._number(value, integer=True, positive=True)
    evidence._pattern(name, r"a43-preparation-encrypted-[a-zA-Z0-9_-]{1,100}")
    _private(repository)
    endpoint = f"repos/{base.REPOSITORY}/actions/artifacts/{artifact_id}"
    meta = github(endpoint)
    if (
        type(meta) is not dict
        or type(meta.get("id")) is not int
        or meta["id"] != artifact_id
        or meta.get("name") != name
    ):
        raise Error("artifact_identity_mismatch")
    evidence._number(meta.get("workflow_run", {}).get("id"), integer=True, positive=True)
    observed = base.retention(meta, run_id=run_id)
    if observed["accepted_days"] > 14:
        raise Error("artifact_retention_unverified")
    target = prep._new(target)
    downloaded = target / "artifact.zip"
    downloader(endpoint + "/zip", downloaded, max_bytes=MAX_DOWNLOAD)
    archive_hash = _extract(downloaded, target / "sealed")
    observed.update(
        state="encrypted_pending_verification",
        purpose=prep.PURPOSE,
        measurement_acceptance="not_applicable",
        artifact_name=name,
        downloaded_sha256=archive_hash,
        ciphertext_sha256=prep.digest(prep._read_file(target / "sealed" / prep.CIPHER, MAX_CIPHER)),
    )
    # This local handoff index is never part of the upload whitelist.
    prep._save_bytes(target / "download.json", prep.canonical(observed))
    return observed


def verify(sealed, target, identity, expected):
    # The expected binding is mandatory and supplied from an independent local
    # run record. Never infer it by decrypting the untrusted artifact itself.
    base._local_only()
    prep.validate_binding(expected)
    validate_sealed(sealed)
    ident = base.load_identity(identity)
    return prep.open_bundle(
        Path(sealed) / prep.CIPHER, target, ident, expected, private_scan=evidence.private_scan
    )


def material_identity(root, version_directory, candidate):
    """Recompute current source/tool bytes. No claim about generation history."""
    evidence._pattern(candidate, r"[a-f0-9]{40}")
    root = prep._safe(root)
    version_directory = prep._safe(version_directory)
    prep._safe(version_directory / "source")
    inventory_raw = prep._read_file(version_directory / "source-inventory.json", 4 * 1024 * 1024)
    inventory = evidence._json(inventory_raw)
    evidence._fields(
        inventory, ("format", "mode", "candidate", "source_commit", "archive_sha256", "files")
    )
    if (
        type(inventory["format"]) is not int
        or inventory["format"] != 1
        or inventory["mode"] != "candidate"
        or inventory["candidate"] != candidate
        or inventory["source_commit"] != candidate
    ):
        raise Error("source_identity_mismatch")
    # An internally consistent self-written inventory is not source provenance.
    # Compare the entire inventory against the actual fixed git object archive.
    archive = versions._git(root, "archive", "--format=tar", candidate)
    found = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for entry in bundle:
            if entry.isdir():
                continue
            if not entry.isfile() or entry.name in found:
                raise Error("source_archive_invalid")
            found[entry.name] = prep.digest(bundle.extractfile(entry).read())
            actual = prep._read_file(version_directory / "source" / entry.name, entry.size)
            if prep.digest(actual) != found[entry.name]:
                raise Error("source_identity_mismatch")
    if (
        inventory["archive_sha256"] != prep.digest(archive)
        or inventory["files"] != found
        or versions.verify(version_directory) != inventory
    ):
        raise Error("source_identity_mismatch")
    tools = launch.tool_inventory(root)
    if "scripts/measurement_preparation_transport.py" not in tools["files"]:
        raise Error("preparation_transport_not_inventoried")
    return dict(
        source_inventory_sha256=prep.plan.digest(inventory),
        tool_sha256=tools["sha256"],
        fixture_sha256=tools["files"]["scripts/measurement_fixture.py"],
        geometry_tool_sha256=tools["files"]["scripts/measurement_geometry.py"],
        preparation_tool_sha256=tools["files"]["scripts/measurement_preparation_evidence.py"],
    )


def ci_seal(
    source,
    target,
    public,
    expected,
    version_directory,
    *,
    root=base.ROOT,
    environ=None,
    system=None,
    repository=base.repository,
):
    """Seal already prepared raw bytes; caller owns before/after generation checks."""
    env = os.environ if environ is None else environ
    prep.validate_binding(expected)
    evidence._fields(public, ("format", "recipient", "fingerprint", "tool", "version"))
    if (
        type(public["format"]) is not int
        or public["format"] != 1
        or public["tool"] != "pyrage"
        or public["version"] != "1.4.0"
        or public["fingerprint"] != evidence.fingerprint(public["recipient"])
    ):
        raise Error("public_identity_mismatch")
    head = versions._git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if (
        env.get("GITHUB_ACTIONS") != "true"
        or env.get("GITHUB_REPOSITORY") != base.REPOSITORY
        or expected["candidate"] != head
        or expected["run_id"] != int(env["GITHUB_RUN_ID"])
        or expected["attempt"] != int(env["GITHUB_RUN_ATTEMPT"])
        or expected["job_id"] != env["GITHUB_JOB"]
        or expected["platform"] != (platform.system() if system is None else system)
    ):
        raise Error("ci_identity_mismatch")
    material = material_identity(root, version_directory, head)
    if any(expected[key] != value for key, value in material.items()):
        raise Error("ci_material_mismatch")
    prep.seal(source, target, public["recipient"], expected)
    # Detect modifications during sealing as well. This is not a replacement for
    # before/after checks around the upstream generator's actual run.
    if (
        versions._git(root, "rev-parse", "HEAD").decode("ascii").strip() != head
        or material_identity(root, version_directory, head) != material
    ):
        raise Error("ci_material_changed")
    paths = upload_gate(target, repository=repository)
    if any("\n" in value or "\r" in value for value in paths):
        raise Error("invalid_output_path")
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("upload-gate")
    gate.add_argument("--sealed", type=Path, required=True)
    fetch = sub.add_parser("download")
    fetch.add_argument("--artifact-id", type=int, required=True)
    fetch.add_argument("--run-id", type=int, required=True)
    fetch.add_argument("--name", required=True)
    fetch.add_argument("--target", type=Path, required=True)
    local = sub.add_parser("verify")
    local.add_argument("--sealed", type=Path, required=True)
    local.add_argument("--target", type=Path, required=True)
    local.add_argument("--identity", type=Path, required=True)
    local.add_argument("--expected", type=Path, required=True)
    seal = sub.add_parser("ci-seal")
    for name in ("source", "target", "public", "expected", "version-directory"):
        seal.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            download(args.artifact_id, args.run_id, args.name, args.target)
        elif args.command == "verify":
            expected = prep._bounded_json(prep._read_file(args.expected, prep.MAX_FILE))
            result = verify(args.sealed, args.target, args.identity, expected)
            print(result["state"])
            return 0 if result["accepted"] else 1
        elif args.command == "upload-gate":
            upload_gate(args.sealed)
        else:
            public = prep._bounded_json(prep._read_file(args.public, MAX_RECEIPT))
            expected = prep._bounded_json(prep._read_file(args.expected, prep.MAX_FILE))
            cipher, receipt = ci_seal(
                args.source, args.target, public, expected, args.version_directory
            )
            output_path = prep._safe(Path(os.environ["GITHUB_OUTPUT"]))
            info = output_path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Error("invalid_output_path")
            descriptor = os.open(
                output_path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as output:
                opened = os.fstat(output.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                ):
                    raise Error("output_file_changed")
                output.write(f"ciphertext={cipher}\nreceipt={receipt}\n")
        print("encrypted_pending_verification")
        return 0
    except Exception:
        # Never expose paths, GitHub payloads, key parsing errors or raw metadata.
        print("preparation_operation_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
