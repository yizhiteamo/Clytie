"""Pure fictional evidence mutations; no producer or asyncio execution."""

import copy
import json
import sqlite3
import unittest

from tests.measurement_tools import load

BatchPredicates = load("measurement_batch_validation").BatchPredicates

B, STAGE = 1 << 40, "a" * 64


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
                    r["event"],
                    0,
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


def row(number, phase, event, **fields):
    return dict(
        id=B + number,
        process_sequence=0,
        phase=phase,
        event=event,
        at_s=number,
        stage_id=STAGE,
        **fields,
    )


def fixture():
    common = dict(
        operation_id=B + 101, scope_state="active", cleanup_source="unknown", source="host_file"
    )
    request = dict(
        **common, client_charge_id=B + 102, charge_sequence=1, direction="write", requested_bytes=5
    )
    server = dict(
        budget_id=B + 100,
        transfer_id=B + 103,
        direction="write",
        requested_bytes=5,
        charge_sequence=1,
    )
    batch = dict(transfer_id=B + 104, direction="write", requested_bytes=5)
    return [
        row(1, "io_slot", "granted", budget_id=B + 100),
        row(2, "batch_charge_binding", "file_started", **common, outcome="unknown"),
        row(3, "batch_charge_binding", "request_started", **request, outcome="unknown"),
        row(4, "io_charge", "started", **server, charged_bytes=None, outcome="unknown"),
        row(5, "io_charge", "finished", **server, charged_bytes=5, outcome="ok"),
        row(6, "batch_charge_binding", "request_finished", **request, outcome="ok"),
        row(
            7,
            "batch_charge_binding",
            "file_link",
            **common,
            client_charge_id=B + 102,
            transfer_id=B + 104,
            outcome="ok",
        ),
        row(8, "file_batch", "started", **batch, actual_bytes=None, outcome="unknown"),
        row(9, "file_batch", "finished", **batch, actual_bytes=5, outcome="ok"),
        row(10, "batch_charge_binding", "file_finished", **common, outcome="ok"),
    ]


def status(rows):
    index = Index(rows)
    try:
        return BatchPredicates(index).report()["requirements"]["host_file_batch_reservations"][
            "status"
        ]
    finally:
        index.db.close()


class Tests(unittest.TestCase):
    def test_exact_file_binding(self):
        self.assertEqual(status(fixture()), "verified")

    def test_missing_group_and_empty_unverified(self):
        self.assertEqual(status([]), "unverified")
        self.assertEqual(
            status([r for r in fixture() if r["phase"] != "batch_charge_binding"]), "unverified"
        )

    def test_unfinished_batch_does_not_disappear_from_predicate(self):
        rows = fixture()
        orphan = row(
            200,
            "file_batch",
            "started",
            transfer_id=B + 300,
            direction="write",
            requested_bytes=1,
            actual_bytes=None,
            outcome="unknown",
        )
        self.assertEqual(status([*rows, orphan]), "unverified")

    def test_old_server_no_sequence_unverified(self):
        rows = fixture()
        for r in rows:
            if r["phase"] == "io_charge":
                del r["charge_sequence"]
        self.assertEqual(status(rows), "unverified")

    def test_actual_over_reservation_failed(self):
        rows = fixture()
        rows[8]["actual_bytes"] = 6
        self.assertEqual(status(rows), "failed")

    def test_short_batch_success_contradiction_failed(self):
        rows = fixture()
        rows[8]["actual_bytes"] = 2
        self.assertEqual(status(rows), "failed")
        rows[9]["outcome"] = "failed"
        self.assertEqual(status(rows), "verified")

    def test_wrong_server_budget_failed(self):
        rows = fixture()
        rows[0]["budget_id"] += 1
        self.assertEqual(status(rows), "failed")

    def test_unconfirmed_request_not_consumable(self):
        rows = fixture()
        rows[5]["outcome"] = "cancelled"
        self.assertEqual(status(rows), "failed")

    def test_server_amount_and_source_contradictions(self):
        rows = fixture()
        rows[4]["charged_bytes"] = 4
        self.assertEqual(status(rows), "failed")
        rows = fixture()
        rows[2]["source"] = rows[5]["source"] = "unclassified"
        self.assertEqual(status(rows), "failed")

    def test_no_double_consumption(self):
        rows = fixture()
        extra = copy.deepcopy(rows[6])
        extra.update(id=B + 200, transfer_id=B + 201)
        self.assertEqual(status([*rows, extra]), "failed")

    def test_cross_process_join_is_not_clock_or_concatenation_order(self):
        rows = fixture()
        for r in rows:
            if r["phase"] in {"io_slot", "io_charge"}:
                r["id"] += B
                r["process_sequence"] = 1
                r["at_s"] = 999  # Deliberately unrelated clocks do not identify requests.
                r["budget_id"] += B
                if "transfer_id" in r:
                    r["transfer_id"] += B
        self.assertEqual(status(reversed(rows)), "verified")

    def test_disabled_scope_not_exempt_by_cleanup_marker(self):
        rows = fixture()
        rows[6]["scope_state"] = "disabled"
        rows[6]["cleanup_source"] = "management_cleanup_borrow"
        rows[6]["client_charge_id"] = None
        self.assertEqual(status(rows), "unverified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
