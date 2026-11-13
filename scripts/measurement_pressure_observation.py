"""Format 6 raw metadata observation; never a mechanism pass verdict.

WAL IDs join actual ViewLimits objects to observed snapshot views and logical
databases. Epoch microseconds preserve timestamp meaning, not original
ISO field bytes. No new SQL, file reads, pressure polls or scheduler/status calls. Values come
from calls already made by production. Missing/native byte counts stay unknown.
"""

import functools
import hashlib
import importlib.util
import json
import math
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from unittest.mock import patch
from weakref import WeakKeyDictionary

if __package__:
    from . import measurement_evidence as evidence
    from .measurement_io_observation import _optional as _optional_module
    from .measurement_io_observation import _outcome
else:
    import measurement_evidence as evidence
    from measurement_io_observation import _optional as _optional_module
    from measurement_io_observation import _outcome


def _optional(name):
    # The approved legacy tree predates the storage package. Check only this
    # known optional parent before attempting its child. find_spec errors and
    # dependency failures from an existing package/module must propagate.
    if name == "clytie.storage.owner" and importlib.util.find_spec("clytie.storage") is None:
        return None
    return _optional_module(name)


PHASES = {
    "owner_pressure",
    "control_pressure",
    "budget_pressure",
    "budget_mode",
    "wal_growth",
    "wal_binding",
    "sqlite_geometry",
}
BASE = ("id", "process_sequence", "phase", "at_s")
RAW = (
    "source",
    "observed_at_unix_us",
    "ready_wait_seconds",
    "execution_seconds",
    "finalizer_wait_seconds",
    "finalizer_capacity_blocked",
    "ordinary",
    "finalizers",
)
FIELDS = {
    "wal_binding": (
        *BASE,
        "wal_id",
        "snapshot_id",
        "limits_id",
        "view_id",
        "database_kind",
        "database_hash",
        "outcome",
    ),
    "owner_pressure": (*BASE, "owner_id", "started_s", "sample", "sample_hash", "outcome"),
    "control_pressure": (
        *BASE,
        "pressure_budget_id",
        "owner_hash",
        "receipt_id",
        "event",
        "sample",
        "sample_hash",
        "outcome",
    ),
    "budget_pressure": (
        *BASE,
        "pressure_budget_id",
        "owner_hash",
        "receipt_id",
        "waiting_s",
        "executing_s",
        "finalizer_wait",
        "received_monotonic_s",
        "accepted",
        "normal",
        "low_since_monotonic_s",
        "outcome",
    ),
    "budget_mode": (
        *BASE,
        "pressure_budget_id",
        "updated_monotonic_s",
        "normal",
        "low_since_monotonic_s",
        "owners_count",
        "signals_count",
    ),
    "wal_growth": (
        *BASE,
        "wal_id",
        "event",
        "started_s",
        "before",
        "after",
        "returned_additional_bytes",
        "outcome",
    ),
    "sqlite_geometry": (
        *BASE,
        "copy_id",
        "wal_id",
        "transfer_id",
        "stage_id",
        "event",
        "page_size",
        "page_count",
        "internal_read_bytes",
        "outcome",
    ),
}
WAL = ("generation_hash", "physical_bytes", "page_size", "additional_bytes")
_receipt = ContextVar("measurement_pressure_receipt", default=None)


def _number(value, *, integer=False, signed=False):
    if value is None:
        return
    if (
        type(value) not in ((int,) if integer else (int, float))
        or not math.isfinite(value)
        or abs(value) > 2**53
        or (not signed and value < 0)
    ):
        raise evidence.EvidenceError("invalid_number")


def _boolean(value):
    if value is not None and type(value) is not bool:
        raise evidence.EvidenceError("invalid_boolean")


def _hash(value):
    if value is not None:
        evidence._pattern(value, r"[a-f0-9]{64}")


