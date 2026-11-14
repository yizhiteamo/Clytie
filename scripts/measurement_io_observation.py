"""Format 5: actual host batches, distinct from budget accounting.

Only host io_read/io_write calls and SQLite backup progress are observed. Plugin
I/O bypassing those interfaces is not covered. Native validation bytes remain
unknown. No product parameters, checkpoints, rate limits or SQL are replaced.
"""

import asyncio
import functools
import hashlib
import importlib
import sys
from unittest.mock import patch

try:
    from . import measurement_evidence as evidence
except ImportError:
    import measurement_evidence as evidence

BASE = ("id", "process_sequence", "phase", "at_s")
TRANSFER = (*BASE, "transfer_id", "stage_id", "event", "outcome")
FILE = (*TRANSFER, "direction", "requested_bytes", "actual_bytes")
CHARGE = (*TRANSFER, "budget_id", "direction", "requested_bytes", "charged_bytes")
SQLITE = (
    *TRANSFER,
    "pages_per_step",
    "sleep_seconds",
    "callback_present",
    "status",
    "remaining_pages",
    "total_pages",
    "copied_pages",
    "internal_read_bytes",
)
SLOT = (*BASE, "budget_id", "stage_id", "event")
CAPABILITY = (*BASE, "host_file_batches", "budget_slots", "sqlite_backup", "plugin_internal_io")


def _signed(value):
    if type(value) is not int or abs(value) > 2**53:
        raise evidence.EvidenceError("invalid_integer")


def _nullable_number(value):
    if value is not None:
        evidence._number(value, integer=True)


def validate_record(row, *, format_version=None):
    phase = row["phase"]
    fields = {
        "file_batch": FILE,
        "io_charge": CHARGE,
        "sqlite_batch": SQLITE,
        "io_slot": SLOT,
        "io_capability": CAPABILITY,
    }[phase]
    if phase == "io_charge" and "charge_sequence" in row:
        if format_version != 6:
            raise evidence.EvidenceError("charge_sequence_format_invalid")
        fields = (*fields, "charge_sequence")
        evidence._number(row["charge_sequence"], integer=True, positive=True)
    evidence._fields(row, fields)
    for field in ("id", "process_sequence", "at_s"):
        evidence._number(row[field], integer=field != "at_s")
    sequence = row["process_sequence"]
    if sequence >= 256 or not (sequence + 1) << 40 < row["id"] < (sequence + 2) << 40:
        raise evidence.EvidenceError("io_namespace_invalid")
    for field in ("budget_id", "transfer_id"):
        if field in row:
            evidence._number(row[field], integer=True, positive=True)
            if not (sequence + 1) << 40 < row[field] < (sequence + 2) << 40:
                raise evidence.EvidenceError("io_namespace_invalid")
    if phase == "io_capability":
        for field in ("host_file_batches", "budget_slots", "sqlite_backup"):
            if type(row[field]) is not bool:
                raise evidence.EvidenceError("invalid_boolean")
        evidence._enum(row["plugin_internal_io"], {"unobserved"})
        return
    if row["stage_id"] is not None:
        evidence._pattern(row["stage_id"], r"[a-f0-9]{64}")
    if phase in {"io_slot", "io_charge"}:
        evidence._number(row["budget_id"], integer=True, positive=True)
        if row["stage_id"] is None:
            raise evidence.EvidenceError("io_stage_required")
    if phase == "io_slot":
        evidence._enum(row["event"], {"granted", "released"})
        return
    evidence._number(row["transfer_id"], integer=True, positive=True)
    evidence._enum(
        row["event"],
        {"started", "finished", "progress"} if phase == "sqlite_batch" else {"started", "finished"},
    )
    evidence._enum(row["outcome"], {"ok", "failed", "cancelled", "unknown"})
    if phase == "sqlite_batch":
        _signed(row["pages_per_step"])
        evidence._number(row["sleep_seconds"])
        if type(row["callback_present"]) is not bool:
            raise evidence.EvidenceError("invalid_boolean")
        for field in ("status", "remaining_pages", "total_pages", "copied_pages"):
            _nullable_number(row[field])
        if row["internal_read_bytes"] is not None:
            raise evidence.EvidenceError("native_bytes_unknown_required")
        if row["event"] == "progress":
            if any(row[field] is None for field in ("status", "remaining_pages", "total_pages")):
                raise evidence.EvidenceError("io_progress_missing")
            if row["remaining_pages"] > row["total_pages"]:
                raise evidence.EvidenceError("io_progress_invalid")
        elif any(
            row[field] is not None
            for field in ("status", "remaining_pages", "total_pages", "copied_pages")
        ):
            raise evidence.EvidenceError("io_progress_unexpected")
    else:
        evidence._enum(row["direction"], {"read", "write"})
        _nullable_number(row["requested_bytes"])
        field = "charged_bytes" if phase == "io_charge" else "actual_bytes"
        _nullable_number(row[field])
        if phase == "io_charge" and row["requested_bytes"] is None:
            raise evidence.EvidenceError("io_size_required")
        if row["event"] == "started" and row[field] is not None:
            raise evidence.EvidenceError("io_result_before_call")
        if row["event"] == "finished" and row["outcome"] == "ok" and row[field] is None:
            raise evidence.EvidenceError("io_result_missing")
    if (row["event"] == "started") != (row["outcome"] == "unknown"):
        raise evidence.EvidenceError("io_outcome_invalid")


