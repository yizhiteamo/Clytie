"""Narrow actual host-batch/request bindings; no additional product operations.

The exact caller code/stream identity distinguishes reserve from arbitrary check
callbacks. No frames, paths, SQL, contents or raw stage tokens are retained.
"""

import inspect
import sys
import threading
from contextvars import ContextVar
from types import CodeType
from unittest.mock import patch

try:
    from . import measurement_evidence as evidence
    from .measurement_io_observation import _optional, _outcome, _stage_hash
except ImportError:
    import measurement_evidence as evidence
    from measurement_io_observation import _optional, _outcome, _stage_hash

FIELDS = (
    "id",
    "process_sequence",
    "phase",
    "at_s",
    "event",
    "operation_id",
    "copy_id",
    "transfer_id",
    "progress_id",
    "stage_id",
    "scope_state",
    "cleanup_source",
    "client_charge_id",
    "charge_sequence",
    "direction",
    "requested_bytes",
    "read_charge_id",
    "write_charge_id",
    "reservation_remaining_pages",
    "outcome",
    "source",
)
EVENTS = {
    "file_started",
    "file_finished",
    "request_started",
    "request_finished",
    "file_link",
    "sqlite_link",
}
IDS = (
    "operation_id",
    "copy_id",
    "transfer_id",
    "progress_id",
    "client_charge_id",
    "read_charge_id",
    "write_charge_id",
)


def validate_record(row):
    evidence._fields(row, FIELDS)
    evidence._enum(row["phase"], {"batch_charge_binding"})
    evidence._enum(row["event"], EVENTS)
    evidence._enum(row["source"], {"host_file", "sqlite_reserve", "unclassified"})
    evidence._enum(row["scope_state"], {"active", "disabled", "unbound", "mismatch"})
    evidence._enum(
        row["cleanup_source"],
        {
            "unknown",
            "management_cleanup_borrow",
            "restore_finish_committed",
            "backup_verify_previous",
        },
    )
    evidence._enum(row["outcome"], {"unknown", "ok", "failed", "cancelled"})
    evidence._number(row["process_sequence"], integer=True)
    evidence._number(row["at_s"])
    seq = row["process_sequence"]
    if seq >= 256:
        raise evidence.EvidenceError("batch_namespace_invalid")
    for key in ("id", *IDS):
        value = row[key]
        if value is not None:
            evidence._number(value, integer=True, positive=True)
            if not (seq + 1) << 40 < value < (seq + 2) << 40:
                raise evidence.EvidenceError("batch_namespace_invalid")
    if row["id"] is None:
        raise evidence.EvidenceError("batch_id_required")
    if row["stage_id"] is not None:
        evidence._pattern(row["stage_id"], r"[a-f0-9]{64}")
    for key in ("charge_sequence", "requested_bytes", "reservation_remaining_pages"):
        if row[key] is not None:
            evidence._number(row[key], integer=True, positive=key == "charge_sequence")
    if row["direction"] is not None:
        evidence._enum(row["direction"], {"read", "write"})
    if row["event"].startswith("request_"):
        if any(
            row[key] is None
            for key in (
                "client_charge_id",
                "charge_sequence",
                "stage_id",
                "direction",
                "requested_bytes",
            )
        ):
            raise evidence.EvidenceError("batch_request_missing")
    if row["event"] in {"file_started", "file_finished", "file_link"}:
        if row["operation_id"] is None:
            raise evidence.EvidenceError("batch_operation_missing")
    if row["event"] == "file_link" and row["transfer_id"] is None:
        raise evidence.EvidenceError("batch_transfer_missing")
    if row["event"] == "sqlite_link" and row["progress_id"] is None:
        raise evidence.EvidenceError("batch_progress_missing")
    if row["event"].endswith("started") and row["outcome"] != "unknown":
        raise evidence.EvidenceError("batch_outcome_invalid")


