import asyncio
import hashlib
import sqlite3
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from tests.measurement_tools import load

from clytie.backup import io_client, native_sqlite
from clytie.backup.operation_slot import OperationSlot

draft = load("measurement_native_observation")
evidence = load("measurement_evidence")
ROOT = Path(__file__).resolve().parents[1]


def verify_native_stage_intervals(rows):
    slots, native = {}, {}
    for row in rows:
        if row.get("phase") == "io_slot":
            key = row["stage_id"]
            events = slots.setdefault(key, {})
            event = row["event"]
            if event in events:
                raise ValueError("stage_binding_ambiguous")
            events[event] = row["at_s"]
        elif row.get("phase") == "native_validation":
            native.setdefault(row["validation_id"], []).append(row)
    for events in native.values():
        events.sort(key=lambda row: (row["at_s"], row["id"]))
        first = next(row for row in events if row["event"] == "started")
        context = tuple(
            first.get(key)
            for key in ("stage_id", "io_scope_state", "stage_thread_matches", "cleanup_source")
        )
        if any(
            tuple(
                row.get(key)
                for key in ("stage_id", "io_scope_state", "stage_thread_matches", "cleanup_source")
            )
            != context
            for row in events
        ):
            raise ValueError("native_context_changed")
        stage = slots.get(first.get("stage_id"))
        if stage is None or set(stage) != {"granted", "released"}:
            raise ValueError("native_stage_unconfirmed")
        if first.get("stage_thread_matches") is not True:
            raise ValueError("native_stage_thread_unconfirmed")
        if first.get("io_scope_state") not in {"active", "disabled"}:
            raise ValueError("native_scope_unconfirmed")
        # "disabled" and cleanup_source are observations, NOT an exemption judgment.
        if first["at_s"] < stage["granted"] or stage["released"] < stage["granted"]:
            raise ValueError("native_stage_unconfirmed")
        terminal = [
            row for row in events if row["event"] in {"driver_finished", "connection_closed"}
        ]
        if not terminal or max(row["at_s"] for row in terminal) > stage["released"]:
            raise ValueError("native_resources_after_release")
        # finished is emitted after validation_stage.__exit__, so may follow release.
        # NativeEvents independently verifies all attachments closed and driver ended.
    return len(native)


class Recorder:
    failed = False

    def __init__(self, version=6):
        self.format_version, self.rows = version, []

    def append(self, name, row):
        self.rows.append(row)


