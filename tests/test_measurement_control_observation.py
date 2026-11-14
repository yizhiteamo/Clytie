import copy
import itertools
import json
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.measurement_tools import load

from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.plugin_host.locking import process_identity
from clytie.sdk.testing import MockClock

draft = load("measurement_control_observation")
measurement_evidence = load("measurement_evidence")
IoObservation = load("measurement_io_observation").IoObservation


class Recorder:
    format_version = 6
    failed = False

    def __init__(self):
        self.rows = []

    def append(self, name, row):
        if row["phase"] == draft.PHASE:
            draft.validate_record(row)
        else:
            measurement_evidence.validate_record(name, row, format_version=6)
        self.rows.append(row)


class ControlTests(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def temporary_home(self, tmp_path):
        self.root = tmp_path

    def setUp(self):
        # Retain tiny synthetic files; no recursive deletion or actual home access.
        self.recorder = Recorder()
        tick = itertools.count(1)
        observer = SimpleNamespace(
            root=self.root,
            recorder=self.recorder,
            guard=threading.Lock(),
            sequence=1 << 40,
            process_sequence=0,
            now=lambda: next(tick),
        )
        self.io = IoObservation(observer, 0)
        self.binding = draft.ControlObservation(self.io)
        self.stack = ExitStack()
        self.io.install(self.stack)
        self.binding.install(self.stack)
        self.addCleanup(self.stack.close)

    async def exercise(self):
        control = IoControl(MockClock(), home=self.root)
        session = control.register(storage_owner=False)
        operation, stage = uuid4().hex, uuid4().hex
        identity = process_identity()
        await control.reserve(session, operation)
        await control.bind_execution(session, operation, identity)
        endpoint = control.sessions[session].endpoints[operation]
        await endpoint.request(dict(action="acquire", stage=stage))
        await endpoint.request(
            dict(action="charge", stage=stage, sequence=1, direction="read", size=17)
        )
        await endpoint.request(dict(action="release", stage=stage))
        self.assertTrue(await control.retire_operation(operation))
        await control.ended(session, confirmed=True)
        self.assertFalse(self.recorder.failed)
        return control, session, operation, stage, identity

    async def test_actual_control_durable_instance_endpoint_and_generation(self):
        control, session, operation, stage, identity = await self.exercise()
        rows = self.recorder.rows
        result = draft.single_instance_bindings(rows)
        self.assertEqual(
            result, dict(status="observed_bindings_complete", reasons=[], stages=1, unbound_io=0)
        )
        anchor = json.loads((self.root / "supervisor/io-responsibility/anchor.json").read_text())
        ledger = json.loads((self.root / "supervisor/io-responsibility/index.json").read_text())
        generation = ledger["index"][0]
        endpoint = next(row for row in rows if row.get("event") == "endpoint_bound")
        self.assertEqual(endpoint["instance_hash"], draft.token_hash(anchor["instance"]))
        self.assertEqual(endpoint["controller_hash"], draft.token_hash(control.controller))
        self.assertEqual(endpoint["session_hash"], draft.token_hash(session))
        self.assertEqual(endpoint["operation_hash"], draft.token_hash(operation))
        self.assertEqual(endpoint["generation_hash"], draft.token_hash(generation))
        self.assertEqual(endpoint["execution_hash"], draft.identity_hash(identity))
        self.assertEqual(endpoint["budget_id"], self.io.budget_identifier(control.budget))
        grants = [
            row for row in rows if row.get("phase") == "io_slot" and row["event"] == "granted"
        ]
        self.assertEqual(grants[0]["stage_id"], draft.token_hash(stage))
        states = {
            row["responsibility_state"]
            for row in rows
            if row.get("event") == "responsibility_observed"
        }
        self.assertEqual(states, {"reserved", "active", "ended"})
        raw = json.dumps(rows)
        for value in (
            str(self.root),
            anchor["instance"],
            control.controller,
            session,
            operation,
            stage,
            generation,
            identity["started_at"],
        ):
            self.assertNotIn(value, raw)

    async def test_reloaded_persistent_instance_same_controller_not_inferred_from_path(self):
        first, *_ = await self.exercise()
        replacement = IoControl(MockClock(), home=self.root)
        self.assertEqual((await replacement.recheck())["state"], "ready")
        contexts = self.binding.controls
        self.assertEqual(contexts[first]["instance_hash"], contexts[replacement]["instance_hash"])
        self.assertNotEqual(
            contexts[first]["controller_hash"], contexts[replacement]["controller_hash"]
        )
        result = draft.single_instance_bindings(self.recorder.rows)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("controller_multiplicity_uncovered", result["reasons"])

    async def test_original_stage_binding_cannot_be_replaced_by_same_operation(self):
        await self.exercise()
        rows = copy.deepcopy(self.recorder.rows)
        for row in rows:
            if (
                row.get("event") in {"request_started", "request_finished"}
                and row.get("action") == "acquire"
            ):
                row["stage_id"] = "f" * 64
        result = draft.single_instance_bindings(rows)
        self.assertIn("slot_endpoint_unconfirmed", result["reasons"])

    async def test_missing_or_forged_responsibility_cannot_pass(self):
        await self.exercise()
        rows = [row for row in self.recorder.rows if row.get("event") != "responsibility_observed"]
        self.assertIn(
            "responsibility_source_unconfirmed", draft.single_instance_bindings(rows)["reasons"]
        )
        rows = copy.deepcopy(self.recorder.rows)
        for row in rows:
            if (
                row.get("event") in {"request_started", "request_finished"}
                and row.get("action") == "acquire"
            ):
                row["generation_hash"] = "f" * 64
        result = draft.single_instance_bindings(rows)
        self.assertIn("endpoint_source_unconfirmed", result["reasons"])
        self.assertIn("responsibility_source_unconfirmed", result["reasons"])

    async def test_save_only_and_lost_request_end_remain_unknown(self):
        await self.exercise()
        rows = [row for row in self.recorder.rows if row.get("event") != "ledger_loaded"]
        result = draft.single_instance_bindings(rows)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("slot_controller_unconfirmed", result["reasons"])
        rows = [row for row in self.recorder.rows if row.get("event") != "request_finished"]
        self.assertIn("control_call_incomplete", draft.single_instance_bindings(rows)["reasons"])

    async def test_empty_unmapped_and_unbound_are_unknown(self):
        self.assertEqual(draft.single_instance_bindings([])["status"], "unknown")
        await self.exercise()
        nohome = IoControl(MockClock())
        self.assertIsNone(self.binding.controls[nohome]["instance_hash"])
        rows = list(self.recorder.rows)
        rows.append(dict(phase="file_batch", event="started", stage_id=None))
        result = draft.single_instance_bindings(rows)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["unbound_io"], 1)
        self.assertIn("controller_scope_unknown", result["reasons"])
        self.assertIn("unbound_io_requires_scope_classification", result["reasons"])

    async def test_endpoint_exception_preserves_original_failure(self):
        control = IoControl(MockClock(), home=self.root)
        session, operation = control.register(), uuid4().hex
        await control.authorize_execution(session, operation, process_identity())
        endpoint = control.sessions[session].endpoints[operation]
        with self.assertRaises(IoBudgetError) as raised:
            await endpoint.request(dict(action="acquire", stage="fictional_invalid!"))
        self.assertEqual(raised.exception.code, "backup_io_request_invalid")
        self.assertTrue(
            any(
                row.get("event") == "request_finished" and row["outcome"] == "failed"
                for row in self.recorder.rows
            )
        )
        await control.retire_operation(operation)
        await control.ended(session, confirmed=True)
        self.assertFalse(self.recorder.failed)

    async def test_order_independent_join_and_schema_rejects_raw_identifier(self):
        await self.exercise()
        result = draft.single_instance_bindings(list(reversed(self.recorder.rows)))
        self.assertEqual(result["status"], "observed_bindings_complete")
        row = copy.deepcopy(
            next(row for row in self.recorder.rows if row.get("event") == "endpoint_bound")
        )
        row["operation_hash"] = "fictional_plain_operation"
        with self.assertRaisesRegex(draft.BindingError, "control_binding_hash_invalid"):
            draft.validate_record(row)

    async def test_failed_real_ledger_load_remains_unknown(self):
        control, *_ = await self.exercise()
        # A new record is fabricated locally; no deployed data is read or overwritten.
        path = self.root / "supervisor/io-responsibility/index.json"
        path.write_text("{}", encoding="utf-8")
        self.assertEqual((await control.recheck())["state"], "blocked")
        result = draft.single_instance_bindings(self.recorder.rows)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("ledger_evidence_failed", result["reasons"])
        self.assertFalse(self.recorder.failed)

    async def test_changed_real_endpoint_cannot_keep_old_authorization_binding(self):
        control = IoControl(MockClock(), home=self.root)
        session, operation = control.register(), uuid4().hex
        await control.authorize_execution(session, operation, process_identity())
        endpoint = control.sessions[session].endpoints[operation]
        original = endpoint.operation
        try:
            endpoint.operation = "fictional_different_operation"
            self.assertEqual(self.binding.request_context(endpoint), {})
            self.assertTrue(self.recorder.failed)
        finally:
            endpoint.operation = original
            await control.retire_operation(operation)
            await control.ended(session, confirmed=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
