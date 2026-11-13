import asyncio
import copy
import io
import itertools
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from tests.measurement_tools import load

from clytie.backup import io_client, sqlite_io
from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.sdk.testing import MockClock

io_draft = load("measurement_io_observation")
evidence_draft = load("measurement_evidence")
evidence = evidence_draft
BatchPredicates = load("measurement_batch_validation").BatchPredicates
_batch = load("measurement_batch_observation")
BatchObservation, validate_record = _batch.BatchObservation, _batch.validate_record
ControlObservation = load("measurement_control_observation").ControlObservation
PressureObservation = load("measurement_pressure_observation").PressureObservation


class Index:
    def __init__(self, rows):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE rows(id INTEGER,phase TEXT,event TEXT,t REAL,stage TEXT,budget INTEGER,transfer INTEGER,j TEXT)"
        )
        self.db.executemany(
            "INSERT INTO rows VALUES(?,?,?,?,?,?,?,?)",
            (
                (
                    r["id"],
                    r["phase"],
                    r.get("event"),
                    r["at_s"],
                    r.get("stage_id"),
                    r.get("budget_id"),
                    r.get("transfer_id"),
                    json.dumps(r),
                )
                for r in rows
            ),
        )

    def rows(self, where, args=(), order="id"):
        for (raw,) in self.db.execute(
            "SELECT j FROM rows WHERE " + where + " ORDER BY " + order, args
        ):
            yield json.loads(raw)


def report(rows):
    index = Index(rows)
    try:
        return BatchPredicates(index).report()["requirements"]
    finally:
        index.db.close()


class Recorder:
    format_version = 6

    def __init__(self):
        self.failed, self.rows = False, []

    def append(self, name, row):
        if row["phase"] == "batch_charge_binding":
            validate_record(row)
            evidence_draft.validate_record(name, row, format_version=6)
        else:
            evidence.validate_record(name, row, format_version=6)
        self.rows.append(row)


