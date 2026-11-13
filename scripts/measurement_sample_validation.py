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


class ResourceReleases:
    """V5 release stream, independent of native SQLite connection evidence.

    Owner thread 1 and snapshot threads starting at 2 share a namespace. Views
    bind to their snapshot thread, but references and slots have distinct keys.
    Use explicitly for completion judgments, never to reject failed raw archives.
    """

    TERMINALS = {
        "thread": "exit_verified",
        "process": "exit_verified",
        "view": "released",
        "reference": "released",
        "slot": "released",
    }

    def __init__(self, run):
        if run["format"] not in {5, 6}:
            raise SampleError("measurement_resource_format_invalid")
        self.processes = set(run["observer_sequences"]) - {0}
        self.required = {("process", number) for number in self.processes}
        if run["arm"] != "baseline":
            self.required.add(("thread", 1))
        self.states = {}

    def add(self, row):
        resource, identifier = row["resource"], row["id"]
        event, at = row["event"], row["at_s"]
        if (
            resource not in self.TERMINALS
            or type(identifier) is not int
            or identifier < 1
            or type(at) not in {int, float}
            or not math.isfinite(at)
            or at < 0
        ):
            raise SampleError("measurement_resource_identity_invalid")
        key = resource, identifier
        if resource == "process" and identifier not in self.processes:
            raise SampleError("measurement_process_not_planned")
        if resource == "view" and identifier < 2:
            raise SampleError("measurement_view_owner_collision")
        if event in {"unknown", "failed"}:
            raise SampleError("measurement_resource_release_unconfirmed")
        state = self.states.get(key)
        if event == "acquired":
            if state is not None:
                raise SampleError("measurement_resource_duplicated")
            if resource == "view":
                thread = self.states.get(("thread", identifier))
                if thread is None or thread["done"] or at < thread["last"]:
                    raise SampleError("measurement_view_thread_unbound")
            self.states[key] = dict(last=at, done=False, events={event})
            return
        if state is None or state["done"] or at < state["last"] or event in state["events"]:
            raise SampleError("measurement_resource_event_unbound")
        if event == self.TERMINALS[resource]:
            if resource == "thread":
                view = self.states.get(("view", identifier))
                if view is not None and (not view["done"] or at < view["last"]):
                    raise SampleError("measurement_view_release_unconfirmed")
            state["done"] = True
        elif event not in {"cancel_requested", "io_returned"}:
            raise SampleError("measurement_resource_terminal_invalid")
        state["last"] = at
        state["events"].add(event)

    def complete(self):
        if not self.required.issubset(self.states) or any(
            not state["done"] for state in self.states.values()
        ):
            raise SampleError("measurement_resource_release_missing")


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
