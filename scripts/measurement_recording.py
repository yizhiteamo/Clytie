"""Bounded, content-free measurement collection; no uploader or instance access.

Samples remain in memory during the measured interval, including failures. Their
memory cost is part of measured RSS. Final serialization is outside that interval;
it is not durable archival until the authorized encryption/local verification flow.
"""

import hashlib
import json
import os
import threading
from pathlib import Path

try:
    from . import measurement_evidence as evidence
except ImportError:
    import measurement_evidence as evidence


class Recorder:
    def __init__(self, *, max_file_bytes=None, format_version=None):
        if format_version is not None and (
            type(format_version) is not int or format_version not in {1, 2, 3, 4, 5, 6}
        ):
            raise evidence.EvidenceError("unsupported_format")
        self.format_version = format_version
        self.limit = evidence.MAX_FILE if max_file_bytes is None else max_file_bytes
        if type(self.limit) is not int or not 0 < self.limit <= evidence.MAX_FILE:
            raise evidence.EvidenceError("recording_invalid_limit")
        self.rows = {name: bytearray() for name in evidence.FILES[:-1]}
        self.lock = threading.Lock()
        self.closed = self.failed = False

    def append(self, name, row):
        with self.lock:
            if self.closed:
                raise evidence.EvidenceError("recording_closed")
            if self.failed:
                raise evidence.EvidenceError("recording_invalid")
            try:
                if name not in self.rows:
                    raise evidence.EvidenceError("unknown_file")
                evidence.validate_record(name, row, format_version=self.format_version)
                raw = (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode()
                if len(raw) > evidence.MAX_LINE or len(self.rows[name]) + len(raw) > self.limit:
                    raise evidence.EvidenceError("recording_capacity")
                self.rows[name].extend(raw)
            except Exception:
                # A recorder error invalidates the whole run; never discard only
                # the offending/slow sample and continue claiming complete data.
                self.failed = True
                raise

    def finish(self, directory, run):
        with self.lock:
            if self.closed:
                raise evidence.EvidenceError("recording_closed")
            if self.failed:
                raise evidence.EvidenceError("recording_invalid")
            if self.format_version is not None and run.get("format") != self.format_version:
                self.failed = True
                raise evidence.EvidenceError("recording_format_mismatch")
            self.closed = True
            contents = {name: bytes(raw) for name, raw in self.rows.items()}
        contents["run-manifest.json"] = json.dumps(run, separators=(",", ":")).encode()
        # Full generic scan is deliberately outside the timed workload. The CI
        # encryptor repeats validation, so this is never an upload permission.
        evidence.validate_files(contents, run)
        directory = Path(directory).absolute()
        evidence._safe_path(directory.parent)
        if directory.exists() or directory.is_symlink():
            raise evidence.EvidenceError("recording_destination_exists")
        directory.mkdir(mode=0o700)
        for name in evidence.FILES:
            with (directory / name).open("xb") as stream:
                stream.write(contents[name])
                stream.flush()
                os.fsync(stream.fileno())
        return {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}

    def append_bytes(self, name, raw):
        """Join complete child records without re-encoding their original bytes."""
        with self.lock:
            if self.closed:
                raise evidence.EvidenceError("recording_closed")
            if self.failed:
                raise evidence.EvidenceError("recording_invalid")
            try:
                if name not in self.rows:
                    raise evidence.EvidenceError("unknown_file")
                if type(raw) is not bytes or not raw or not raw.endswith(b"\n"):
                    raise evidence.EvidenceError("child_recording_incomplete")
                if len(self.rows[name]) + len(raw) > self.limit:
                    raise evidence.EvidenceError("recording_capacity")
                for line in raw.splitlines(keepends=True):
                    if len(line) > evidence.MAX_LINE:
                        raise evidence.EvidenceError("line_size_limit")
                    evidence.validate_record(
                        name, evidence._json(line), format_version=self.format_version
                    )
                self.rows[name].extend(raw)
            except Exception:
                self.failed = True
                raise