class CountingClock(MockClock):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def monotonic(self):
        self.calls += 1
        return super().monotonic()


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recorder = Recorder()
        ticks = itertools.count(1)
        host = SimpleNamespace(
            root=Path(__file__).resolve().parents[1],
            recorder=self.recorder,
            guard=threading.Lock(),
            sequence=1 << 40,
            process_sequence=0,
            now=lambda: next(ticks),
        )
        self.io = io_draft.IoObservation(host, 0)
        self.pressure = PressureObservation(self.io)
        self.io.pressure = self.pressure
        self.batch = BatchObservation(self.io)
        self.io.batch = self.batch
        self.stack = ExitStack()
        self.io.install(self.stack)
        ControlObservation(self.io).install(self.stack)
        self.pressure.install(self.stack)
        self.batch.install(self.stack)
        self.addCleanup(self.stack.close)
        self.clock = CountingClock()
        self.control = IoControl(self.clock)
        session = self.control.register()
        self.control.authorize(session, "fictional_operation")
        self.endpoint = self.control.sessions[session].endpoints["fictional_operation"]

    async def run_worker(self, work):
        loop = asyncio.get_running_loop()
        endpoint = self.endpoint

        def request(arguments, *, cleanup=False):
            return asyncio.run_coroutine_threadsafe(endpoint.request(arguments), loop).result()

        client = io_client.IoClient(request)

        def worker():
            with io_client.io_scope(client), client.stage():
                return work(client)

        value = await asyncio.to_thread(worker)
        self.assertFalse(self.recorder.failed)
        self.assertIsNone(self.control.budget._active)
        return value

    def charges(self):
        return [
            r for r in self.recorder.rows if r["phase"] == "io_charge" and r["event"] == "finished"
        ]

    async def test_actual_file_bytesio_and_regular_read_exact_sequences(self):
        def work(client):
            stream = io.BytesIO()
            with client.stage():
                self.assertEqual(io_client.io_write(stream, b"fictional"), 9)
            with tempfile.TemporaryFile() as file:
                file.write(
                    b"fictional"
                )  # Fixture setup outside observed host helper; no compliance claim.
                file.seek(0)
                self.assertEqual(io_client.io_read(file, 20), b"fictional")

        await self.run_worker(work)
        self.assertEqual([r["charge_sequence"] for r in self.charges()], [1, 2])
        self.assertEqual(len({r["stage_id"] for r in self.charges()}), 1)
        result = report(self.recorder.rows)
        self.assertEqual(result["host_file_batch_reservations"]["status"], "verified")
        self.assertEqual(result["all_heavy_io_rate_limited"]["status"], "unverified")
        # Raw order may be parent+child concatenation; identity-based join does not
        # assume chronological JSONL ordering across processes.
        self.assertEqual(report(reversed(self.recorder.rows)), result)
        missing = [r for r in self.recorder.rows if r.get("event") != "file_link"]
        self.assertEqual(report(missing)["host_file_batch_reservations"]["status"], "unverified")
        wrong = copy.deepcopy(self.recorder.rows)
        for r in wrong:
            if r["phase"] == "io_charge" and r["event"] == "finished":
                r["charged_bytes"] += 1
        self.assertEqual(report(wrong)["host_file_batch_reservations"]["status"], "failed")

    async def test_no_new_clock_or_checkpoint_calls(self):
        checks = 0

        def work(client):
            def check():
                nonlocal checks
                checks += 1

            before = self.clock.calls
            with io_client.io_checks(check):
                io_client.io_write(io.BytesIO(), b"fictional")
            self.assertEqual(self.clock.calls - before, 1)

        await self.run_worker(work)
        self.assertEqual(checks, 2)

    async def test_observer_failure_does_not_prevent_write_or_release(self):
        original = self.batch.source

        def fail(*args):
            raise ValueError("fictional_observer_failure")

        self.batch.source = fail
        loop = asyncio.get_running_loop()

        def request(arguments, *, cleanup=False):
            return asyncio.run_coroutine_threadsafe(self.endpoint.request(arguments), loop).result()

        def work():
            client = io_client.IoClient(request)
            stream = io.BytesIO()
            with io_client.io_scope(client), client.stage():
                io_client.io_write(stream, b"fictional")
            return stream.getvalue()

        try:
            self.assertEqual(await asyncio.to_thread(work), b"fictional")
            self.assertTrue(self.recorder.failed)
            self.assertIsNone(self.control.budget._active)
        finally:
            self.batch.source = original

    async def test_short_write_keeps_reservation_distinct_from_actual(self):
        class Short(io.BytesIO):
            def write(self, value):
                return super().write(value[:2])

        def work(client):
            with self.assertRaisesRegex(IoBudgetError, "backup_io_short_write"):
                io_client.io_write(Short(), b"fictional")

        await self.run_worker(work)
        actual = next(
            r for r in self.recorder.rows if r["phase"] == "file_batch" and r["event"] == "finished"
        )
        operation = next(r for r in self.recorder.rows if r.get("event") == "file_finished")
        self.assertEqual((actual["actual_bytes"], self.charges()[0]["charged_bytes"]), (2, 9))
        self.assertEqual(operation["outcome"], "failed")
        self.assertEqual(
            report(self.recorder.rows)["host_file_batch_reservations"]["status"], "verified"
        )

    async def test_post_charge_cancel_has_no_actual_file_batch_and_releases(self):
        checks = 0

        def work(client):
            def check():
                nonlocal checks
                checks += 1
                if checks == 2:
                    raise IoBudgetError("cancelled")

            with io_client.io_checks(check), self.assertRaisesRegex(IoBudgetError, "cancelled"):
                io_client.io_write(io.BytesIO(), b"fictional")

        await self.run_worker(work)
        self.assertEqual(len(self.charges()), 1)
        self.assertFalse(any(r["phase"] == "file_batch" for r in self.recorder.rows))
        self.assertEqual(
            report(self.recorder.rows)["host_file_batch_reservations"]["status"], "unverified"
        )

    async def test_disabled_scope_with_held_stage_is_unknown_not_exempt(self):
        def work(client):
            with io_client.io_scope(None):
                self.assertEqual(io_client.io_read(io.BytesIO(b"fictional"), 9), b"fictional")

        await self.run_worker(work)
        link = next(r for r in self.recorder.rows if r.get("event") == "file_link")
        self.assertIsNotNone(link["stage_id"])
        self.assertEqual(link["scope_state"], "disabled")
        self.assertIsNone(link["client_charge_id"])
        self.assertEqual(
            report(self.recorder.rows)["host_file_batch_reservations"]["status"], "unverified"
        )

    def connection_type(self):
        observer = self

        class Connection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                return observer.pressure.cursor(self, sql, super().execute(sql, *args, **kwargs))

            def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
                return observer.io.backup(
                    super().backup, target, pages=pages, progress=progress, name=name, sleep=sleep
                )

        return Connection

    async def test_real_sqlite_reserve_and_callback_charges_not_confused(self):
        checks = 0

        def work(client):
            source = sqlite3.connect(":memory:", factory=self.connection_type())
            target = sqlite3.connect(":memory:")
            try:
                source.execute("CREATE TABLE fictional(value TEXT)")
                source.execute("INSERT INTO fictional VALUES ('fictional')")
                source.commit()
                called = False

                def check():
                    nonlocal called, checks
                    checks += 1
                    if not called:
                        called = True
                        client.charge("read", 1)  # Actual callback charge; not a reserve charge.

                before = self.clock.calls
                sqlite_io.copy_database(source, target, pages=1, check=check)
                self.assertEqual(self.clock.calls - before, 5)
                self.assertEqual(target.execute("SELECT count(*) FROM fictional").fetchone()[0], 1)
            finally:
                source.close()
                target.close()

        await self.run_worker(work)
        # Two extra invocations are from the explicit callback's own charge.
        self.assertEqual(checks, 17)
        requests = [
            r
            for r in self.recorder.rows
            if r["phase"] == "batch_charge_binding" and r.get("event") == "request_finished"
        ]
        self.assertEqual(requests[0]["source"], "unclassified")
        self.assertTrue(all(r["source"] == "sqlite_reserve" for r in requests[1:]))
        links = [r for r in self.recorder.rows if r.get("event") == "sqlite_link"]
        self.assertEqual(len(links), 2)
        self.assertNotIn(requests[0]["client_charge_id"], [r["read_charge_id"] for r in links])
        self.assertEqual(
            report(self.recorder.rows)["sqlite_logical_progress_reservations"]["status"], "verified"
        )
        corrupted = copy.deepcopy(self.recorder.rows)
        for r in corrupted:
            if r.get("event") == "sqlite_link":
                r["read_charge_id"] = requests[0]["client_charge_id"]
                break
        self.assertEqual(
            report(corrupted)["sqlite_logical_progress_reservations"]["status"], "failed"
        )

    def test_empty_missing_and_old_formats_never_pass(self):
        result = report([])
        self.assertTrue(all(r["status"] == "unverified" for r in result.values()))
        for version in (1, 2, 3, 4, 5):
            with self.assertRaises(evidence.EvidenceError):
                evidence.validate_record(
                    "storage-phases.jsonl",
                    {"phase": "batch_charge_binding"},
                    format_version=version,
                )

    def test_historical_sqlite_without_budget_module_remains_unbound(self):
        self.io.client = None
        assert self.batch.scope()["scope_state"] == "unbound"
        self.batch.safe(lambda: self.batch.sqlite_link((1 << 40) + 500, (1 << 40) + 501))
        assert not self.recorder.failed
        link = self.recorder.rows[-1]
        assert link["scope_state"] == "unbound"
        assert link["read_charge_id"] is None
        assert link["write_charge_id"] is None

    async def test_sqlite_cancel_after_first_step_keeps_observed_partial_progress(self):
        checks = 0

        def work(client):
            source = sqlite3.connect(":memory:", factory=self.connection_type())
            target = sqlite3.connect(":memory:")
            try:
                source.execute("CREATE TABLE fictional(value TEXT)")
                source.commit()

                def check():
                    nonlocal checks
                    checks += 1
                    if checks == 7:
                        raise IoBudgetError("cancelled")

                with self.assertRaisesRegex(IoBudgetError, "cancelled"):
                    sqlite_io.copy_database(source, target, pages=1, check=check)
            finally:
                source.close()
                target.close()

        await self.run_worker(work)
        finished = next(
            row
            for row in self.recorder.rows
            if row["phase"] == "sqlite_batch" and row["event"] == "finished"
        )
        progress = [
            row
            for row in self.recorder.rows
            if row["phase"] == "sqlite_batch" and row["event"] == "progress"
        ]
        self.assertEqual(finished["outcome"], "cancelled")
        self.assertEqual(len(progress), 1)
        self.assertGreater(progress[0]["remaining_pages"], 0)
        self.assertEqual(len(self.charges()), 2)
        # This narrow predicate verifies only that observed partial progress had
        # reservations. The copy's cancellation/result is not overwritten.
        self.assertEqual(
            report(self.recorder.rows)["sqlite_logical_progress_reservations"]["status"], "verified"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
