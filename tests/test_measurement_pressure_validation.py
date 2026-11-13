import copy
import hashlib
import json
import sqlite3
import unittest

from tests.measurement_tools import load

validation = load("measurement_pressure_validation")
BURST = validation.BURST
BUSY = validation.BUSY
CHUNK = validation.CHUNK
NORMAL = validation.NORMAL
Invalid = validation.Invalid
Predicates = validation.Predicates
refill_expected = validation.refill_expected
verify_deduction = validation.verify_deduction
verify_refill = validation.verify_refill


B, H = (1 << 40) + 5, "a" * 64


class Index:
    def __init__(self, rows):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE rows(id INTEGER,phase TEXT,event TEXT,t REAL,stage TEXT,budget INTEGER,transfer INTEGER,j TEXT)"
        )
        for number, row in enumerate(rows, 1):
            row = {"id": (1 << 40) + 100 + number, "at_s": number / 10, **row}
            self.db.execute(
                "INSERT INTO rows VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["phase"],
                    row.get("event"),
                    row["at_s"],
                    row.get("stage_id"),
                    row.get("budget_id"),
                    row.get("transfer_id"),
                    json.dumps(row),
                ),
            )

    def rows(self, where, args=(), order="id"):
        for (raw,) in self.db.execute(
            "SELECT j FROM rows WHERE " + where + " ORDER BY " + order, args
        ):
            yield json.loads(raw)


