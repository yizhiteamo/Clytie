"""Observe real in-memory budget/Owner calls; no product data or formal trial."""

import itertools
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from tests.measurement_tools import load
from tests.test_measurement_pressure_validation import Index

from clytie.backup.io_control import IoControl
from clytie.sdk.testing import MockClock
from clytie.storage.owner import StorageOwner

io_draft = load("measurement_io_observation")
accounting = load("measurement_budget_observation")
validation = load("measurement_pressure_validation")
Predicates, verify_deduction, verify_refill = (
    validation.Predicates,
    validation.verify_deduction,
    validation.verify_refill,
)
evidence = load("measurement_evidence")
ControlObservation = load("measurement_control_observation").ControlObservation
PressureObservation = load("measurement_pressure_observation").PressureObservation


class Recorder:
    format_version = 6
    failed = False

    def __init__(self):
        self.rows = []

    def append(self, name, row):
        if row["phase"] in {"budget_accounting", "pressure_source_binding"}:
            accounting.validate_record(row)
        elif row["phase"] == "io_charge":
            io_draft.validate_record(row, format_version=6)
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


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mb-"))
        self.recorder = Recorder()
        ticks = itertools.count(1)
        self.observer = SimpleNamespace(
            root=self.root,
            recorder=self.recorder,
            guard=threading.Lock(),
            sequence=1 << 40,
            process_sequence=0,
            now=lambda: next(ticks),
            pressure=None,
        )
        self.io = io_draft.IoObservation(self.observer, 0)
        self.pressure = PressureObservation(self.io)
        self.observer.pressure = self.pressure
        self.io.pressure = self.pressure
        self.accounting = accounting.BudgetObservation(self.io)
        self.stack = ExitStack()
        self.io.install(self.stack)
        ControlObservation(self.io).install(self.stack)
        self.pressure.install(self.stack)
        self.accounting.install(self.stack)
        self.addCleanup(self.stack.close)

    async def test_actual_refill_and_charge_sequence_no_new_clock_reads(self):
        clock = CountingClock()
        control = IoControl(clock)
        session = control.register()
        control.authorize(session, "fictional_operation")
        endpoint = control.sessions[session].endpoints["fictional_operation"]
        await endpoint.request(dict(action="acquire", stage="fictional_stage_a"))
        before = clock.calls
        await endpoint.request(
            dict(
                action="charge",
                stage="fictional_stage_a",
                sequence=1,
                direction="read",
                size=256 * 1024,
            )
        )
        self.assertEqual(clock.calls, before + 1)
        await endpoint.request(
            dict(
                action="charge",
                stage="fictional_stage_a",
                sequence=2,
                direction="write",
                size=256 * 1024,
            )
        )
        await endpoint.request(dict(action="release", stage="fictional_stage_a"))
        clock.advance(0.01)
        await endpoint.request(dict(action="acquire", stage="fictional_stage_b"))
        await endpoint.request(
            dict(action="charge", stage="fictional_stage_b", sequence=1, direction="read", size=1)
        )
        await endpoint.request(dict(action="release", stage="fictional_stage_b"))
        self.assertFalse(self.recorder.failed)
        rows = self.recorder.rows
        refills = {row["refill_id"]: row for row in rows if row.get("event") == "refill_returned"}
        for row in refills.values():
            verify_refill(row)
        for row in rows:
            if row.get("event") == "charge_returned":
                verify_deduction(refills[row["refill_id"]], row)
        index = Index(rows)
        try:
            report = Predicates(index).report()
        finally:
            index.db.close()
        self.assertEqual(
            report["requirements"]["token_16_4_mibps_512kib_model"]["status"], "verified"
        )
        self.assertEqual(sum(row.get("event") == "budget_created" for row in rows), 1)

    async def test_real_owner_object_bound_before_actual_sample(self):
        clock = CountingClock()
        owner = await StorageOwner.start(
            lambda: SimpleNamespace(close=lambda: None),
            companion_id="companion_a",
            clock=clock,
            coordination_path=self.root / "fictional-owner-capture.lock",
        )
        try:
            control = IoControl(clock)
            session = control.register(storage_owner=True)
            self.accounting.bind_source(owner, control, session)
            control.pressure(session, owner.sample_io_pressure())
            binding = next(
                row for row in self.recorder.rows if row["phase"] == "pressure_source_binding"
            )
            sample = next(row for row in self.recorder.rows if row["phase"] == "owner_pressure")
            received = next(
                row
                for row in self.recorder.rows
                if row["phase"] == "control_pressure" and row["event"] == "received"
            )
            self.assertEqual(binding["owner_id"], self.pressure.identifier(owner))
            self.assertEqual(binding["owner_id"], sample["owner_id"])
            self.assertEqual(binding["budget_id"], received["pressure_budget_id"])
            self.assertEqual(binding["owner_hash"], received["owner_hash"])
            self.assertLess(binding["at_s"], sample["at_s"])
            wrong_session = control.register(storage_owner=False)
            with self.assertRaisesRegex(ValueError, "pressure_source_binding_invalid"):
                self.accounting.bind_source(owner, control, wrong_session)
            index = Index(self.recorder.rows)
            try:
                report = Predicates(index).report()
            finally:
                index.db.close()
            self.assertEqual(
                report["requirements"]["pressure_owner_identity"]["status"], "verified"
            )
            self.assertFalse(self.recorder.failed)
        finally:
            await owner.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
