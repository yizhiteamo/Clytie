"""Verify a complete fixed-arrival request stream, never repair missing samples."""

import math


class SampleError(ValueError):
    pass


class ProcessExits:
    """Frozen child ordinals; only an observed wait can emit exit_verified."""

    def __init__(self, run):
        self.expected = set(run["observer_sequences"]) - {0}
        self.acquired, self.exited = {}, {}

    def add(self, row):
        if row["resource"] != "process":
            return
        identifier = row["id"]
        if identifier not in self.expected:
            raise SampleError("measurement_process_not_planned")
        if row["event"] == "acquired":
            if identifier in self.acquired:
                raise SampleError("measurement_process_duplicated")
            self.acquired[identifier] = row["at_s"]
        elif row["event"] == "exit_verified":
            if (
                identifier not in self.acquired
                or identifier in self.exited
                or row["at_s"] < self.acquired[identifier]
            ):
                raise SampleError("measurement_process_exit_unbound")
            self.exited[identifier] = row["at_s"]

    def complete(self):
        if set(self.exited) != self.expected:
            raise SampleError("measurement_process_exit_missing")


class RequestPlan:
    def __init__(self, run):
        self.load, self.arm = run["load"], run["arm"]
        self.count = 0
        self.start = self.last_finish = None
        self.backups = []

    def add(self, row):
        if row["kind"] == "backup":
            self.backups.append(row)
            if len(self.backups) > 1:
                raise SampleError("measurement_backup_duplicated")
        if row["kind"] != "business":
            return
        self.count += 1
        if row["id"] != self.count or self.count > self.load["request_count"]:
            raise SampleError("measurement_business_sequence_invalid")
        if self.start is None:
            self.start = row["scheduled_s"]
        expected = self.start + (self.count - 1) * self.load["interval_seconds"]
        # Only binary floating subtraction tolerance, not a timing allowance.
        if not math.isclose(row["scheduled_s"], expected, abs_tol=1e-7, rel_tol=0):
            raise SampleError("measurement_arrival_plan_changed")
        if (
            row["started_s"] + 1e-7 < row["scheduled_s"]
            or (self.last_finish is not None and row["started_s"] < self.last_finish)
            or row["outcome"] != "ok"
        ):
            raise SampleError("measurement_business_incomplete")
        self.last_finish = row["finished_s"]

    def complete(self):
        if self.count != self.load["request_count"] or self.start is None:
            raise SampleError("measurement_business_missing")
        needs_backup = self.arm in {"candidate", "original"}
        if len(self.backups) != int(needs_backup):
            raise SampleError("measurement_backup_missing_or_unexpected")
        for row in self.backups:
            if (
                row["outcome"] != "ok"
                or not math.isclose(
                    row["scheduled_s"],
                    self.start + self.load["backup_start_seconds"],
                    abs_tol=1e-7,
                    rel_tol=0,
                )
                or row["started_s"] + 1e-7 < row["scheduled_s"]
            ):
                raise SampleError("measurement_backup_plan_changed")
