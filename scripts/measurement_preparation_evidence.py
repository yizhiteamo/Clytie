"""Bounded preparation metadata archive. No uploader, CLI, key lookup or probing.

Expected bindings are supplied by a trusted outer caller; JSON is not proof of
CI provenance. Accepted means locally authenticated/scanned metadata only.
"""

import hashlib
import io
import json
import os
import stat
import zipfile
from importlib.metadata import version
from pathlib import Path

import pyrage

try:
    from . import measurement_evidence as evidence
    from . import measurement_plan as plan
except ImportError:
    import measurement_evidence as evidence
    import measurement_plan as plan

FILES = ("geometry.json", "runtime.json")
MAX_FILE = 64 * 1024
MAX_LINE = 16 * 1024
MAX_TOTAL = 4 * MAX_FILE
CIPHER = "preparation.age"
PURPOSE = "preparation_only_not_measurement"
FORMAT = "preparation_archive_v1"
HASH_FIELDS = (
    "source_inventory_sha256",
    "tool_sha256",
    "geometry_tool_sha256",
    "preparation_tool_sha256",
    "fixture_sha256",
)
BINDING_FIELDS = (
    "candidate",
    *HASH_FIELDS,
    "fixture_version",
    "seed",
    "platform",
    "run_id",
    "job_id",
    "attempt",
)
Error = evidence.EvidenceError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_binding(binding):
    evidence._fields(binding, BINDING_FIELDS)
    evidence._pattern(binding["candidate"], r"[a-f0-9]{40}")
    for key in HASH_FIELDS:
        evidence._pattern(binding[key], r"[a-f0-9]{64}")
    evidence._enum(binding["fixture_version"], {"legal_memory_v2"})
    evidence._number(binding["seed"], integer=True)
    if binding["seed"] != 73:
        raise Error("preparation_seed_invalid")
    evidence._enum(binding["platform"], {"Windows", "Linux"})
    for key in ("run_id", "attempt"):
        evidence._number(binding[key], integer=True, positive=True)
    evidence._enum(binding["job_id"], {"prepare_" + binding["platform"].lower()})


def _bounded_json(raw):
    if type(raw) is not bytes or not raw or len(raw) > MAX_FILE:
        raise Error("preparation_size_invalid")
    # Decode explicitly: json.loads(bytes) would also accept UTF-16/UTF-32.
    try:
        decoded = raw.decode("utf-8")
    except UnicodeError:
        raise Error("preparation_encoding_invalid") from None
    if any(len(line) > MAX_LINE for line in raw.splitlines()):
        raise Error("preparation_line_invalid")
    return evidence._json(decoded)


def _numbers(value):
    if type(value) is dict:
        for item in value.values():
            _numbers(item)
    elif type(value) is not str:
        evidence._number(value, integer=True)


def _scan(raw, value, name, scan):
    if any(scan(token) for token in evidence._strings(value)):
        raise Error("sensitive_string")
    hits = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        categories = scan(line)
        if not isinstance(categories, set) or categories - {"possible-account-number"}:
            raise Error("unapproved_scan_category")
        if categories:
            hits.append(number)
    if not hits:
        return []
    return [
        dict(
            file=name,
            category="schema_validated_fictional_numeric",
            count=len(hits),
            first_line=hits[0],
            last_line=hits[-1],
        )
    ]


