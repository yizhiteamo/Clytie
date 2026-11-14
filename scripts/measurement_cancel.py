"""Opt-in v6 planned cancellation of a real operation, never the business schedule.

Only actual observed online-capture calls can trigger. The observer does not wait
for acceptance in a production thread and introduces no checkpoint or transport.
Native BackupWorkers.cancel owns the control write and durable journal update.
"""

import asyncio
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from unittest.mock import patch

try:
    from . import measurement_evidence as evidence
except ImportError:
    import measurement_evidence as evidence

TRIGGERS = {
    "large_database": "first_budgeted_chunk_after_view_acquired",
    "large_asset": "first_asset_copy_chunk",
}


def validate_record(row):
    evidence._fields(
        row,
        (
            "id",
            "process_sequence",
            "phase",
            "at_s",
            "event",
            "trigger",
            "source_row_id",
            "source_at_s",
            "stage_id",
            "view_id",
            "cancel_requested",
            "terminal_phase",
        ),
    )
    evidence._enum(row["phase"], {"planned_cancel"})
    evidence._enum(
        row["event"], {"triggered", "requested", "accepted", "rejected", "failed", "terminal"}
    )
    evidence._enum(row["trigger"], set(TRIGGERS.values()))
    evidence._number(row["id"], integer=True, positive=True)
    if type(row["process_sequence"]) is not int or row["process_sequence"] != 0:
        raise evidence.EvidenceError("cancel_observer_invalid")
    if not 1 << 40 < row["id"] < 2 << 40:
        raise evidence.EvidenceError("cancel_namespace_invalid")
    evidence._number(row["at_s"])
    for field in ("source_row_id", "view_id"):
        if row[field] is not None:
            evidence._number(row[field], integer=True, positive=True)
    if row["source_row_id"] is not None:
        if not 1 << 40 < row["source_row_id"] < 2 << 40:
            raise evidence.EvidenceError("cancel_namespace_invalid")
        evidence._number(row["source_at_s"])
        evidence._pattern(row["stage_id"], r"[a-f0-9]{64}")
        if row["source_at_s"] > row["at_s"]:
            raise evidence.EvidenceError("cancel_time_invalid")
        if (row["trigger"] == TRIGGERS["large_database"]) != (row["view_id"] is not None):
            raise evidence.EvidenceError("cancel_view_invalid")
    elif row["event"] != "terminal" or any(
        row[field] is not None for field in ("source_at_s", "stage_id", "view_id")
    ):
        raise evidence.EvidenceError("cancel_trigger_missing")
    if row["cancel_requested"] is not None and type(row["cancel_requested"]) is not bool:
        raise evidence.EvidenceError("cancel_acceptance_invalid")
    if row["event"] == "accepted" and row["cancel_requested"] is not True:
        raise evidence.EvidenceError("cancel_acceptance_invalid")
    if row["event"] == "rejected" and row["cancel_requested"] is not False:
        raise evidence.EvidenceError("cancel_acceptance_invalid")
    if row["event"] in {"triggered", "requested", "failed"} and row["cancel_requested"] is not None:
        raise evidence.EvidenceError("cancel_acceptance_invalid")
    if row["event"] == "terminal":
        evidence._enum(row["terminal_phase"], evidence.BACKUP_PHASES)
    elif row["terminal_phase"] is not None:
        raise evidence.EvidenceError("cancel_terminal_unexpected")


class PlannedCancel:
    def __init__(self, observation, workers, operation, *, scale):
        if observation.recorder.format_version != 6 or observation.process_sequence != 0:
            raise ValueError("measurement_cancel_format_invalid")
        if scale not in TRIGGERS:
            raise ValueError("measurement_cancel_scale_invalid")
        self.observation, self.workers, self.operation = observation, workers, operation
        self.trigger = TRIGGERS[scale]
        self.loop = asyncio.get_running_loop()
        self.started = self.loop.create_future()
        self.guard = threading.Lock()
        self.asset = ContextVar("measurement_actual_asset_copy", default=False)
        self.source = None
        self.accepted = False

    def record(self, event, *, cancel_requested=None, terminal_phase=None):
        fields = self.source or dict(
            source_row_id=None, source_at_s=None, stage_id=None, view_id=None
        )
        self.observation.io.event(
            "planned_cancel",
            **fields,
            event=event,
            trigger=self.trigger,
            cancel_requested=cancel_requested,
            terminal_phase=terminal_phase,
        )

    def observe(self, row):
        if self.source is not None or row.get("stage_id") is None:
            return
        view_id = None
        if self.trigger == TRIGGERS["large_database"]:
            if not (
                row.get("phase") == "sqlite_batch"
                and row.get("event") == "progress"
                and row.get("outcome") == "ok"
                and (row.get("copied_pages") or 0) > 0
            ):
                return
            pressure = self.observation.pressure
            snapshot, copy = pressure.snapshot.get(), pressure.copy.get()
            if (
                snapshot is None
                or copy is None
                or snapshot._view_limits is None
                or copy.get("transfer_id") != row["transfer_id"]
                or copy["source"] not in snapshot._connections.values()
                or not copy["source"].in_transaction
                or snapshot.io_client is None
            ):
                return
            view_id = getattr(snapshot, "_measurement_view_id", None)
            if view_id is None:
                return
        elif not (
            self.asset.get()
            and row.get("phase") == "file_batch"
            and row.get("event") == "finished"
            and row.get("direction") == "write"
            and row.get("outcome") == "ok"
            and (row.get("actual_bytes") or 0) > 0
        ):
            return
        with self.guard:
            if self.source is not None:
                return
            self.source = dict(
                source_row_id=row["id"],
                source_at_s=row["at_s"],
                stage_id=row["stage_id"],
                view_id=view_id,
            )
        self.record("triggered")
        self.loop.call_soon_threadsafe(self.dispatch)

    def dispatch(self):
        if not self.started.done():
            self.started.set_result(self.loop.create_task(self.request()))

    async def request(self):
        self.record("requested")
        try:
            result = await self.workers.cancel(self.operation)
        except Exception:
            self.record("failed")
            return
        self.accepted = result.get("cancel_requested") is True
        self.record("accepted" if self.accepted else "rejected", cancel_requested=self.accepted)

    async def settle(self):
        if self.source is not None:
            task = await asyncio.shield(self.started)
            await asyncio.shield(task)

    def terminal(self, result):
        self.record(
            "terminal",
            cancel_requested=result.get("cancel_requested"),
            terminal_phase=result["phase"],
        )
        return self.source is not None and self.accepted and result["phase"] == "cancelled"

    @contextmanager
    def install(self):
        from clytie.assets import AssetStore

        recorder = self.observation.recorder
        original_append, original_copy = recorder.append, AssetStore.copy_resolved

        def append(name, row):
            original_append(name, row)
            if name == "storage-phases.jsonl" and row.get("phase") != "planned_cancel":
                try:
                    self.observe(row)
                except Exception:
                    # An observation failure cannot prevent native cleanup.
                    recorder.failed = True

        def copy(store, *args, **kwargs):
            token = self.asset.set(True)
            try:
                return original_copy(store, *args, **kwargs)
            finally:
                self.asset.reset(token)

        with (
            patch.object(recorder, "append", append),
            patch.object(AssetStore, "copy_resolved", copy),
        ):
            yield self
