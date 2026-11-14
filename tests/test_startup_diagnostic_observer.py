import errno
import json
from types import SimpleNamespace

import pytest
from tests.startup_diagnostic_plugin import Observation


def test_observation_only_whitelisted_fields_and_error_codes():
    times = iter([10.0, 10.125, 10.250])
    records = []
    observer = Observation(clock=lambda: next(times), sink=records.append)
    failure = PermissionError(errno.EACCES, "sensitive arbitrary message /fictional/path")
    observer.error("status_read", failure)
    observer.emit("child_state", child_state="exited", exit_code=0)
    assert records[0]["exception"] == "PermissionError"
    assert records[0]["errno"] == "0000:000d"
    assert records[0]["elapsed_seconds"] == 0.125
    text = json.dumps(records)
    assert "sensitive" not in text and "/fictional" not in text
    assert records[1]["exit_code"] == "0000:0000"
    assert all(
        set(row)
        == {
            "event",
            "stage",
            "elapsed_seconds",
            "exception",
            "errno",
            "winerror",
            "child_state",
            "exit_code",
        }
        for row in records
    )


def test_unknown_exception_name_and_payload_not_preserved():
    records = []
    observer = Observation(clock=lambda: 1, sink=records.append)

    class SensitiveClassName(Exception):
        pass

    observer.error("status_read", SensitiveClassName("never output"))
    assert records[0]["exception"] == "Other"
    assert "SensitiveClassName" not in json.dumps(records)


def test_trace_tracks_spawn_before_popen_has_assigned_args():
    records = []
    observer = Observation(clock=lambda: 1, sink=records.append)
    process = SimpleNamespace(returncode=None)
    frame = SimpleNamespace(
        f_globals={"__name__": "subprocess"},
        f_code=SimpleNamespace(co_name="__init__"),
        f_locals={"self": process, "args": ["python", "_supervisor", "fictional-path"]},
    )
    assert observer.trace(frame, "call", None) == observer.trace
    observer.trace(frame, "return", None)
    assert observer.children == [process]
    assert records[0]["stage"] == "child_spawned"
    assert "fictional-path" not in json.dumps(records)


def test_unknown_stage_refused():
    observer = Observation(clock=lambda: 1, sink=lambda _: None)
    with pytest.raises(ValueError, match="not_allowed"):
        observer.emit("arbitrary-content")