def validate_record(row):
    evidence._fields(row, FIELDS[row["phase"]])
    evidence._number(row["id"], integer=True, positive=True)
    primary = {
        "owner_pressure": "owner_id",
        "control_pressure": "pressure_budget_id",
        "budget_pressure": "pressure_budget_id",
        "budget_mode": "pressure_budget_id",
        "wal_growth": "wal_id",
        "wal_binding": "wal_id",
        "sqlite_geometry": "copy_id",
    }[row["phase"]]
    evidence._number(row[primary], integer=True, positive=True)
    sequence = row["process_sequence"]
    evidence._number(sequence, integer=True)
    if sequence >= 256:
        raise evidence.EvidenceError("io_namespace_invalid")
    for field in (
        "id",
        "owner_id",
        "pressure_budget_id",
        "receipt_id",
        "wal_id",
        "copy_id",
        "snapshot_id",
        "limits_id",
        "transfer_id",
    ):
        if field in row and row[field] is not None:
            value = row[field]
            evidence._number(value, integer=True, positive=True)
            if not (sequence + 1) << 40 < value < (sequence + 2) << 40:
                raise evidence.EvidenceError("io_namespace_invalid")
    evidence._number(row["at_s"])
    if "outcome" in row:
        evidence._enum(row["outcome"], {"ok", "failed", "cancelled", "unknown"})
    for field in ("owner_hash", "stage_id", "sample_hash", "database_hash"):
        if field in row:
            _hash(row[field])
    if "started_s" in row:
        evidence._number(row["started_s"])
        if row["started_s"] > row["at_s"]:
            raise evidence.EvidenceError("observation_time_invalid")
    phase = row["phase"]
    if phase == "wal_binding":
        if row["view_id"] is not None:
            evidence._number(row["view_id"], integer=True, positive=True)
        evidence._enum(row["database_kind"], {"memory", "ledger", "other", "unknown"})
    elif phase in {"owner_pressure", "control_pressure"}:
        sample = row["sample"]
        evidence._fields(sample, RAW)
        evidence._enum(sample["source"], {"owner_control_cache", "unknown"})
        for field in RAW[1:]:
            if field == "finalizer_capacity_blocked":
                _boolean(sample[field])
            else:
                _number(
                    sample[field],
                    integer=field in {"ordinary", "finalizers", "observed_at_unix_us"},
                )
        if row["sample_hash"] != sample_hash(sample):
            raise evidence.EvidenceError("pressure_hash_mismatch")
        if phase == "control_pressure":
            evidence._enum(row["event"], {"received", "returned"})
            if (row["event"] == "received") != (row["outcome"] == "unknown"):
                raise evidence.EvidenceError("io_outcome_invalid")
    elif phase == "budget_pressure":
        for field in ("waiting_s", "executing_s"):
            _number(row[field], signed=True)
        for field in ("finalizer_wait", "accepted", "normal"):
            _boolean(row[field])
        for field in ("received_monotonic_s", "low_since_monotonic_s"):
            _number(row[field])
        if row["accepted"] is True and row["received_monotonic_s"] is None:
            raise evidence.EvidenceError("pressure_receipt_missing")
    elif phase == "budget_mode":
        _boolean(row["normal"])
        for field in (
            "updated_monotonic_s",
            "low_since_monotonic_s",
            "owners_count",
            "signals_count",
        ):
            _number(row[field], integer=field.endswith("count"))
    elif phase == "wal_growth":
        evidence._enum(row["event"], {"initialized", "sampled"})
        _number(row["returned_additional_bytes"], integer=True)
        for field in ("before", "after"):
            state = row[field]
            if state is None:
                continue
            evidence._fields(state, WAL)
            _hash(state["generation_hash"])
            for name in WAL[1:]:
                _number(state[name], integer=True)
        if row["outcome"] != "ok" and row["returned_additional_bytes"] is not None:
            raise evidence.EvidenceError("wal_result_unconfirmed")
    elif phase == "sqlite_geometry":
        evidence._enum(row["event"], {"page_size", "page_count", "backup_started", "finished"})
        _number(row["page_size"], integer=True)
        _number(row["page_count"], integer=True)
        if row["internal_read_bytes"] is not None:
            raise evidence.EvidenceError("native_bytes_unknown_required")


def raw_sample(value):
    result = dict.fromkeys(RAW)
    result["source"] = "unknown"
    if type(value) is not dict:
        return result
    evidence._fields(
        value, tuple("observed_at" if key == "observed_at_unix_us" else key for key in RAW)
    )
    if value.get("source") == "owner_control_cache":
        result["source"] = "owner_control_cache"
    stamp = value.get("observed_at")
    if type(stamp) is str:
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is not None:
                delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
                result["observed_at_unix_us"] = (
                    delta.days * (24 * 60 * 60) + delta.seconds
                ) * 1_000_000 + delta.microseconds
        except ValueError:
            pass
    for field in RAW[2:]:
        candidate = value.get(field)
        try:
            if field == "finalizer_capacity_blocked":
                _boolean(candidate)
            else:
                _number(
                    candidate, integer=field in {"ordinary", "finalizers", "observed_at_unix_us"}
                )
            result[field] = candidate
        except evidence.EvidenceError:
            pass
    return result


