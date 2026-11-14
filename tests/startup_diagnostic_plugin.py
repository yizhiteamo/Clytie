"""Opt-in test-side observation; never changes startup calls, budgets or protocol."""

import json
import os
import sys

import pytest

from clytie.clock import SystemClock


class Observation:
    def __init__(self, *, clock=None, sink=None):
        self.clock = clock or SystemClock().monotonic
        self.started = self.clock()
        self.sink = sink or (
            lambda row: print("CLYTIE_STARTUP_DIAGNOSTIC " + json.dumps(row), flush=True)
        )
        self.children = []
        self.states = {}
        self.count = 0
        self.overflow = False

    @staticmethod
    def code(value):
        if type(value) is not int or not -(2**31) <= value < 2**32:
            return None
        text = format(value & (2**32 - 1), "08x")
        return text[:4] + ":" + text[4:]

    def emit(
        self,
        stage,
        *,
        event="observed",
        exception=None,
        errno=None,
        winerror=None,
        child_state=None,
        exit_code=None,
    ):
        if stage not in {
            "await_start",
            "http_status_request",
            "status_read",
            "state_file_read",
            "confirmation_pipe",
            "child_spawned",
            "child_poll",
            "test_begin",
            "test_end_child",
            "test_end",
            "child_state",
        } or event not in {"observed", "exception", "enter", "return"}:
            raise ValueError("diagnostic_field_not_allowed")
        if self.count >= 1024:
            self.overflow = True
            return
        self.count += 1
        self.sink(
            dict(
                event=event,
                stage=stage,
                elapsed_seconds=round(self.clock() - self.started, 3),
                exception=exception,
                errno=self.code(errno),
                winerror=self.code(winerror),
                child_state=child_state,
                exit_code=self.code(exit_code),
            )
        )

    def error(self, stage, exc):
        name = type(exc).__name__
        if name not in {
            "PermissionError",
            "BrokenPipeError",
            "OSError",
            "TimeoutError",
            "ConnectionResetError",
            "NotRunning",
        }:
            name = "Other"
        self.emit(
            stage,
            event="exception",
            exception=name,
            errno=getattr(exc, "errno", None),
            winerror=getattr(exc, "winerror", None),
        )

    def trace(self, frame, event, arg):
        module = frame.f_globals.get("__name__")
        name = frame.f_code.co_name
        stages = {
            ("clytie.api.client", "_await_start"): "await_start",
            ("clytie.api.client", "request"): "http_status_request",
            ("clytie.supervisor.entry", "status"): "status_read",
            ("clytie.supervisor.common", "read"): "state_file_read",
            ("clytie.supervisor.processes", "frame"): "confirmation_pipe",
        }
        stage = stages.get((module, name))
        if module == "subprocess" and name in {"__init__", "poll"}:
            process = frame.f_locals.get("self")
            args = getattr(process, "args", None) or frame.f_locals.get("args")
            if isinstance(args, (list, tuple)) and "_supervisor" in args:
                if event == "return" and name == "__init__":
                    self.children.append(process)
                    self.emit("child_spawned", child_state="running")
                elif event == "return" and name == "poll":
                    value = getattr(process, "returncode", None)
                    key = id(process)
                    if key not in self.states or self.states[key] != value:
                        self.states[key] = value
                        self.emit(
                            "child_poll",
                            child_state="running" if value is None else "exited",
                            exit_code=value,
                        )
                return self.trace
        if stage is None:
            return None
        if (
            stage == "confirmation_pipe"
            and frame.f_locals.get("message", {}).get("type") != "startup_confirmed"
        ):
            return None
        if event == "call":
            self.emit(stage, event="enter")
        elif event == "exception":
            exc = arg[1]
            if isinstance(exc, OSError) or type(exc).__name__ == "NotRunning":
                self.error(stage, exc)
        elif event == "return":
            self.emit(stage, event="return")
        return self.trace


@pytest.fixture(autouse=True)
def observe_startup():
    if os.environ.get("CLYTIE_STARTUP_DIAGNOSTIC") != "1":
        yield
        return
    observer = Observation()
    previous = sys.gettrace()
    observer.emit("test_begin")
    sys.settrace(observer.trace)
    try:
        yield
    finally:
        sys.settrace(previous)
        for process in observer.children:
            code = process.poll()
            observer.emit(
                "test_end_child",
                child_state="running" if code is None else "exited",
                exit_code=code,
            )
        observer.emit("test_end")
        if observer.overflow:
            pytest.fail("startup_diagnostic_incomplete_event_limit")