class IoEvents:
    """Namespace checks for all data; completion checks only for claimed pass."""

    def __init__(self, run):
        self.run, self.seen, self.active, self.slots, self.capabilities = run, set(), {}, {}, {}
        self.transfers = set()

    def add(self, row):
        sequence = (row["id"] >> 40) - 1
        if sequence not in self.run["observer_sequences"] or row["id"] in self.seen:
            raise evidence.EvidenceError("io_namespace_invalid")
        self.seen.add(row["id"])
        phase = row["phase"]
        if phase not in evidence.IO_PHASES:
            if phase == "native_validation":
                for name in ("validation_id", "connection_id"):
                    value = row[name]
                    if value and (value >> 40) - 1 != sequence:
                        raise evidence.EvidenceError("io_namespace_invalid")
            return
        if row["process_sequence"] != sequence:
            raise evidence.EvidenceError("io_namespace_invalid")
        if self.run["result"] != "pass":
            return
        if phase == "io_capability":
            if sequence in self.capabilities:
                raise evidence.EvidenceError("io_capability_duplicate")
            self.capabilities[sequence] = row
            return
        if phase == "io_slot":
            key = (sequence, row["budget_id"])
            if row["event"] == "granted":
                if key in self.slots:
                    raise evidence.EvidenceError("io_slot_overlap")
                self.slots[key] = row["stage_id"]
            elif self.slots.pop(key, None) != row["stage_id"]:
                raise evidence.EvidenceError("io_release_unconfirmed")
            return
        key = (sequence, row["transfer_id"])
        if row["event"] == "started":
            if key in self.transfers or len(self.active) >= 8192:
                raise evidence.EvidenceError("io_event_sequence_invalid")
            self.transfers.add(key)
            self.active[key] = row.copy()
            return
        start = self.active.get(key)
        immutable = {
            "phase",
            "stage_id",
            "budget_id",
            "direction",
            "requested_bytes",
            "charge_sequence",
            "pages_per_step",
            "sleep_seconds",
            "callback_present",
        }
        if (
            start is None
            or row["at_s"] < start["at_s"]
            or any(row.get(field) != start.get(field) for field in immutable)
        ):
            raise evidence.EvidenceError("io_event_sequence_invalid")
        start["at_s"] = row["at_s"]
        if phase == "sqlite_batch" and row["event"] == "progress":
            previous = start.get("progress")
            copied = None
            if row["status"] in {0, 101}:
                if previous is None:
                    copied = row["total_pages"] - row["remaining_pages"]
                elif previous[1] == row["total_pages"] and previous[0] >= row["remaining_pages"]:
                    copied = previous[0] - row["remaining_pages"]
                start["progress"] = row["remaining_pages"], row["total_pages"], row["status"]
            if row["copied_pages"] != copied:
                raise evidence.EvidenceError("io_progress_invalid")
        if row["event"] == "finished":
            if phase == "sqlite_batch" and row["outcome"] == "ok":
                progress = start.get("progress")
                if progress is None or progress[0] != 0 or progress[2] != 101:
                    raise evidence.EvidenceError("io_copy_unconfirmed")
            self.active.pop(key)

    def complete(self):
        if self.run["result"] != "pass":
            return
        if self.active or self.slots:
            raise evidence.EvidenceError("io_lifecycle_incomplete")
        if set(self.capabilities) != set(self.run["observer_sequences"]):
            raise evidence.EvidenceError("io_observer_missing")
        if self.run["arm"] == "candidate" and self.run["phase"] in {
            "matrix",
            "mechanism",
            "closeout",
        }:
            if any(
                not all(
                    row[field] for field in ("host_file_batches", "budget_slots", "sqlite_backup")
                )
                for row in self.capabilities.values()
            ):
                raise evidence.EvidenceError("io_observation_unavailable")