def sample_hash(sample):
    if any(value is None for value in sample.values()) or sample["source"] != "owner_control_cache":
        return None
    return hashlib.sha256(
        json.dumps(sample, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def wal_state(growth):
    last = getattr(growth, "last", None)
    result = dict(
        generation_hash=None,
        physical_bytes=None,
        page_size=None,
        additional_bytes=getattr(growth, "additional", None),
    )
    if last is not None:
        (device, inode, header), size = last
        encoded = json.dumps([device, inode, header.hex()], separators=(",", ":"))
        result.update(
            generation_hash=hashlib.sha256(encoded.encode()).hexdigest(), physical_bytes=size
        )
        if len(header) >= 12:
            result["page_size"] = int.from_bytes(header[8:12], "big")
    return result


class WalBindings:
    """Association checks, separate from production mechanism correctness."""

    def __init__(self):
        self.wals, self.bindings, self.views, self.copies = set(), {}, set(), set()

    def add(self, name, row):
        if name == "release-events.jsonl":
            if row["resource"] == "view" and row["event"] in {"acquired", "unknown"}:
                self.views.add(row["id"])
            return
        if name != "storage-phases.jsonl":
            return
        if row["phase"] == "wal_growth":
            self.wals.add(row["wal_id"])
        elif row["phase"] == "wal_binding":
            if row["wal_id"] in self.bindings:
                raise evidence.EvidenceError("wal_binding_duplicate")
            self.bindings[row["wal_id"]] = row
        elif row["phase"] == "sqlite_geometry" and row["wal_id"] is not None:
            self.copies.add(row["wal_id"])

    def complete(self):
        if self.wals != set(self.bindings) or not self.copies <= self.wals:
            raise evidence.EvidenceError("wal_binding_missing")
        snapshots, limits, databases = {}, {}, set()
        for row in self.bindings.values():
            if (
                row["view_id"] not in self.views
                or row["snapshot_id"] is None
                or row["limits_id"] is None
                or row["database_hash"] is None
                or row["database_kind"] == "unknown"
            ):
                raise evidence.EvidenceError("wal_view_unconfirmed")
            snapshot, limit = row["snapshot_id"], row["limits_id"]
            if (
                snapshots.setdefault(snapshot, row["view_id"]) != row["view_id"]
                or limits.setdefault(limit, snapshot) != snapshot
                or (limit, row["database_hash"]) in databases
            ):
                raise evidence.EvidenceError("wal_binding_inconsistent")
            databases.add((limit, row["database_hash"]))


class PressureObservation:
    def __init__(self, io):
        self.io = io
        self.objects = WeakKeyDictionary()
        self.modes = WeakKeyDictionary()
        self.guard = threading.RLock()
        self.copy = ContextVar("measurement_sqlite_geometry", default=None)
        self.snapshot = ContextVar("measurement_snapshot", default=None)
        self.limits = ContextVar("measurement_view_limits", default=None)

    def safe(self, function):
        try:
            return function()
        except Exception:
            self.io.observer.recorder.failed = True
            return None

    def identifier(self, instance):
        with self.guard:
            if instance not in self.objects:
                self.objects[instance] = self.io.identifier()
            return self.objects[instance]

    def event(self, phase, **fields):
        self.io.event(phase, **fields)

    def bind_wal(self, growth, path, outcome):
        worker, limits = self.snapshot.get(), self.limits.get()
        names = (
            [name for name, value in worker.paths.items() if str(value) + "-wal" == str(path)]
            if worker is not None
            else []
        )
        name = names[0] if len(names) == 1 else None
        self.event(
            "wal_binding",
            wal_id=self.identifier(growth),
            snapshot_id=self.identifier(worker) if worker is not None else None,
            limits_id=self.identifier(limits) if limits is not None else None,
            view_id=getattr(worker, "_measurement_view_id", None),
            database_kind=name if name in {"memory", "ledger"} else "other" if name else "unknown",
            database_hash=hashlib.sha256(name.encode()).hexdigest() if name else None,
            outcome=outcome,
        )

    def copy_wal(self, source):
        worker = self.snapshot.get()
        if worker is None or worker._view_limits is None:
            return None
        names = [name for name, connection in worker._connections.items() if connection is source]
        growth = worker._view_limits.wals.get(names[0]) if len(names) == 1 else None
        return self.identifier(growth) if growth is not None else None

    def mode(self, budget):
        owners = None if budget._owners is None else frozenset(budget._owners)
        state = (budget._normal, budget._low_since, owners, frozenset(budget._signals))
        if self.modes.get(budget) == state:
            return
        self.modes[budget] = state
        self.event(
            "budget_mode",
            pressure_budget_id=self.identifier(budget),
            updated_monotonic_s=budget._updated,
            normal=budget._normal,
            low_since_monotonic_s=budget._low_since,
            owners_count=None if budget._owners is None else len(budget._owners),
            signals_count=len(budget._signals),
        )

    def cursor(self, connection, sql, cursor):
        state = self.copy.get()
        if state is None or state["source"] is not connection or type(sql) is not str:
            return cursor
        words = sql.strip().rstrip(";").lower().split()
        field = (
            "page_size"
            if words == ["pragma", "page_size"]
            else ("page_count" if words == ["pragma", "page_count"] else None)
        )
        if field is None:
            return cursor
        observation = self

        class Cursor:
            def __getattr__(self, name):
                return getattr(cursor, name)

            def fetchone(self):
                value = cursor.fetchone()

                def capture():
                    # Only inspect the returned row. Never fetch on behalf of production.
                    number = value[0] if value is not None else None
                    _number(number, integer=True)
                    state[field] = number
                    state["stage"] = observation.io.stage()
                    observation.geometry(state, field, "ok")

                observation.safe(capture)
                return value

        return Cursor()

    def bind_backup(self, fields):
        state = self.copy.get()
        if state is not None:
            state["transfer_id"] = fields["transfer_id"]
            state["stage"] = fields["stage_id"]
            self.geometry(state, "backup_started", "unknown")

    def geometry(self, state, event, outcome):
        self.event(
            "sqlite_geometry",
            copy_id=state["id"],
            wal_id=state.get("wal_id"),
            transfer_id=state.get("transfer_id"),
            stage_id=state["stage"],
            event=event,
            page_size=state["page_size"],
            page_count=state["page_count"],
            internal_read_bytes=None,
            outcome=outcome,
        )

    def install(self, stack):
        owner_module = _optional("clytie.storage.owner")
        control_module = _optional("clytie.backup.io_control")
        limits_module = _optional("clytie.backup.view_limits")
        sqlite_module = _optional("clytie.backup.sqlite_io")
        # Historical arms have neither ViewLimits nor the budgeted SQLite
        # helper. Their capture module may predate _SnapshotWorker entirely.
        # Only require this exact binding interface where its feature exists;
        # missing interfaces in an existing implementation remain errors.
        capture_module = _optional("clytie.backup.capture") if limits_module is not None else None
        if capture_module is not None:
            cls = capture_module._SnapshotWorker
            methods = ("_capture", "_copy_checked") if sqlite_module is not None else ("_capture",)
            for method in methods:
                original = getattr(cls, method)

                def scope(worker, *args, _original=original, **kwargs):
                    token = self.snapshot.set(worker)
                    try:
                        return _original(worker, *args, **kwargs)
                    finally:
                        self.snapshot.reset(token)

                stack.enter_context(patch.object(cls, method, scope))
        if owner_module is not None and hasattr(owner_module.StorageOwner, "sample_io_pressure"):
            original_sample = owner_module.StorageOwner.sample_io_pressure

            def sample(owner):
                started = self.io.observer.now()
                error, result = None, None
                try:
                    result = original_sample(owner)
                    return result
                except BaseException as exc:
                    error = exc
                    raise
                finally:

                    def record():
                        value = raw_sample(result)
                        self.event(
                            "owner_pressure",
                            owner_id=self.identifier(owner),
                            started_s=started,
                            sample=value,
                            sample_hash=sample_hash(value),
                            outcome=_outcome(error),
                        )

                    self.safe(record)

            stack.enter_context(
                patch.object(owner_module.StorageOwner, "sample_io_pressure", sample)
            )

        if control_module is not None:
            original_control = control_module.IoControl.pressure

            def control(control, session, sample):
                receipt = self.safe(lambda: self.io.identifier())
                token = _receipt.set(receipt)
                error = None

                def record(event, outcome):
                    value = raw_sample(sample)
                    self.event(
                        "control_pressure",
                        pressure_budget_id=self.identifier(control.budget),
                        owner_hash=hashlib.sha256(session.encode()).hexdigest(),
                        receipt_id=receipt,
                        event=event,
                        sample=value,
                        sample_hash=sample_hash(value),
                        outcome=outcome,
                    )

                self.safe(lambda: record("received", "unknown"))
                try:
                    return original_control(control, session, sample)
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    self.safe(lambda: record("returned", _outcome(error)))
                    _receipt.reset(token)

            stack.enter_context(patch.object(control_module.IoControl, "pressure", control))

        if self.io.budget is not None:
            cls = self.io.budget.IoBudget
            original_pressure, original_refill = cls.pressure, cls._refill

            def pressure(budget, owner, *, waiting, executing, finalizer_wait):
                error = None
                try:
                    return original_pressure(
                        budget,
                        owner,
                        waiting=waiting,
                        executing=executing,
                        finalizer_wait=finalizer_wait,
                    )
                except BaseException as exc:
                    error = exc
                    raise
                finally:

                    def record():
                        signal = budget._signals.get(owner)
                        self.event(
                            "budget_pressure",
                            pressure_budget_id=self.identifier(budget),
                            owner_hash=hashlib.sha256(owner.encode()).hexdigest(),
                            receipt_id=_receipt.get(),
                            waiting_s=waiting,
                            executing_s=executing,
                            finalizer_wait=finalizer_wait,
                            received_monotonic_s=signal[0] if error is None and signal else None,
                            accepted=error is None,
                            normal=budget._normal,
                            low_since_monotonic_s=budget._low_since,
                            outcome=_outcome(error),
                        )
                        self.mode(budget)

                    self.safe(record)

            def refill(budget):
                result = original_refill(budget)
                self.safe(lambda: self.mode(budget))
                return result

            stack.enter_context(patch.object(cls, "pressure", pressure))
            stack.enter_context(patch.object(cls, "_refill", refill))

        if limits_module is not None:
            limits_cls = getattr(limits_module, "ViewLimits", None)
            if limits_cls is not None:
                original_limits_init = limits_cls.__init__

                def limits_init(limits, *args, **kwargs):
                    token = self.limits.set(limits)
                    try:
                        return original_limits_init(limits, *args, **kwargs)
                    finally:
                        self.limits.reset(token)

                stack.enter_context(patch.object(limits_cls, "__init__", limits_init))
            cls = limits_module.WalGrowth
            original_init, original_wal = cls.__init__, cls.sample

            def invoke(growth, function, event, *args):
                started = self.io.observer.now()
                before = self.safe(lambda: wal_state(growth)) if event == "sampled" else None
                result, error = None, None
                try:
                    result = function(growth, *args)
                    return result
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    self.safe(
                        lambda: self.event(
                            "wal_growth",
                            wal_id=self.identifier(growth),
                            event=event,
                            started_s=started,
                            before=before,
                            after=wal_state(growth),
                            returned_additional_bytes=result
                            if event == "sampled" and error is None
                            else None,
                            outcome=_outcome(error),
                        )
                    )

            def initialize(growth, path):
                error = None
                try:
                    return invoke(growth, original_init, "initialized", path)
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    self.safe(lambda: self.bind_wal(growth, path, _outcome(error)))

            def wal(growth):
                return invoke(growth, original_wal, "sampled")

            stack.enter_context(patch.object(cls, "__init__", initialize))
            stack.enter_context(patch.object(cls, "sample", wal))

        if sqlite_module is not None:
            # Preserve the production heavy_io decorator. Replace aliases of the
            # decorated function with a delegating scope instead. All arguments are unchanged.
            original_copy = sqlite_module.copy_database

            @functools.wraps(original_copy)
            def copy(source, target, *, pages, check):
                state = self.safe(
                    lambda: dict(
                        id=self.io.identifier(),
                        source=source,
                        stage=self.io.stage(),
                        page_size=None,
                        page_count=None,
                        wal_id=self.copy_wal(source),
                    )
                )
                token = self.copy.set(state)
                error = None
                try:
                    return original_copy(source, target, pages=pages, check=check)
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    if state is not None:
                        self.safe(lambda: self.geometry(state, "finished", _outcome(error)))
                    self.copy.reset(token)

            import sys

            for name, module in tuple(sys.modules.items()):
                if name.startswith("clytie.") and module is not None:
                    for field, value in tuple(vars(module).items()):
                        if value is original_copy:
                            stack.enter_context(patch.object(module, field, copy))

            def restore_late_aliases():
                for name, module in tuple(sys.modules.items()):
                    if name.startswith("clytie.") and module is not None:
                        for field, value in tuple(vars(module).items()):
                            if value is copy:
                                setattr(module, field, original_copy)

            stack.callback(restore_late_aliases)