class BatchObservation:
    def __init__(self, io):
        self.io = io
        self.file = ContextVar("measurement_file_operation", default=None)
        self.reserve_code = None

    def safe(self, function):
        try:
            return function()
        except Exception:
            self.io.observer.recorder.failed = True
            return None

    def scope(self):
        module = self.io.client
        client, stage = module.current_client(), module._stage.get()
        state = (
            "disabled"
            if client is None
            else "unbound"
            if stage is None
            else "active"
            if stage["client"] is client and stage["thread"] == threading.get_ident()
            else "mismatch"
        )
        native = sys.modules.get("scripts.measurement_native_observation") or sys.modules.get(
            "measurement_native_observation"
        )
        marker = getattr(native, "_cleanup_source", None)
        return dict(
            stage_id=None if stage is None else _stage_hash(stage["id"]),
            scope_state=state,
            cleanup_source=marker.get() if marker else "unknown",
        )

    def event(self, event, **fields):
        values = dict.fromkeys(FIELDS[4:])
        values.update(self.scope(), event=event, outcome="unknown", source="unclassified")
        values.update(fields)
        return self.io.event("batch_charge_binding", **values)

    def file_operation(self, function, stream, argument):
        state = self.safe(
            lambda: dict(
                id=self.io.identifier(),
                code=inspect.unwrap(function).__code__,
                stream=stream,
                pending=None,
            )
        )
        token = self.file.set(state)
        if state is not None:
            self.safe(
                lambda: self.event("file_started", operation_id=state["id"], source="host_file")
            )
        error = None
        try:
            return function(stream, argument)
        except BaseException as exc:
            error = exc
            raise
        finally:
            if state is not None:
                self.safe(
                    lambda: self.event(
                        "file_finished",
                        operation_id=state["id"],
                        source="host_file",
                        outcome=_outcome(error),
                    )
                )
            self.file.reset(token)

    def file_link(self, transfer_id):
        state = self.file.get()
        if state is None:
            return
        pending, state["pending"] = state["pending"], None
        self.event(
            "file_link",
            operation_id=state["id"],
            transfer_id=transfer_id,
            client_charge_id=pending,
            source="host_file",
            outcome="ok",
        )

    def source(self, client, caller):
        file = self.file.get()
        if (
            file is not None
            and caller.f_code is file["code"]
            and caller.f_locals.get("stream") is file["stream"]
            and caller.f_locals.get("client") is client
        ):
            return dict(source="host_file", operation_id=file["id"]), file
        pressure = getattr(self.io, "pressure", None)
        copy = pressure.copy.get() if pressure is not None else None
        if (
            copy is not None
            and caller.f_code is self.reserve_code
            and caller.f_locals.get("client") is client
        ):
            return dict(
                source="sqlite_reserve",
                copy_id=copy["id"],
                reservation_remaining_pages=caller.f_locals["remaining"],
            ), copy
        return dict(source="unclassified"), None

    def sqlite_link(self, progress_id, transfer_id):
        pressure = getattr(self.io, "pressure", None)
        state = pressure.copy.get() if pressure is not None else None
        pending = state.pop("batch_reservation", None) if state is not None else None
        self.event(
            "sqlite_link",
            copy_id=state["id"] if state else None,
            transfer_id=transfer_id,
            progress_id=progress_id,
            read_charge_id=pending.get("read") if pending else None,
            write_charge_id=pending.get("write") if pending else None,
            reservation_remaining_pages=pending.get("remaining") if pending else None,
            source="sqlite_reserve" if pending else "unclassified",
            outcome="ok",
        )

    def install(self, stack):
        if self.io.client is None:
            return
        sqlite_io = _optional("clytie.backup.sqlite_io")
        if sqlite_io is not None:
            original_copy = inspect.unwrap(sqlite_io.copy_database)
            codes = [
                value
                for value in original_copy.__code__.co_consts
                if isinstance(value, CodeType) and value.co_name == "reserve"
            ]
            self.reserve_code = codes[0] if len(codes) == 1 else None
        cls = self.io.client.IoClient
        original = cls._request

        def request(client, arguments):
            if arguments.get("action") != "charge":
                return original(client, arguments)

            # Exact source callsite, never a timestamp or mere context association.
            def prepare(caller):
                fields, state = self.source(client, caller)
                fields = {
                    **fields,
                    "client_charge_id": self.io.identifier(),
                    "stage_id": _stage_hash(arguments["stage"]),
                    "charge_sequence": arguments["sequence"],
                    "direction": arguments["direction"],
                    "requested_bytes": arguments["size"],
                }
                return fields, state

            # `_getframe(2)` is evaluated here, before safe adds its own frames.
            caller = sys._getframe(2)
            try:
                association = self.safe(lambda caller=caller: prepare(caller))
            finally:
                del caller
            if association is None:
                return original(client, arguments)
            fields, state = association
            self.safe(lambda: self.event("request_started", **fields))
            error = None
            try:
                result = original(client, arguments)
                if state is not None:

                    def bind():
                        if fields["source"] == "host_file":
                            state["pending"] = fields["client_charge_id"]
                        else:
                            remaining = fields["reservation_remaining_pages"]
                            pending = state.setdefault(
                                "batch_reservation", {"remaining": remaining}
                            )
                            if pending["remaining"] != remaining or fields["direction"] in pending:
                                raise ValueError("sqlite_reservation_overlap")
                            pending[fields["direction"]] = fields["client_charge_id"]

                    self.safe(bind)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                self.safe(lambda: self.event("request_finished", **fields, outcome=_outcome(error)))

        stack.enter_context(patch.object(cls, "_request", request))
