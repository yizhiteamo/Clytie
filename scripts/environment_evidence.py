"""Runtime-only evidence container. No collection, network, upload or measurement entry."""

import io
import os
import stat
import zipfile
from importlib.metadata import version

import pyrage

from . import measurement_evidence as evidence
from . import measurement_preparation_evidence as prep

FORMAT = "environment_archive_v1"
PURPOSE = "environment_only_not_measurement"
CIPHER = "environment.age"
RECEIPT = "environment-receipt.json"
LIMIT = 64 * 1024


def validate(runtime, binding):
    evidence._fields(
        binding, ("product_commit", "tool_commit", "platform", "run_id", "job_id", "attempt")
    )
    for field in ("product_commit", "tool_commit"):
        evidence._pattern(binding[field], r"[a-f0-9]{40}")
    evidence._enum(binding["platform"], {"Windows", "Linux"})
    evidence._number(binding["run_id"], integer=True, positive=True)
    if (
        type(binding["attempt"]) is not int
        or binding["attempt"] != 1
        or binding["job_id"] != "environment_" + binding["platform"].lower()
    ):
        raise evidence.EvidenceError("environment_binding_invalid")
    evidence._fields(runtime, ("environment", "python", "sqlite"))
    for field in ("python", "sqlite"):
        evidence._pattern(runtime[field], r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
    value = runtime["environment"]
    evidence._fields(value, ("cpu_count", "os", "release_sha256", "machine_sha256"))
    evidence._number(value["cpu_count"], integer=True, positive=True)
    evidence._enum(value["os"], {binding["platform"]})
    for field in ("release_sha256", "machine_sha256"):
        evidence._pattern(value[field], r"[a-f0-9]{64}")
    scanner = evidence.public_scanner()
    return prep._scan(prep.canonical(runtime), runtime, "runtime.json", scanner) + prep._scan(
        prep.canonical(binding), binding, "binding", scanner
    )


def seal(runtime, binding, target, recipient):
    exceptions = validate(runtime, binding)
    if version("pyrage") != "1.4.0":
        raise evidence.EvidenceError("tool_version_mismatch")
    raw = prep.canonical(runtime)
    manifest = dict(
        format=FORMAT,
        purpose=PURPOSE,
        binding=binding,
        recipient_sha256=evidence.fingerprint(recipient),
        runtime_sha256=prep.digest(raw),
        generic_exceptions=exceptions,
        private_scan="not_run_in_ci",
    )
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("runtime.json", raw)
        archive.writestr("archive-manifest.json", prep.canonical(manifest))
    if plain.tell() > LIMIT:
        raise evidence.EvidenceError("environment_size_limit")
    plain.seek(0)
    target = prep._new(target)
    partial = target / (CIPHER + ".partial")
    with partial.open("xb") as output:
        pyrage.encrypt_io(plain, output, [evidence.recipient_type().from_str(recipient)])
        output.flush()
        os.fsync(output.fileno())
    ciphertext = prep._read_file(partial, LIMIT + 4096)
    if not ciphertext:
        raise evidence.EvidenceError("empty_ciphertext")
    partial.rename(target / CIPHER)
    receipt = dict(
        format=FORMAT,
        purpose=PURPOSE,
        state="encrypted_pending_verification",
        recipient_sha256=manifest["recipient_sha256"],
        ciphertext_sha256=prep.digest(ciphertext),
        ciphertext_bytes=len(ciphertext),
        file=CIPHER,
    )
    prep._save_bytes(target / RECEIPT, prep.canonical(receipt))
    return receipt


class Bounded(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > LIMIT:
            raise evidence.EvidenceError("environment_size_limit")
        return super().write(data)


def verify(source, target, ident, expected, *, private_scan):
    result = dict(
        accepted=False, state="verification_failed", measurement_acceptance="not_applicable"
    )
    try:
        source = prep._safe(source)
        if {p.name for p in source.iterdir()} != {CIPHER, RECEIPT}:
            raise evidence.EvidenceError("unexpected_environment_files")
        cipher = prep._read_file(source / CIPHER, LIMIT + 4096)
        receipt = evidence._json(prep._read_file(source / RECEIPT, LIMIT))
        fingerprint = evidence.fingerprint(str(ident.to_public()))
        if receipt != dict(
            format=FORMAT,
            purpose=PURPOSE,
            state="encrypted_pending_verification",
            recipient_sha256=fingerprint,
            ciphertext_sha256=prep.digest(cipher),
            ciphertext_bytes=len(cipher),
            file=CIPHER,
        ):
            raise evidence.EvidenceError("environment_receipt_mismatch")
        plain = Bounded()
        pyrage.decrypt_io(io.BytesIO(cipher), plain, [ident])
        plain.seek(0)
        with zipfile.ZipFile(plain) as archive:
            entries = archive.infolist()
            if (
                archive.comment
                or len(entries) != 2
                or {e.filename for e in entries} != {"runtime.json", "archive-manifest.json"}
            ):
                raise evidence.EvidenceError("environment_archive_invalid")
            for entry in entries:
                if (
                    entry.orig_filename != entry.filename
                    or entry.extra
                    or entry.comment
                    or entry.is_dir()
                    or entry.compress_type != zipfile.ZIP_STORED
                    or entry.flag_bits & 1
                    or entry.file_size > LIMIT
                    or entry.compress_size != entry.file_size
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                ):
                    raise evidence.EvidenceError("environment_archive_entry")
            raw = archive.read("runtime.json")
            manifest_raw = archive.read("archive-manifest.json")
        runtime = evidence._json(raw)
        manifest = evidence._json(manifest_raw)
        exceptions = validate(runtime, expected)
        if manifest != dict(
            format=FORMAT,
            purpose=PURPOSE,
            binding=expected,
            recipient_sha256=fingerprint,
            runtime_sha256=prep.digest(raw),
            generic_exceptions=exceptions,
            private_scan="not_run_in_ci",
        ):
            raise evidence.EvidenceError("environment_manifest_mismatch")
        target = prep._new(target)
        paths = []
        for name, data in (("runtime.json", raw), ("archive-manifest.json", manifest_raw)):
            path = target / name
            prep._save_bytes(path, data)
            paths.append(path)
        scan = private_scan(paths)
        result.update(
            state="verified_private_pass_generic_exception"
            if scan == "pass"
            else "private_scan_not_pass",
            accepted=scan == "pass",
            runtime=runtime,
            private_scan=scan,
            runtime_sha256=prep.digest(raw),
        )
    except Exception:
        pass
    return result