def validate_files(contents, expected):
    """Pure schema/scanning. Does not claim that the caller's provenance is true."""
    try:
        if type(contents) is not dict or set(contents) != set(FILES):
            raise Error("unexpected_file_set")
        validate_binding(expected)
        geometry, runtime = (_bounded_json(contents[name]) for name in FILES)
        _numbers(geometry)
        # The existing pure validator specifies geometry independently of OS.
        # Reusing its rules for one payload does NOT create a second export or
        # assert any observation from the other platform.
        plan._validate_inputs(
            dict(
                candidate=expected["candidate"],
                fixture_sha256=expected["fixture_sha256"],
                tool_sha256=expected["tool_sha256"],
                verification_sha256="a" * 64,
                environments={platform: "a" * 64 for platform in plan.PLATFORMS},
            ),
            {platform: geometry for platform in plan.PLATFORMS},
        )
        evidence._fields(runtime, ("environment", "python", "sqlite"))
        for field in ("python", "sqlite"):
            evidence._pattern(runtime[field], r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
        environment = runtime["environment"]
        evidence._fields(environment, ("cpu_count", "os", "release_sha256", "machine_sha256"))
        evidence._number(environment["cpu_count"], integer=True, positive=True)
        evidence._enum(environment["os"], {expected["platform"]})
        for field in ("release_sha256", "machine_sha256"):
            evidence._pattern(environment[field], r"[a-f0-9]{64}")
        scan = evidence.public_scanner()
        exceptions = _scan(canonical(expected), expected, "binding", scan)
        for name, value in zip(FILES, (geometry, runtime)):
            exceptions.extend(_scan(contents[name], value, name, scan))
        return exceptions
    except Error:
        raise
    except Exception:
        raise Error("preparation_validation_unavailable") from None


def _safe(path):
    path = Path(path)
    if ".." in path.parts:
        raise Error("path_traversal")
    return evidence._safe_path(path)


def _read_file(path, limit):
    path = _safe(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise Error("invalid_source_file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())

    def identity(s):
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink

    if identity(before) != identity(opened) or identity(before) != identity(after):
        raise Error("source_changed_or_linked")
    if len(raw) != before.st_size or len(raw) > limit:
        raise Error("file_size_limit")
    return raw


def _read_sources(source):
    source = _safe(source)
    if not source.is_dir() or {path.name for path in source.iterdir()} != set(FILES):
        raise Error("unexpected_file_set")
    return {name: _read_file(source / name, MAX_FILE) for name in FILES}


def _new(path):
    path = Path(path).absolute()
    _safe(path.parent)
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def _save_bytes(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _files(contents):
    return {name: dict(size=len(raw), sha256=digest(raw)) for name, raw in contents.items()}


def seal(source, target, recipient, expected):
    """Public-key-only local sealing; upload requires a separate authorization gate."""
    try:
        if version("pyrage") != "1.4.0":
            raise Error("tool_version_mismatch")
        contents = _read_sources(source)
        exceptions = validate_files(contents, expected)
        fingerprint = evidence.fingerprint(recipient)
        manifest = dict(
            format=FORMAT,
            purpose=PURPOSE,
            tool="pyrage",
            version="1.4.0",
            recipient_sha256=fingerprint,
            binding=expected,
            files=_files(contents),
            runtime_sha256=plan.digest(_bounded_json(contents["runtime.json"])),
            generic_exceptions=exceptions,
            private_scan="not_run_in_ci",
        )
        plain = io.BytesIO()
        with zipfile.ZipFile(plain, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, raw in contents.items():
                archive.writestr(name, raw)
            archive.writestr("archive-manifest.json", canonical(manifest))
        if plain.tell() > MAX_TOTAL:
            raise Error("archive_size_limit")
        plain.seek(0)
        target = _new(target)
        partial = target / (CIPHER + ".partial")
        with partial.open("xb") as output:
            pyrage.encrypt_io(plain, output, [evidence.recipient_type().from_str(recipient)])
            output.flush()
            os.fsync(output.fileno())
        ciphertext = _read_file(partial, MAX_TOTAL + MAX_FILE)
        if not ciphertext:
            raise Error("empty_ciphertext")
        partial.rename(target / CIPHER)
        receipt = dict(
            state="encrypted_pending_verification",
            format=FORMAT,
            purpose=PURPOSE,
            recipient_sha256=fingerprint,
            ciphertext_sha256=digest(ciphertext),
            ciphertext_bytes=len(ciphertext),
            file=CIPHER,
        )
        _save_bytes(target / "receipt.json", canonical(receipt))
        return receipt
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise Error("preparation_seal_failed") from None


class _BoundedOutput(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_TOTAL:
            raise Error("archive_size_limit")
        return super().write(data)


def _archive(plain, ident, expected):
    validate_binding(expected)
    with zipfile.ZipFile(plain) as archive:
        if archive.comment:
            raise Error("unexpected_archive_metadata")
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or set(names) != {*FILES, "archive-manifest.json"}:
            raise Error("unexpected_archive_entries")
        for entry in entries:
            if (
                entry.is_dir()
                or entry.orig_filename != entry.filename
                or entry.comment
                or entry.extra
                or entry.compress_type != zipfile.ZIP_STORED
                or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                or entry.flag_bits & 1
                or entry.file_size > MAX_FILE
                or entry.compress_size != entry.file_size
            ):
                raise Error("invalid_archive_entry")
        contents = {name: archive.read(name) for name in FILES}
        manifest_raw = archive.read("archive-manifest.json")
        manifest = _bounded_json(manifest_raw)
    evidence._fields(
        manifest,
        (
            "format",
            "purpose",
            "tool",
            "version",
            "recipient_sha256",
            "binding",
            "files",
            "runtime_sha256",
            "generic_exceptions",
            "private_scan",
        ),
    )
    if (
        manifest["format"] != FORMAT
        or manifest["purpose"] != PURPOSE
        or manifest["tool"] != "pyrage"
        or manifest["version"] != "1.4.0"
        or manifest["private_scan"] != "not_run_in_ci"
        or manifest["recipient_sha256"] != evidence.fingerprint(str(ident.to_public()))
    ):
        raise Error("archive_identity_mismatch")
    validate_binding(manifest["binding"])
    if canonical(manifest["binding"]) != canonical(expected):
        raise Error("preparation_identity_mismatch")
    if canonical(manifest["files"]) != canonical(_files(contents)):
        raise Error("file_digest_mismatch")
    if manifest["runtime_sha256"] != plan.digest(_bounded_json(contents["runtime.json"])):
        raise Error("runtime_digest_mismatch")
    exceptions = validate_files(contents, expected)
    if canonical(exceptions) != canonical(manifest["generic_exceptions"]):
        raise Error("scan_evidence_mismatch")
    return contents, manifest_raw, manifest


def open_bundle(ciphertext, target, ident, expected, *, private_scan):
    """Mandatory caller callback; never reads a real private list on its own."""
    target_path = None
    state = dict(
        state="verification_failed",
        accepted=False,
        purpose=PURPOSE,
        measurement_acceptance="not_applicable",
    )
    try:
        target_path = _new(target)
        if version("pyrage") != "1.4.0" or not callable(private_scan):
            raise Error("verification_unavailable")
        encrypted = _read_file(ciphertext, MAX_TOTAL + MAX_FILE)
        plain = _BoundedOutput()
        pyrage.decrypt_io(io.BytesIO(encrypted), plain, [ident])
        plain.seek(0)
        contents, manifest_raw, manifest = _archive(plain, ident, expected)
        raw_dir = _new(target_path / "raw")
        paths = []
        for name, raw in {**contents, "archive-manifest.json": manifest_raw}.items():
            path = raw_dir / name
            _save_bytes(path, raw)
            paths.append(path)
        outcome = private_scan(paths)
        if outcome not in {"pass", "fail", "unavailable"}:
            outcome = "unavailable"
        state.update(
            state={
                "pass": "verified_private_pass_generic_exception",
                "fail": "private_scan_failed",
                "unavailable": "encrypted_pending_verification",
            }[outcome],
            accepted=outcome == "pass",
            private_scan=outcome,
            files=manifest["files"],
            generic_exceptions=manifest["generic_exceptions"],
            runtime_sha256=manifest["runtime_sha256"],
            ciphertext_sha256=digest(encrypted),
        )
        _save_bytes(target_path / "state.json", canonical(state))
        return state
    except BaseException as exc:
        if target_path is not None and not (target_path / "state.json").exists():
            _save_bytes(target_path / "state.json", canonical(state))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise Error("verification_failed") from None