class Tiny(unittest.TestCase):
    def test_schema_preserves_old_v6_and_requires_complete_new_context(self):
        rows = self.run_check()
        native = [row for row in rows if row.get("phase") == "native_validation"]
        for row in native:
            evidence.validate_record("storage-phases.jsonl", row, format_version=6)
            old = {
                key: value
                for key, value in row.items()
                if key not in evidence.NATIVE_CONTEXT_FIELDS
            }
            evidence.validate_record("storage-phases.jsonl", old, format_version=6)
            evidence.validate_record("storage-phases.jsonl", old, format_version=5)
            with self.assertRaises(ValueError):
                evidence.validate_record("storage-phases.jsonl", row, format_version=5)
            for field in evidence.NATIVE_CONTEXT_FIELDS:
                incomplete = dict(row)
                incomplete.pop(field)
                with self.assertRaises(ValueError):
                    evidence.validate_record("storage-phases.jsonl", incomplete, format_version=6)
        unbound = [
            {key: value for key, value in row.items() if key not in evidence.NATIVE_CONTEXT_FIELDS}
            if row["phase"] == "native_validation"
            else dict(row)
            for row in rows
        ]
        with self.assertRaisesRegex(ValueError, "native_stage_unconfirmed"):
            verify_native_stage_intervals(unbound)

    def setup_observation(self, version=6):
        recorder = Recorder(version)
        timer = iter(range(1, 10 * 1000))
        observer = SimpleNamespace(
            recorder=recorder,
            guard=threading.Lock(),
            sequence=1 << 40,
            io=SimpleNamespace(client=io_client),
            now=lambda: next(timer),
        )
        native = draft.NativeObservation(observer)

        def request(arguments, **kwargs):
            if arguments["action"] in {"acquire", "release"}:
                recorder.append(
                    "storage-phases.jsonl",
                    dict(
                        phase="io_slot",
                        stage_id=hashlib.sha256(arguments["stage"].encode()).hexdigest(),
                        event="granted" if arguments["action"] == "acquire" else "released",
                        at_s=observer.now(),
                    ),
                )
            return {}

        client = io_client.IoClient(request)

        class Connection(sqlite3.Connection):
            def close(self):
                result = super().close()
                native.closed(self)
                return result

        return observer, native, client, Connection

    def run_check(self, mode="ok", version=6, nested=False):
        observer, native, client, connection = self.setup_observation(version)
        with ExitStack() as stack:
            native.install(stack, lambda: 0)
            with io_client.io_scope(client):
                try:
                    with native_sqlite.validation_stage():
                        db = sqlite3.connect(":memory:", factory=connection)
                        try:
                            db.execute("CREATE TABLE fictional(value INTEGER)")
                            db.execute("INSERT INTO fictional VALUES (1)")
                            count = [0]

                            def cancel():
                                count[0] += 1
                                if mode == "cancel" and count[0] == 2:
                                    raise asyncio.CancelledError()

                            with io_client.io_checks(cancel):
                                with native_sqlite.native_checks(db):
                                    if nested:
                                        with native_sqlite.validation_stage():
                                            self.assertEqual(
                                                db.execute("PRAGMA integrity_check").fetchone(),
                                                ("ok",),
                                            )
                                    elif mode == "error":
                                        db.execute("SELECT * FROM fictional_missing")
                                    else:
                                        self.assertEqual(
                                            db.execute("PRAGMA integrity_check").fetchone(), ("ok",)
                                        )
                        finally:
                            db.close()
                except (asyncio.CancelledError, sqlite3.OperationalError):
                    if mode == "ok":
                        raise
            native.check_unsettled()
        return observer.recorder.rows

    def test_real_sqlite_normal_nested_and_release_order(self):
        rows = self.run_check(nested=True)
        self.assertEqual(sum(r.get("event") == "granted" for r in rows), 1)
        self.assertEqual(verify_native_stage_intervals(rows), 1)
        finished = next(r["at_s"] for r in rows if r.get("event") == "finished")
        released = next(r["at_s"] for r in rows if r.get("event") == "released")
        self.assertGreater(finished, released)

    def test_real_sqlite_cancel_and_exception_closed_before_release(self):
        for mode in ("cancel", "error"):
            rows = self.run_check(mode)
            self.assertEqual(verify_native_stage_intervals(rows), 1)
            self.assertTrue(
                any(
                    r.get("outcome") == ("cancelled" if mode == "cancel" else "failed")
                    for r in rows
                )
            )

    def test_v4_v5_shape_preserved(self):
        for version in (4, 5):
            rows = self.run_check(version=version)
            self.assertTrue(
                all(
                    "stage_id" not in r and "cleanup_source" not in r
                    for r in rows
                    if r.get("phase") == "native_validation"
                )
            )

    def test_unbound_and_disabled_not_guessed_cleanup(self):
        observer, native, client, _ = self.setup_observation()
        self.assertEqual(native.scope_snapshot()["io_scope_state"], "unbound")
        self.assertEqual(native.scope_snapshot()["cleanup_source"], "unknown")
        with io_client.io_scope(client), client.stage(), io_client.io_scope(None):
            row = native.scope_snapshot()
            self.assertEqual(row["io_scope_state"], "disabled")
            self.assertEqual(row["cleanup_source"], "unknown")
            self.assertIsNotNone(row["stage_id"])

    def test_real_operation_slot_cleanup_source_and_context_restoration(self):
        observer, native, client, _ = self.setup_observation()
        with ExitStack() as stack:
            native.install(stack, lambda: 0)
            slot = OperationSlot(
                client, ROOT / ".cache/native-stage-evidence-audit", "companion_a", "e" * 32
            )
            slot.acquire()
            try:
                with slot.borrow(slot.binding, cleanup=True):
                    row = native.scope_snapshot()
                    self.assertEqual(row["cleanup_source"], "management_cleanup_borrow")
                    self.assertEqual(row["io_scope_state"], "disabled")
                    self.assertTrue(row["stage_thread_matches"])
                self.assertEqual(native.scope_snapshot()["cleanup_source"], "unknown")
            finally:
                slot.release()

    def test_parent_then_child_stream_join_and_early_release_rejection(self):
        rows = self.run_check()
        parent = [r for r in rows if r["phase"] == "io_slot"]
        child = [r for r in rows if r["phase"] == "native_validation"]
        self.assertEqual(verify_native_stage_intervals(parent + child), 1)
        altered = [dict(r) for r in parent + child]
        for row in altered:
            if row.get("event") == "released":
                row["at_s"] = 0
        with self.assertRaisesRegex(
            ValueError, "native_stage_unconfirmed|native_resources_after_release"
        ):
            verify_native_stage_intervals(altered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
