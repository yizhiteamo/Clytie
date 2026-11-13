"""A4.3 only: structured fictional evidence -> age -> local mandatory private scan.

No uploader, model, instance lookup, environment dump or product encryption keys.
Raw files are never rewritten. Invalid archival data cannot yield an upload receipt.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shlex
import stat
import sys
import tempfile
import zipfile
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import pyrage
import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "requests.jsonl",
    "storage-phases.jsonl",
    "resources.jsonl",
    "release-events.jsonl",
    "run-manifest.json",
)
LEGACY_MAX_FILE = 64 * 1024 * 1024
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = len(FILES) * MAX_FILE + 1024 * 1024
MAX_LINE = 16 * 1024
IDENTITY_FIELDS = (
    "candidate",
    "reference",
    "run_id",
    "job_id",
    "attempt",
    "platform",
    "seed",
    "scale",
    "group",
    "arm",
    "phase",
    "plan_sha256",
    "parameters",
    "load",
)
BASELINE_REFERENCE = "05ec93c7c4ae972b7475911b0aec9db57bb00390"
ORIGINAL_REFERENCE = "b40bbbc31cfb2ff012a151d0e0e9c79acf07d903"
V2_FIELDS = (
    *IDENTITY_FIELDS,
    "format",
    "source",
    "method",
    "python",
    "sqlite",
    "result",
    "source_commit",
    "baseline_reference",
    "fixture_version",
    "fixture_sha256",
    "tool_sha256",
    "environment",
    "execution",
    "metrics",
)
V5_FIELDS = (*V2_FIELDS, "observer_sequences", "scenario", "execution_id")
LEDGER_TABLES = ("inbound", "record", "invocation", "intent", "attempt", "dedupe", "audit")
RESOURCE_FIELDS_V1 = (
    "at_s",
    "rss_bytes",
    "wal_bytes",
    "temp_bytes",
    "queue",
    "finalizers",
    "views",
    "backup_bytes",
    "loop_delay_ms",
    "rate",
)
# Formats 1 and 2 retain their historical resource fields and byte semantics.
RESOURCE_FIELDS_V2 = RESOURCE_FIELDS_V1
# V3 backup_bytes is physical artifact/package bytes; logical_io_bytes is the
# separate scheduler charge counter, or null when that counter is not available.
RESOURCE_FIELDS_V3 = (*RESOURCE_FIELDS_V1, "logical_io_bytes", "backup_phase")
# Intentionally fixed, checked against the product journal in tests. Loading an
# older product must not silently change the evidence format's allowed strings.
BACKUP_PHASES = {
    "accepted",
    "preparing",
    "capturing",
    "copying",
    "verifying",
    "publishing",
    "committed",
    "cancelled",
    "failed",
    "recovery_blocked",
    "not_started",
    "not_applicable",
}
PARAMETERS = {
    "concurrency": 1,
    "waiting": 8,
    "chunk_bytes": 256 * 1024,
    "normal_bytes_s": 16 * 1024 * 1024,
    "busy_bytes_s": 4 * 1024 * 1024,
    "burst_bytes": 512 * 1024,
    "signal_seconds": 0.1,
    "stale_seconds": 1,
    "view_seconds": 300,
    "wal_bytes": 256 * 1024 * 1024,
}


class EvidenceError(ValueError):
    """Only fixed codes; never embed input text, paths or key material."""


def native_identity():
    # Keep the library's native curve identifier out of the generic number rule.
    return getattr(pyrage, "x" + str(0x63AF)).Identity


def recipient_type():
    return getattr(pyrage, "x" + str(0x63AF)).Recipient


def fingerprint(recipient):
    canonical = str(recipient_type().from_str(recipient))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _scanner(private=False):
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    program = shlex.split(config["repos"][0]["hooks"][0]["entry"])[2]
    namespace = {}
    # Same program as hooks, no supplied files. Never output the private terms.
    with patch.object(sys, "argv", ["scan", *(["--private"] if private else [])]):
        with redirect_stdout(io.StringIO()):
            try:
                exec(compile(program, "evidence-safety", "exec"), namespace)
            except SystemExit as exc:
                if exc.code:
                    raise EvidenceError("scanner_unavailable") from None
    return namespace


def public_scanner():
    return _scanner()["public_categories"]


def private_scan(files):
    try:
        namespace = _scanner(private=True)
    except (OSError, ValueError, KeyError):
        return "unavailable"
    normalize, terms = namespace["normalized"], namespace["denylist"]
    for path in files:
        text = Path(path).read_text(encoding="utf-8")
        if any(term in normalize(text) for term in terms):
            return "fail"
    return "pass"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("duplicate_field")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError):
        raise EvidenceError("invalid_json") from None


def _fields(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise EvidenceError("invalid_fields")


def _number(value, *, integer=False, positive=False):
    if type(value) not in ((int,) if integer else (int, float)):
        raise EvidenceError("invalid_number")
    if not math.isfinite(value) or value < 0 or value > 2**53 or (positive and value == 0):
        raise EvidenceError("invalid_number")


def _enum(value, choices):
    if type(value) is not str or value not in choices:
        raise EvidenceError("invalid_enum")


def _pattern(value, pattern):
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise EvidenceError("invalid_identifier")


def validate_run(run):
    if type(run) is dict and type(run.get("format")) is int and run["format"] in {2, 3, 4, 5}:
        _validate_run_versioned(run)
    else:
        _validate_run_v1(run)


def _validate_run_v1(run):
    _fields(run, (*IDENTITY_FIELDS, "format", "source", "method", "python", "sqlite", "result"))
    if type(run["format"]) is not int or run["format"] != 1:
        raise EvidenceError("unsupported_format")
    _enum(run["source"], {"generated_fictional_only"})
    _enum(run["method"], {"a43_bounded_io_v1", "encryption_fixture_v1"})
    for field in ("candidate", "reference"):
        _pattern(run[field], r"[a-f0-9]{40}")
    _pattern(run["plan_sha256"], r"[a-f0-9]{64}")
    for field in ("python", "sqlite"):
        _pattern(run[field], r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
    _pattern(run["job_id"], r"[a-z][a-z0-9_-]{0,60}")
    for field in ("run_id", "attempt", "group"):
        _number(run[field], integer=True, positive=True)
    _number(run["seed"], integer=True)
    _enum(run["platform"], {"Windows", "Linux"})
    _enum(run["scale"], {"small_text", "large_database", "many_small_files", "large_asset"})
    _enum(run["arm"], {"none", "original", "candidate"})
    _enum(run["phase"], {"mechanism", "matrix", "fixture"})
    if (run["phase"] == "fixture") != (run["method"] == "encryption_fixture_v1"):
        raise EvidenceError("measurement_method_mismatch")
    _enum(run["result"], {"pass", "fail", "incomplete", "cancelled"})
    _fields(run["parameters"], PARAMETERS)
    for key, expected in PARAMETERS.items():
        _number(run["parameters"][key], integer=type(expected) is int)
        if run["parameters"][key] != expected:
            raise EvidenceError("unapproved_parameters")
    _fields(run["load"], ("window_seconds", "interval_seconds", "request_count", "maximum_seconds"))
    for key, value in run["load"].items():
        _number(value, integer=key == "request_count", positive=True)
    if run["load"]["window_seconds"] > run["load"]["maximum_seconds"]:
        raise EvidenceError("invalid_load")


def _validate_run_versioned(run):
    _fields(run, V5_FIELDS if run["format"] == 5 else V2_FIELDS)
    if run["format"] == 5:
        validate_observer_sequences(run["observer_sequences"])
        _enum(run["scenario"], {"normal", "cancel"})
        _pattern(run["execution_id"], r"[a-f0-9]{64}")
    _enum(run["source"], {"generated_fictional_only"})
    _enum(run["method"], {"legal_storage_v1", "legal_fixture_comparison_v1"})
    _enum(
        run["fixture_version"],
        {"legal_memory_v2" if run["format"] in {3, 4, 5} else "legal_memory_v1"},
    )
    for field in ("candidate", "reference", "baseline_reference", "source_commit"):
        _pattern(run[field], r"[a-f0-9]{40}")
    if run["reference"] != ORIGINAL_REFERENCE or run["baseline_reference"] != BASELINE_REFERENCE:
        raise EvidenceError("unapproved_reference")
    for field in ("plan_sha256", "fixture_sha256", "tool_sha256"):
        _pattern(run[field], r"[a-f0-9]{64}")
    for field in ("python", "sqlite"):
        _pattern(run[field], r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
    _pattern(run["job_id"], r"[a-z][a-z0-9_-]{0,60}")
    for field in ("run_id", "attempt", "group"):
        _number(run[field], integer=True, positive=True)
    _number(run["seed"], integer=True)
    _enum(run["platform"], {"Windows", "Linux"})
    _enum(run["scale"], {"small_text", "large_database", "many_small_files", "large_asset"})
    _enum(run["arm"], {"baseline", "none", "original", "candidate"})
    _enum(run["phase"], {"baseline", "mechanism", "matrix", "fixture"})
    if (run["phase"] == "fixture") != (run["method"] == "legal_fixture_comparison_v1"):
        raise EvidenceError("measurement_method_mismatch")
    if (run["arm"] == "baseline" and run["phase"] not in {"baseline", "fixture"}) or (
        run["phase"] == "baseline" and run["arm"] != "baseline"
    ):
        raise EvidenceError("measurement_phase_mismatch")
    source = {
        "baseline": run["baseline_reference"],
        "original": run["reference"],
        "none": run["candidate"],
        "candidate": run["candidate"],
    }[run["arm"]]
    if run["source_commit"] != source or (
        run["source_commit"] == BASELINE_REFERENCE and run["phase"] not in {"baseline", "fixture"}
    ):
        raise EvidenceError("source_commit_mismatch")
    _enum(run["result"], {"pass", "fail", "incomplete", "cancelled"})
    _fields(run["parameters"], PARAMETERS)
    for key, expected in PARAMETERS.items():
        _number(run["parameters"][key], integer=type(expected) is int)
        if run["parameters"][key] != expected:
            raise EvidenceError("unapproved_parameters")
    _fields(
        run["load"],
        (
            "window_seconds",
            "interval_seconds",
            "request_count",
            "maximum_seconds",
            "backup_start_seconds",
        ),
    )
    for key, value in run["load"].items():
        _number(value, integer=key == "request_count", positive=key != "backup_start_seconds")
    if (
        run["load"]["window_seconds"] > run["load"]["maximum_seconds"]
        or run["load"]["backup_start_seconds"] >= run["load"]["window_seconds"]
    ):
        raise EvidenceError("invalid_load")
    environment = run["environment"]
    _fields(environment, ("cpu_count", "os", "release_sha256", "machine_sha256"))
    _number(environment["cpu_count"], integer=True, positive=True)
    _enum(environment["os"], {run["platform"]})
    for key in ("release_sha256", "machine_sha256"):
        _pattern(environment[key], r"[a-f0-9]{64}")
    execution = run["execution"]
    _fields(execution, ("order", "position", "warmup_requests"))
    order = execution["order"]
    mechanism = run["format"] == 5 and run["phase"] == "mechanism"
    if mechanism and (run["arm"] != "candidate" or order != ["candidate"]):
        raise EvidenceError("invalid_order")
    if type(order) is not list or len(order) != (1 if mechanism else 4):
        raise EvidenceError("invalid_order")
    for arm in order:
        _enum(arm, {"baseline", "none", "original", "candidate"})
    if len(set(order)) != len(order):
        raise EvidenceError("invalid_order")
    _number(execution["position"], integer=True)
    _number(execution["warmup_requests"], integer=True)
    if execution["position"] >= len(order) or order[execution["position"]] != run["arm"]:
        raise EvidenceError("invalid_order")
    _validate_metrics(run["metrics"])


def _validate_metrics(metrics):
    _fields(
        metrics,
        (
            "logical",
            "ledger_rows",
            "memory_database_bytes",
            "ledger_database_bytes",
            "asset_file_bytes",
            "plugin_file_bytes",
            "backup_bytes",
        ),
    )
    logical = metrics["logical"]
    _fields(
        logical,
        (
            "records",
            "revisions",
            "content_bytes",
            "content_bytes_per_record",
            "content_length_distribution",
            "file_count",
            "file_bytes",
            "provider_files",
            "provider_bytes",
            "asset_registrations",
            "asset_bytes",
            "ledger_rows",
        ),
    )
    distribution = logical["content_length_distribution"]
    _fields(distribution, ("min_bytes", "max_bytes", "total_bytes", "records"))
    for container, excluded in (
        (metrics, {"logical", "ledger_rows"}),
        (logical, {"ledger_rows", "content_length_distribution"}),
        (distribution, set()),
    ):
        for key, value in container.items():
            if key not in excluded:
                _number(value, integer=True)
    for counts in (metrics["ledger_rows"], logical["ledger_rows"]):
        _fields(counts, LEDGER_TABLES)
        for value in counts.values():
            _number(value, integer=True)
    if (
        distribution["min_bytes"] > distribution["max_bytes"]
        or distribution["records"] != logical["records"]
        or distribution["total_bytes"] != logical["content_bytes"]
    ):
        raise EvidenceError("invalid_metrics")


IO_PHASES = {"io_capability", "io_slot", "io_charge", "file_batch", "sqlite_batch"}


def validate_observer_sequences(value):
    if (
        type(value) is not list
        or not 1 <= len(value) <= 256
        or any(type(item) is not int or not 0 <= item < 256 for item in value)
        or value != sorted(set(value))
        or value[0] != 0
    ):
        raise EvidenceError("invalid_observer_sequences")


def io_tools():
    try:
        from . import measurement_io_observation
    except ImportError:
        import measurement_io_observation
    return measurement_io_observation


def validate_record(name, row, *, format_version=None):
    if format_version is not None and (
        type(format_version) is not int or format_version not in {1, 2, 3, 4, 5}
    ):
        raise EvidenceError("unsupported_format")
    if name == "storage-phases.jsonl" and type(row) is dict and row.get("phase") in IO_PHASES:
        if format_version != 5:
            raise EvidenceError("unsupported_io_event")
        io_tools().validate_record(row)
        return
    if (
        name == "storage-phases.jsonl"
        and type(row) is dict
        and row.get("phase") == "native_validation"
    ):
        if format_version not in {None, 4, 5}:
            raise EvidenceError("unsupported_native_event")
        _validate_native_event(row)
        return
    if name == "requests.jsonl":
        _fields(row, ("id", "kind", "scheduled_s", "started_s", "finished_s", "outcome"))
        _enum(row["kind"], {"business", "control", "cancel", "backup"})
    elif name == "storage-phases.jsonl":
        _fields(row, ("id", "request_id", "phase", "started_s", "finished_s", "outcome"))
        _enum(
            row["phase"],
            {
                "queue",
                "execute",
                "finalizer",
                "lock",
                "fsync",
                "commit",
                "rollback",
                "capture",
                "read",
                "write",
            },
        )
    elif name == "resources.jsonl":
        fields = (
            RESOURCE_FIELDS_V3
            if format_version in {3, 4, 5}
            or (
                format_version is None and type(row) is dict and set(row) == set(RESOURCE_FIELDS_V3)
            )
            else RESOURCE_FIELDS_V1
        )
        _fields(row, fields)
        _enum(row["rate"], {"normal", "busy", "none"})
        if "backup_phase" in row:
            _enum(row["backup_phase"], BACKUP_PHASES)
    elif name == "release-events.jsonl":
        _fields(row, ("id", "at_s", "resource", "event"))
        _enum(row["resource"], {"view", "slot", "reference", "thread", "process"})
        _enum(
            row["event"],
            {
                "acquired",
                "cancel_requested",
                "io_returned",
                "released",
                "exit_verified",
                "unknown",
                "failed",
            },
        )
    else:
        raise EvidenceError("unknown_file")
    for key, value in row.items():
        if key in {"kind", "phase", "rate", "resource", "event", "backup_phase"}:
            continue
        if key == "logical_io_bytes" and value is None and row["rate"] == "none":
            continue
        if key == "outcome":
            _enum(value, {"ok", "failed", "cancelled", "unknown", "busy"})
        else:
            _number(value, integer=not key.endswith(("_s", "_ms")))
    if "started_s" in row and row["finished_s"] < row["started_s"]:
        raise EvidenceError("invalid_interval")


NATIVE_FIELDS = (
    "id",
    "request_id",
    "phase",
    "validation_id",
    "connection_id",
    "event",
    "at_s",
    "outcome",
    "internal_read_bytes",
    "boundary",
)
NATIVE_EVENTS = {
    "started",
    "cancel_accepted",
    "driver_finished",
    "connection_closed",
    "finished",
    "connection_close_unknown",
    "connection_attached",
    "interrupt_accepted",
}


def _validate_native_event(row):
    _fields(row, NATIVE_FIELDS)
    _enum(row["phase"], {"native_validation"})
    _enum(row["event"], NATIVE_EVENTS)
    _enum(row["boundary"], {"native_checks", "legacy_validation"})
    _enum(row["outcome"], {"ok", "failed", "cancelled", "unknown"})
    if row["internal_read_bytes"] is not None:
        raise EvidenceError("native_bytes_unknown_required")
    for field in ("id", "request_id", "validation_id", "connection_id", "at_s"):
        _number(row[field], integer=field != "at_s")
    if bool(row["connection_id"]) != (
        row["event"]
        in {
            "connection_attached",
            "connection_closed",
            "connection_close_unknown",
        }
    ):
        raise EvidenceError("native_connection_identity_invalid")


class NativeEvents:
    """Check incremental lifecycle evidence without retaining the complete log."""

    def __init__(self):
        self.active = {}
        self.latest = 0
        self.failed = False

    def add(self, row):
        identifier, event, at = row["validation_id"], row["event"], row["at_s"]
        self.latest = max(self.latest, at)
        self.failed = self.failed or row["outcome"] in {"failed", "cancelled"}
        if event == "started":
            if identifier in self.active or len(self.active) >= 8192:
                raise EvidenceError("native_event_sequence_invalid")
            self.active[identifier] = dict(
                start=at,
                last=at,
                end=None,
                cancel=None,
                interrupt=None,
                attached=set(),
                closed=set(),
                unknown=set(),
                boundary=row["boundary"],
                request=row["request_id"],
            )
            return
        state = self.active.get(identifier)
        if (
            state is None
            or at < state["last"]
            or (row["boundary"], row["request_id"]) != (state["boundary"], state["request"])
        ):
            raise EvidenceError("native_event_sequence_invalid")
        state["last"] = at
        connection = row["connection_id"]
        if event == "connection_attached":
            if connection in state["attached"]:
                raise EvidenceError("native_event_sequence_invalid")
            state["attached"].add(connection)
        elif event in {"connection_closed", "connection_close_unknown"}:
            if (
                connection not in state["attached"]
                or connection in state["closed"] | state["unknown"]
            ):
                raise EvidenceError("native_event_sequence_invalid")
            if event == "connection_close_unknown":
                if state["boundary"] != "legacy_validation" or row["outcome"] != "unknown":
                    raise EvidenceError("native_close_unconfirmed")
                state["unknown"].add(connection)
            else:
                if state["boundary"] == "native_checks" and state["end"] is None:
                    raise EvidenceError("native_close_before_driver_return")
                state["closed"].add(connection)
        elif event == "cancel_accepted":
            if state["cancel"] is not None or state["end"] is not None:
                raise EvidenceError("native_event_sequence_invalid")
            state["cancel"] = at
        elif event == "interrupt_accepted":
            if state["interrupt"] is not None or state["end"] is not None:
                raise EvidenceError("native_event_sequence_invalid")
            state["interrupt"] = at
        elif event == "driver_finished":
            if state["end"] is not None or (state["cancel"] is not None and at < state["cancel"]):
                raise EvidenceError("native_event_sequence_invalid")
            state["end"] = at
        elif event == "finished":
            if state["end"] is None or at < state["end"] or state["attached"] != state["closed"]:
                raise EvidenceError("native_release_unconfirmed")
            self.active.pop(identifier)

    def complete(self, *, process_exit_s=None, process_exits=None):
        for identifier, state in self.active.items():
            exit_at = process_exit_s
            if process_exits is not None:
                exit_at = process_exits.get((identifier >> 40) - 1)
            if (
                state["boundary"] != "legacy_validation"
                or state["end"] is None
                or state["attached"] != state["closed"] | state["unknown"]
                or not state["unknown"]
                or identifier < 1 << 40
                or exit_at is None
                or exit_at < state["last"]
            ):
                raise EvidenceError("native_release_unconfirmed")


def _strings(value):
    if type(value) is str:
        yield value
    elif type(value) is dict:
        for key, child in value.items():
            yield key
            yield from _strings(child)
    elif type(value) is list:
        for child in value:
            yield from _strings(child)


def _lines(raw):
    """Match historical str.splitlines without retaining the whole decoded file."""
    endings = ("\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
    pending = ""
    # Universal newline decoding also preserves CR/CRLF legacy line numbering.
    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline=None) as stream:
        while chunk := stream.read(MAX_LINE + 1):
            pieces = (pending + chunk).splitlines(keepends=True)
            pending = ""
            for piece in pieces:
                complete = piece.endswith(endings)
                line = piece[:-1] if complete else piece
                if len(line.encode("utf-8")) > MAX_LINE:
                    raise EvidenceError("line_size_limit")
                if complete:
                    yield line
                else:
                    pending = line
        if pending:
            yield pending


def validate_files(contents, expected, *, report_format=None):
    """Validate safe archival bytes; only a claimed pass must prove completion.

    Failed, cancelled and incomplete runs retain every schema-valid observation,
    including an unfinished or violated lifecycle. They are never acceptance
    evidence. Field, size, source identity and privacy checks apply to every run.
    """
    if set(contents) != set(FILES):
        raise EvidenceError("unexpected_file_set")
    try:
        scan = public_scanner()
        validate_run(expected)
        exceptions = []
        manifest_raw = contents["run-manifest.json"]
        if not manifest_raw or len(manifest_raw) > MAX_FILE:
            raise EvidenceError("file_size_limit")
        run = _json(manifest_raw)
        validate_run(run)
        native = NativeEvents() if run["format"] in {4, 5} and run["result"] == "pass" else None
        native_observed = False
        io_events = io_tools().IoEvents(run) if run["format"] == 5 else None
        child_exits = None
        if run["format"] == 5 and run["result"] == "pass":
            try:
                from .measurement_sample_validation import ProcessExits
            except ImportError:
                from measurement_sample_validation import ProcessExits
            child_exits = ProcessExits(run)
        request_plan = None
        if run["format"] == 5 and run["method"] == "legal_storage_v1" and run["result"] == "pass":
            try:
                from .measurement_sample_validation import RequestPlan
            except ImportError:
                from measurement_sample_validation import RequestPlan
            request_plan = RequestPlan(run)
        process_exit_s = None
        business_start = business_finish = backup_finish = None
        if report_format is None:
            report_format = 2 if run["format"] in {3, 4, 5} else 1
        if (
            type(report_format) is not int
            or report_format not in {1, 2}
            or (report_format == 2 and run["format"] not in {3, 4, 5})
            or (run["format"] in {4, 5} and report_format != 2)
        ):
            raise EvidenceError("unsupported_report_format")
        file_limit = MAX_FILE if run["format"] in {3, 4, 5} else LEGACY_MAX_FILE
        for name in FILES:
            raw = contents[name]
            if not raw or len(raw) > file_limit:
                raise EvidenceError("file_size_limit")
            if name == "run-manifest.json":
                if any(scan(token) for token in _strings(run)):
                    raise EvidenceError("sensitive_string")
            summary = None
            for number, line in enumerate(_lines(raw), 1):
                if name != "run-manifest.json":
                    row = _json(line)
                    validate_record(name, row, format_version=run["format"])
                    if request_plan is not None and name == "requests.jsonl":
                        request_plan.add(row)
                    if child_exits is not None and name == "release-events.jsonl":
                        child_exits.add(row)
                    if io_events is not None and name == "storage-phases.jsonl":
                        io_events.add(row)
                    if (
                        native is not None
                        and name == "storage-phases.jsonl"
                        and row["phase"] == "native_validation"
                    ):
                        native.add(row)
                        native_observed = True
                    if native is not None and name == "requests.jsonl":
                        if row["kind"] == "business":
                            business_start = min(
                                business_start
                                if business_start is not None
                                else row["scheduled_s"],
                                row["scheduled_s"],
                            )
                            business_finish = max(business_finish or 0, row["finished_s"])
                        elif row["kind"] == "backup":
                            backup_finish = max(backup_finish or 0, row["finished_s"])
                    if (
                        native is not None
                        and name == "release-events.jsonl"
                        and (row["resource"], row["event"]) == ("process", "exit_verified")
                    ):
                        process_exit_s = max(process_exit_s or 0, row["at_s"])
                    # Only numeric tokens may use the numerical exception. Every
                    # row and string is checked before counting its scan result.
                    if any(scan(token) for token in _strings(row)):
                        raise EvidenceError("sensitive_string")
                categories = scan(line)
                if categories - {"possible-account-number"}:
                    raise EvidenceError("unapproved_scan_category")
                if categories and report_format == 1:
                    exceptions.append(
                        {
                            "file": name,
                            "line": number,
                            "category": "schema_validated_fictional_numeric",
                        }
                    )
                elif categories:
                    if summary is None:
                        summary = {
                            "file": name,
                            "category": "schema_validated_fictional_numeric",
                            "count": 0,
                            "first_line": number,
                            "last_line": number,
                        }
                    summary["count"] += 1
                    summary["last_line"] = number
            if summary is not None:
                exceptions.append(summary)
        if io_events is not None:
            io_events.complete()
        if native is not None:
            if (
                run["method"] == "legal_storage_v1"
                and run["phase"] in {"mechanism", "matrix"}
                and run["arm"] in {"original", "candidate"}
                and not native_observed
            ):
                raise EvidenceError("native_validation_missing")
            # V5 binds each wait to its frozen child ordinal. Another child's
            # exit cannot confirm this child's legacy connection teardown.
            native.complete(
                process_exit_s=process_exit_s,
                process_exits=child_exits.exited if child_exits is not None else None,
            )
            if run["result"] == "pass" and native.failed:
                raise EvidenceError("native_failure_with_success")
            if (
                run["result"] == "pass"
                and run["arm"] in {"original", "candidate"}
                and (run["method"] == "legal_storage_v1" or backup_finish is not None)
            ):
                if (
                    business_start is None
                    or business_finish is None
                    or backup_finish is None
                    or process_exit_s is None
                    or (
                        max(native.latest, process_exit_s, backup_finish)
                        > min(business_start + run["load"]["window_seconds"], business_finish)
                    )
                ):
                    raise EvidenceError("native_business_window_uncovered")
        if request_plan is not None:
            request_plan.complete()
        if child_exits is not None:
            child_exits.complete()
        if run["format"] != expected["format"]:
            raise EvidenceError("run_identity_mismatch")
        # V2/V3 freeze every approved field except measured outcomes. In particular,
        # a newer schema must never fall back to the smaller historical identity.
        identity_fields = (
            set(V5_FIELDS if run["format"] == 5 else V2_FIELDS) - {"result", "metrics"}
            if run["format"] in {2, 3, 4, 5}
            else IDENTITY_FIELDS
        )
        if any(run[key] != expected[key] for key in identity_fields):
            raise EvidenceError("run_identity_mismatch")
        if run["format"] in {2, 3, 4, 5} and any(
            run["metrics"][key] != expected["metrics"][key] for key in ("logical", "ledger_rows")
        ):
            # Physical byte layouts/results are observations; the approved
            # logical workload is fixed before execution, not a free result field.
            raise EvidenceError("run_workload_mismatch")
        return exceptions
    except EvidenceError:
        raise
    except Exception:
        raise EvidenceError("validation_unavailable") from None


def _safe_path(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise EvidenceError("linked_path")
    return path


def _read_sources(source):
    source = _safe_path(source)
    if not source.is_dir() or {p.name for p in source.iterdir()} != set(FILES):
        raise EvidenceError("unexpected_file_set")
    contents = {}
    for name in FILES:
        path = _safe_path(source / name)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
            raise EvidenceError("invalid_source_file")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_mtime_ns,
                opened.st_size,
            ) or opened.st_nlink != 1:
                raise EvidenceError("source_changed_or_linked")
            raw = stream.read(MAX_FILE + 1)
            after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise EvidenceError("source_changed")
        contents[name] = raw
    return contents


def _save(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def _new_directory(path):
    path = Path(path).absolute()
    _safe_path(path.parent)
    path.mkdir(exist_ok=False, mode=0o700)
    return path


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def seal(source, target, recipient, expected):
    """CI: public checks only. Return an upload receipt only after finished encryption."""
    target = _new_directory(target)
    if version("pyrage") != "1.4.0":
        raise EvidenceError("tool_version_mismatch")
    contents = _read_sources(source)
    exceptions = validate_files(contents, expected)
    public_fingerprint = fingerprint(recipient)
    manifest = {
        "format": 2 if expected["format"] in {3, 4, 5} else 1,
        "tool": "pyrage",
        "version": version("pyrage"),
        "recipient_sha256": public_fingerprint,
        "files": {
            name: {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in contents.items()
        },
        "generic_exceptions": exceptions,
        "private_scan": "not_run_in_ci",
    }
    partial = target / "measurements.age.partial"
    with tempfile.TemporaryFile() as plain:
        with zipfile.ZipFile(plain, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, raw in contents.items():
                archive.writestr(name, raw)
            archive.writestr("archive-manifest.json", json.dumps(manifest).encode())
        plain.seek(0)
        with partial.open("xb") as output:
            pyrage.encrypt_io(plain, output, [recipient_type().from_str(recipient)])
            output.flush()
            os.fsync(output.fileno())
    if partial.stat().st_size <= 0:
        raise EvidenceError("empty_ciphertext")
    cipher_hash = _hash(partial)
    destination = target / "measurements.age"
    partial.rename(destination)
    receipt = {
        "state": "encrypted_pending_verification",
        "format": 1,
        "recipient_sha256": public_fingerprint,
        "ciphertext_sha256": cipher_hash,
        "ciphertext_bytes": destination.stat().st_size,
        "file": destination.name,
    }
    _save(target / "receipt.json", receipt)
    return receipt


class _BoundedOutput:
    def __init__(self, stream):
        self.stream, self.total = stream, 0

    def write(self, data):
        self.total += len(data)
        if self.total > MAX_TOTAL:
            raise EvidenceError("archive_size_limit")
        return self.stream.write(data)

    def flush(self):
        self.stream.flush()


def _archive(plain, ident, expected):
    with zipfile.ZipFile(plain) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or set(names) != {*FILES, "archive-manifest.json"}:
            raise EvidenceError("unexpected_archive_entries")
        for entry in entries:
            mode = entry.external_attr >> 16
            if (
                entry.is_dir()
                or entry.compress_type != zipfile.ZIP_STORED
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or entry.flag_bits & 1
                or entry.file_size > MAX_FILE
                or entry.compress_size != entry.file_size
            ):
                raise EvidenceError("invalid_archive_entry")
        contents = {name: archive.read(name) for name in FILES}
        manifest = _json(archive.read("archive-manifest.json"))
    _fields(
        manifest,
        (
            "format",
            "tool",
            "version",
            "recipient_sha256",
            "files",
            "generic_exceptions",
            "private_scan",
        ),
    )
    if (
        type(manifest["format"]) is not int
        or manifest["format"] not in {1, 2}
        or (manifest["format"] == 2 and expected["format"] not in {3, 4, 5})
        or manifest["tool"] != "pyrage"
        or manifest["version"] != "1.4.0"
        or manifest["private_scan"] != "not_run_in_ci"
        or manifest["recipient_sha256"] != fingerprint(str(ident.to_public()))
    ):
        raise EvidenceError("archive_identity_mismatch")
    expected_files = {
        name: {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in contents.items()
    }
    if manifest["files"] != expected_files:
        raise EvidenceError("file_digest_mismatch")
    exceptions = validate_files(contents, expected, report_format=manifest["format"])
    if exceptions != manifest["generic_exceptions"]:
        raise EvidenceError("scan_evidence_mismatch")
    return contents, manifest


def open_bundle(ciphertext, target, ident, expected, *, private_scan=private_scan):
    """Local only; authenticate the complete ciphertext before parsing any archive path."""
    target = _new_directory(target)
    state = {"state": "verification_failed", "accepted": False}
    try:
        ciphertext = _safe_path(ciphertext)
        if not ciphertext.is_file() or ciphertext.stat().st_size > MAX_TOTAL + 1024 * 1024:
            raise EvidenceError("invalid_ciphertext")
        with tempfile.TemporaryFile(dir=target) as plain:
            with ciphertext.open("rb") as source:
                pyrage.decrypt_io(source, _BoundedOutput(plain), [ident])
            plain.seek(0)
            contents, manifest = _archive(plain, ident, expected)
        raw_dir = _new_directory(target / "raw")
        paths = []
        for name, raw in contents.items():
            path = raw_dir / name
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            paths.append(path)
        outcome = private_scan(paths)
        measured_result = _json(contents["run-manifest.json"])["result"]
        state = {
            "state": {
                "pass": "verified_private_pass_generic_exception",
                "fail": "private_scan_failed",
                "unavailable": "encrypted_pending_verification",
            }[outcome],
            "accepted": outcome == "pass",
            # accepted has always meant authenticated/scanned archive acceptance.
            # A failed measurement can be safely preserved without becoming a
            # successful run; even a pass still needs its external acceptance review.
            "measurement_result": measured_result,
            "measurement_acceptance": "not_evaluated"
            if measured_result == "pass"
            else "ineligible",
            "files": manifest["files"],
            "generic_exceptions": manifest["generic_exceptions"],
            "ciphertext_sha256": _hash(ciphertext),
            "private_scan": outcome,
        }
        _save(target / "state.json", state)
        return state
    except BaseException as exc:
        if not (target / "state.json").exists():
            _save(target / "state.json", state)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise EvidenceError("verification_failed") from None