def _optional(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


def _stage_hash(value):
    if type(value) is not str or not value or len(value) > 128:
        raise evidence.EvidenceError("io_stage_invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _outcome(error):
    worker = sys.modules.get("clytie.backup.worker")
    cancelled = getattr(worker, "Cancelled", None)
    return (
        "cancelled"
        if (
            isinstance(error, asyncio.CancelledError)
            or (isinstance(cancelled, type) and isinstance(error, cancelled))
            or getattr(error, "code", None) == "cancelled"
        )
        else "failed"
        if error is not None
        else "ok"
    )


class IoObservation:
    def __init__(self, observer, sequence):
        if type(sequence) is not int or not 0 <= sequence < 256:
            raise evidence.EvidenceError("invalid_observer_sequence")
        self.observer = observer
        self.client = self.budget = None
        self.budgets = {}
        self.reported = False

    def identifier(self):
        with self.observer.guard:
            self.observer.sequence += 1
            identifier = self.observer.sequence
        if identifier >= (self.observer.process_sequence + 2) << 40:
            self.observer.recorder.failed = True
            raise evidence.EvidenceError("io_namespace_exhausted")
        return identifier

    def event(self, phase, **fields):
        if self.observer.recorder.failed:
            return
        try:
            identifier = self.identifier()
            self.observer.recorder.append(
                "storage-phases.jsonl",
                dict(
                    id=identifier,
                    process_sequence=self.observer.process_sequence,
                    phase=phase,
                    at_s=self.observer.now(),
                    **fields,
                ),
            )
            return identifier
        except Exception:
            # A failed observer invalidates archival; never interrupt accepted I/O
            # or prevent its real cancellation/release path from completing.
            self.observer.recorder.failed = True

    def stage(self):
        state = self.client._stage.get() if self.client is not None else None
        return _stage_hash(state["id"]) if state is not None else None

    def file_call(self, function, argument, direction):
        count = argument if direction == "read" else memoryview(argument).nbytes
        _signed(count)
        identifier = self.identifier()
        fields = dict(
            transfer_id=identifier,
            stage_id=self.stage(),
            direction=direction,
            requested_bytes=None if count < 0 else count,
        )
        batch = getattr(self, "batch", None)
        if batch is not None:
            batch.safe(lambda: batch.file_link(identifier))
        self.event("file_batch", **fields, event="started", actual_bytes=None, outcome="unknown")
        result, error = None, None
        try:
            result = function(argument)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            actual = len(result) if direction == "read" and type(result) is bytes else result
            self.event(
                "file_batch",
                **fields,
                event="finished",
                actual_bytes=actual if error is None else None,
                outcome=_outcome(error),
            )

    def wrap_file(self, function):
        observer = self

        @functools.wraps(function)
        def measured(stream, argument):
            class Stream:
                def __getattr__(self, name):
                    return getattr(stream, name)

                def read(self, count):
                    return observer.file_call(stream.read, count, "read")

                def write(self, data):
                    return observer.file_call(stream.write, data, "write")

            batch = getattr(observer, "batch", None)
            proxy = Stream()
            if batch is not None:
                return batch.file_operation(function, proxy, argument)
            return function(proxy, argument)

        return measured

    def backup(self, function, target, *, pages, progress, name, sleep):
        _signed(pages)
        evidence._number(sleep)
        if type(name) is not str or (progress is not None and not callable(progress)):
            raise evidence.EvidenceError("io_backup_parameters_invalid")
        fields = dict(
            transfer_id=self.identifier(),
            stage_id=self.stage(),
            pages_per_step=pages,
            sleep_seconds=sleep,
            callback_present=progress is not None,
            internal_read_bytes=None,
        )
        pressure = getattr(self, "pressure", None)
        if pressure is not None:
            pressure.safe(lambda: pressure.bind_backup(fields))
        empty = dict(status=None, remaining_pages=None, total_pages=None, copied_pages=None)
        self.event("sqlite_batch", **fields, **empty, event="started", outcome="unknown")
        previous = None

        def callback(status, remaining, total):
            nonlocal previous
            copied = None
            if status in {0, 101}:
                if previous is None:
                    copied = total - remaining
                elif previous[1] == total and previous[0] >= remaining:
                    copied = previous[0] - remaining
                previous = remaining, total
            progress_id = self.event(
                "sqlite_batch",
                **fields,
                event="progress",
                outcome="ok",
                status=status,
                remaining_pages=remaining,
                total_pages=total,
                copied_pages=copied,
            )
            batch = getattr(self, "batch", None)
            if batch is not None:
                batch.safe(lambda: batch.sqlite_link(progress_id, fields["transfer_id"]))
            if progress is not None:
                return progress(status, remaining, total)

        error = None
        try:
            return function(target, pages=pages, progress=callback, name=name, sleep=sleep)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.event("sqlite_batch", **fields, **empty, event="finished", outcome=_outcome(error))

    def budget_identifier(self, budget):
        # Pressure may be observed before the first grant. Both streams bind to
        # this exact object; no timestamp matching or alternate ID namespace.
        state = self.budgets.get(budget)
        if state is None:
            if budget._active is not None:
                self.observer.recorder.failed = True
            state = {"id": self.identifier(), "active": None}
            self.budgets[budget] = state
        return state["id"]

    def sync_budget(self, budget):
        state = self.budgets.get(budget)
        if state is None:
            # Observation installed after a grant cannot invent its grant time.
            self.observer.recorder.failed = True
            return self.identifier()
        previous, active = state["active"], budget._active
        if previous is not active:
            if previous is not None:
                self.event(
                    "io_slot",
                    budget_id=state["id"],
                    stage_id=_stage_hash(previous.key.stage),
                    event="released",
                )
            if active is not None:
                self.event(
                    "io_slot",
                    budget_id=state["id"],
                    stage_id=_stage_hash(active.key.stage),
                    event="granted",
                )
            state["active"] = active
        return state["id"]

    def install(self, stack):
        # Import only modules provided by the selected source tree. Never fall
        # back to a newer candidate implementation for a historical arm.
        self.client = _optional("clytie.backup.io_client")
        self.budget = _optional("clytie.backup.io_budget")
        if not self.reported:
            self.event(
                "io_capability",
                host_file_batches=self.client is not None,
                budget_slots=self.budget is not None,
                sqlite_backup=True,
                plugin_internal_io="unobserved",
            )
            self.reported = True
        replacements = []
        if self.client is not None:
            for name in ("io_read", "io_write"):
                function = getattr(self.client, name)
                replacements.append((function, self.wrap_file(function)))
        if self.budget is not None:
            cls = self.budget.IoBudget
            original_grant, original_finish, original_charge = cls._grant, cls._finish, cls._charge

            def grant(budget):
                self.budget_identifier(budget)
                result = original_grant(budget)
                self.sync_budget(budget)
                return result

            def finish(budget, stage):
                result = original_finish(budget, stage)
                self.sync_budget(budget)
                return result

            async def charge(budget, stage, direction, size):
                fields = dict(
                    transfer_id=self.identifier(),
                    stage_id=_stage_hash(stage.key.stage),
                    budget_id=self.sync_budget(budget),
                    direction=direction,
                    requested_bytes=size,
                )
                if self.observer.recorder.format_version == 6:
                    fields["charge_sequence"] = stage.sequence
                self.event(
                    "io_charge", **fields, event="started", charged_bytes=None, outcome="unknown"
                )
                error = None
                try:
                    return await original_charge(budget, stage, direction, size)
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    self.event(
                        "io_charge",
                        **fields,
                        event="finished",
                        charged_bytes=size if error is None else None,
                        outcome=_outcome(error),
                    )

            stack.enter_context(patch.object(cls, "_grant", grant))
            stack.enter_context(patch.object(cls, "_finish", finish))
            stack.enter_context(patch.object(cls, "_charge", charge))
        for module_name, module in tuple(sys.modules.items()):
            if module_name.startswith("clytie.") and module is not None:
                for name, value in tuple(vars(module).items()):
                    for original, replacement in replacements:
                        if value is original:
                            stack.enter_context(patch.object(module, name, replacement))

        def restore_late_aliases():
            for module_name, module in tuple(sys.modules.items()):
                if module_name.startswith("clytie.") and module is not None:
                    for name, value in tuple(vars(module).items()):
                        for original, replacement in replacements:
                            if value is replacement:
                                setattr(module, name, original)

        stack.callback(restore_late_aliases)