def pressure_rows(wait=0.020, run=0, final=False, normal=False, low=None):
    sample = dict(
        source="owner_control_cache",
        observed_at_unix_us=123,
        ready_wait_seconds=wait,
        execution_seconds=run,
        finalizer_wait_seconds=0,
        finalizer_capacity_blocked=final,
        ordinary=0,
        finalizers=0,
    )
    digest = hashlib.sha256(
        json.dumps(sample, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return [
        dict(
            phase="io_control_binding",
            event="session_registered",
            budget_id=B,
            session_hash=H,
            storage_owner=True,
            source="runtime_object",
            outcome="ok",
        ),
        dict(phase="io_slot", event="granted", budget_id=B, stage_id=H),
        dict(
            phase="owner_pressure", owner_id=B + 1, sample=sample, sample_hash=digest, outcome="ok"
        ),
        dict(
            phase="control_pressure",
            event="received",
            receipt_id=B + 2,
            pressure_budget_id=B,
            owner_hash=H,
            sample=sample,
            sample_hash=digest,
            outcome="unknown",
        ),
        dict(
            phase="budget_pressure",
            receipt_id=B + 2,
            pressure_budget_id=B,
            owner_hash=H,
            waiting_s=wait,
            executing_s=run,
            finalizer_wait=final,
            received_monotonic_s=10,
            accepted=True,
            normal=normal,
            low_since_monotonic_s=low,
            outcome="ok",
        ),
        dict(
            phase="control_pressure",
            event="returned",
            receipt_id=B + 2,
            pressure_budget_id=B,
            owner_hash=H,
            sample=sample,
            sample_hash=digest,
            outcome="ok",
        ),
    ]


def mode(clock, normal, low, *, owners=1, signals=1):
    return dict(
        phase="budget_mode",
        pressure_budget_id=B,
        updated_monotonic_s=clock,
        normal=normal,
        low_since_monotonic_s=low,
        owners_count=owners,
        signals_count=signals,
    )


class PredicateTests(unittest.TestCase):
    def report(self, rows):
        index = Index(rows)
        try:
            return Predicates(index).report()
        finally:
            index.db.close()

    def status(self, rows, name):
        return self.report(rows)["requirements"][name]["status"]

    def test_empty_never_passes(self):
        report = self.report([])
        self.assertEqual(report["mechanism_acceptance"], "not_evaluated")
        self.assertTrue(
            all(value["status"] == "unverified" for value in report["requirements"].values())
        )

    def test_actual_receipt_fields_match_but_owner_identity_unknown(self):
        result = self.report(pressure_rows())["requirements"]
        self.assertEqual(result["pressure_receipt_budget_binding"]["status"], "verified")
        self.assertEqual(result["owner_sample_value_witness"]["status"], "verified")
        self.assertEqual(result["pressure_owner_identity"]["status"], "unverified")
        self.assertEqual(result["token_16_4_mibps_512kib_model"]["status"], "unverified")

    def test_same_wall_time_does_not_join_different_budget(self):
        rows = pressure_rows()
        rows[4]["pressure_budget_id"] += 1
        self.assertEqual(self.status(rows, "pressure_receipt_budget_binding"), "failed")

    def test_values_changed_in_actual_receipt_fail(self):
        rows = pressure_rows()
        rows[4]["waiting_s"] = 0
        self.assertEqual(self.status(rows, "pressure_receipt_budget_binding"), "failed")

    def test_missing_receipt_return_is_unverified(self):
        self.assertEqual(
            self.status(pressure_rows()[:-1], "pressure_receipt_budget_binding"), "unverified"
        )

    def test_unknown_session_registration_is_not_identity_proof(self):
        rows = pressure_rows()
        rows[0]["outcome"] = "unknown"
        self.assertEqual(self.status(rows, "pressure_receipt_budget_binding"), "unverified")

    def test_budget_creation_without_deduction_is_unverified(self):
        rows = [
            dict(
                phase="budget_accounting",
                event="budget_created",
                budget_id=B,
                outcome="ok",
                after=dict(
                    tokens=BURST,
                    charged={"read": 0, "write": 0},
                    updated=0,
                    normal=False,
                    low_since=None,
                ),
            )
        ]
        self.assertEqual(self.status(rows, "token_16_4_mibps_512kib_model"), "unverified")

    def test_exact_busy_threshold_and_finalizer(self):
        for kwargs in (dict(wait=0.020), dict(wait=0, run=0.020), dict(wait=0, final=True)):
            self.assertEqual(
                self.status(pressure_rows(**kwargs), "pressure_busy_threshold_observed"), "verified"
            )
            self.assertEqual(
                self.status(
                    pressure_rows(**kwargs, normal=True), "pressure_busy_threshold_observed"
                ),
                "failed",
            )
        self.assertEqual(
            self.status(pressure_rows(wait=0.019), "pressure_busy_threshold_observed"), "unverified"
        )

    def test_missing_and_expired_signals_cannot_be_normal(self):
        self.assertEqual(
            self.status(
                [mode(10, True, None, signals=0)], "pressure_observed_stale_or_missing_signal"
            ),
            "failed",
        )
        rows = pressure_rows(wait=0)
        rows.append(mode(11.0001, True, 10))
        self.assertEqual(self.status(rows, "pressure_observed_stale_or_missing_signal"), "failed")
        rows[-1] = mode(11.0001, False, None)
        self.assertEqual(self.status(rows, "pressure_observed_stale_or_missing_signal"), "verified")

    def test_one_second_age_boundary_not_prematurely_stale(self):
        rows = pressure_rows(wait=0) + [mode(11, True, 10)]
        self.assertEqual(
            self.status(rows, "pressure_observed_stale_or_missing_signal"), "unverified"
        )

    def test_recovery_uses_actual_monotonic_not_wall(self):
        self.assertEqual(
            self.status(
                [mode(100, False, 100), mode(100.99, True, 100)],
                "pressure_recovery_recorded_minimum",
            ),
            "failed",
        )
        self.assertEqual(
            self.status(
                [mode(100, False, 100), mode(101, True, 100)], "pressure_recovery_recorded_minimum"
            ),
            "verified",
        )

    def test_normal_low_timer_restart_is_not_new_recovery(self):
        self.assertEqual(
            self.status(
                [mode(100, True, None), mode(100.1, True, 100.1)],
                "pressure_recovery_recorded_minimum",
            ),
            "unverified",
        )

    def test_successful_charge_chunk_and_actual_bytes(self):
        base = dict(
            phase="io_charge",
            transfer_id=B + 3,
            stage_id=H,
            budget_id=B,
            direction="read",
            requested_bytes=CHUNK,
        )
        rows = [
            dict(base, event="started", outcome="unknown", charged_bytes=None),
            dict(base, event="finished", outcome="ok", charged_bytes=CHUNK),
        ]
        self.assertEqual(self.status(rows, "charge_success_bytes_and_chunk"), "verified")
        rows[1]["charged_bytes"] -= 1
        self.assertEqual(self.status(rows, "charge_success_bytes_and_chunk"), "failed")
        for row in rows:
            row["requested_bytes"] = CHUNK + 1
        rows[1]["charged_bytes"] = CHUNK + 1
        self.assertEqual(self.status(rows, "charge_success_bytes_and_chunk"), "failed")

    def test_causal_binding_report_points_to_separate_batch_checks(self):
        index = Index([])
        try:
            report = Predicates(index).report()
            for key, reference in (
                ("file_charge_causal_binding", "host_file_batch_reservations"),
                ("sqlite_charge_causal_binding", "sqlite_logical_progress_reservations"),
            ):
                item = report["requirements"][key]
                self.assertEqual(item["status"], "unverified")
                self.assertIn(
                    "checked_separately_in_batch_reservations_" + reference, item["reasons"]
                )
            self.assertEqual(report["mechanism_acceptance"], "not_evaluated")
        finally:
            index.db.close()

    def test_same_stage_totals_do_not_prove_file_charge_causality(self):
        self.assertEqual(self.status([], "file_charge_causal_binding"), "unverified")

    def test_real_sqlite_geometry_not_progress_count_as_bytes(self):
        transfer = B + 4
        rows = [
            dict(
                phase="sqlite_geometry",
                event="backup_started",
                transfer_id=transfer,
                page_size=4096,
                page_count=65,
                stage_id=H,
            ),
            dict(
                phase="sqlite_batch",
                event="started",
                transfer_id=transfer,
                pages_per_step=64,
                stage_id=H,
            ),
            dict(
                phase="sqlite_batch",
                event="progress",
                transfer_id=transfer,
                status=0,
                total_pages=65,
                remaining_pages=1,
                copied_pages=64,
            ),
            dict(
                phase="sqlite_batch",
                event="progress",
                transfer_id=transfer,
                status=101,
                total_pages=65,
                remaining_pages=0,
                copied_pages=1,
            ),
            dict(phase="sqlite_batch", event="finished", transfer_id=transfer, outcome="ok"),
        ]
        self.assertEqual(self.status(rows, "sqlite_fixed_view_batch_geometry"), "verified")
        self.assertEqual(self.status(rows, "sqlite_charge_causal_binding"), "unverified")
        rows[2]["copied_pages"] = 1
        self.assertEqual(self.status(rows, "sqlite_fixed_view_batch_geometry"), "failed")

    def test_sqlite_missing_geometry_is_unverified(self):
        rows = [
            dict(
                phase="sqlite_batch",
                event="started",
                transfer_id=B + 4,
                pages_per_step=64,
                stage_id=H,
            )
        ]
        self.assertEqual(self.status(rows, "sqlite_fixed_view_batch_geometry"), "unverified")


class ExactArithmeticTests(unittest.TestCase):
    def before(self, **kw):
        return dict(
            updated=0.0,
            tokens=0.0,
            normal=False,
            low_since=None,
            charged={"read": 0, "write": 0},
            **kw,
        )

    def test_fixed_busy_normal_burst(self):
        before = self.before()
        self.assertEqual(refill_expected(before, 0.01, None, {})["tokens"], 0.01 * BUSY)
        before["normal"] = True
        self.assertEqual(refill_expected(before, 0.01, [], {})["tokens"], 0.01 * NORMAL)
        self.assertEqual(refill_expected(before, 10, [], {})["tokens"], BURST)

    def test_split_stale_interval_is_not_old_normal_rate(self):
        before = self.before()
        before.update(updated=0.99, normal=True)
        after = refill_expected(before, 1.01, [H], {H: [0, 0, 0, False]})
        self.assertEqual(after["tokens"], (1 - 0.99) * NORMAL + (1.01 - 0.99 - (1 - 0.99)) * BUSY)
        self.assertFalse(after["normal"])

    def test_recovery_exact_one_second(self):
        before = self.before()
        before.update(updated=0.99, low_since=0.0)
        after = refill_expected(before, 1.01, [H], {H: [0.99, 0, 0, False]})
        self.assertEqual(after["tokens"], (1.01 - 1) * NORMAL + (1.01 - 0.99 - (1.01 - 1)) * BUSY)
        self.assertTrue(after["normal"])

    def test_snapshot_mismatch_not_tolerance_accepted(self):
        before = self.before()
        snapshot = dict(
            before=before, after=refill_expected(before, 0.01, None, {}), owners=None, signals={}
        )
        verify_refill(snapshot)
        snapshot["after"]["tokens"] += float("0.0" + "000" + "1")
        with self.assertRaises(Invalid):
            verify_refill(snapshot)

    def test_deduction_requires_same_sequence_and_balance(self):
        fields = dict(budget_id=B, charge_call_id=B + 9, stage_id=H, charge_sequence=1)
        refill = dict(fields, after=self.before())
        refill["after"]["tokens"] = BURST
        charge = dict(
            fields,
            after=copy.deepcopy(refill["after"]),
            requested_bytes=CHUNK,
            direction="write",
            outcome="ok",
        )
        charge["after"]["tokens"] -= CHUNK
        charge["after"]["charged"]["write"] += CHUNK
        verify_deduction(refill, charge)
        charge["charge_sequence"] = 2
        with self.assertRaisesRegex(Invalid, "token_charge_identity_mismatch"):
            verify_deduction(refill, charge)


if __name__ == "__main__":
    unittest.main(verbosity=2)
