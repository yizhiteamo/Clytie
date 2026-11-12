import io

import pytest

from clytie.backup.io_budget import CHUNK, IoBudgetError
from clytie.backup.io_client import IoClient, heavy_io, io_read, io_scope, io_write


class Transport:
    def __init__(self):
        self.requests = []

    def __call__(self, request, *, cleanup=False):
        self.requests.append((dict(request), cleanup))
        return {}


def test_chunks_share_nested_stage_and_preserve_exact_bytes(tmp_path):
    transport = Transport()
    client = IoClient(transport)
    source, destination = tmp_path / "source", tmp_path / "destination"
    value = b"fictional bytes" * (CHUNK // 7)
    source.write_bytes(value)

    @heavy_io
    def nested():
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            block = io_read(incoming, len(value) + 1)
            assert io_read(incoming, 1) == b""
            io_write(outgoing, block)

    @heavy_io
    def outer():
        nested()

    with io_scope(client):
        outer()
    assert destination.read_bytes() == value
    actions = [request["action"] for request, _ in transport.requests]
    assert actions.count("acquire") == actions.count("release") == 1
    charges = [row for row, _ in transport.requests if row["action"] == "charge"]
    assert all(0 < row["size"] <= CHUNK for row in charges)
    assert [row["sequence"] for row in charges] == list(range(1, len(charges) + 1))
    for direction in ("read", "write"):
        assert sum(row["size"] for row in charges if row["direction"] == direction) == len(value)
    assert transport.requests[-1][1] is True


def test_exception_and_failed_acquisition_still_request_own_phase_cleanup():
    transport = Transport()

    @heavy_io
    def operation():
        raise ValueError("fictional failure")

    with io_scope(IoClient(transport)), pytest.raises(ValueError, match="fictional failure"):
        operation()
    assert [row["action"] for row, _ in transport.requests] == ["acquire", "release"]

    def uncertain(request, *, cleanup=False):
        transport(request, cleanup=cleanup)
        if not cleanup:
            raise OSError("fictional response lost")
        return {}

    with io_scope(IoClient(uncertain)), pytest.raises(OSError, match="fictional response lost"):
        operation()
    assert [row["action"] for row, _ in transport.requests] == ["acquire", "release"] * 2


def test_missing_stage_and_unbounded_read_are_rejected_before_io(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"fictional")
    client = IoClient(Transport())
    with io_scope(client), path.open("rb") as stream:
        with pytest.raises(IoBudgetError, match="backup_io_stage_required"):
            io_read(stream, 1)
        assert stream.tell() == 0

        @heavy_io
        def operation():
            io_read(stream, -1)

        with pytest.raises(IoBudgetError, match="backup_io_unbounded_read"):
            operation()
        assert stream.tell() == 0


async def test_event_loop_cannot_make_blocking_budget_request():
    transport = Transport()

    @heavy_io
    def operation():
        pytest.fail("must not enter synchronous heavy I/O on event loop")

    with (
        io_scope(IoClient(transport)),
        pytest.raises(IoBudgetError, match="backup_io_on_event_loop"),
    ):
        operation()
    assert not transport.requests


def test_outside_backup_scope_is_not_a_global_io_hook():
    target = io.BytesIO()
    io_write(target, b"fictional")
    target.seek(0)
    assert io_read(target, -1) == b"fictional"


def test_nested_identical_checks_run_once_per_checkpoint_and_restore_on_error():
    from clytie.backup.io_client import checkpoint, io_checks

    calls = []

    def first():
        calls.append("first")

    def second():
        calls.append("second")

    with io_checks(first):
        with pytest.raises(ValueError, match="fictional failure"):
            with io_checks(first), io_checks(second), io_checks(first):
                checkpoint()
                assert calls == ["first", "second"]
                raise ValueError("fictional failure")
        checkpoint()
        assert calls == ["first", "second", "first"]
    checkpoint()
    assert calls == ["first", "second", "first"]


def test_equal_but_distinct_checks_are_not_merged():
    from clytie.backup.io_client import checkpoint, io_checks

    calls = []

    class Check:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            pytest.fail("check identity must not invoke equality")

        def __call__(self):
            calls.append(self.name)

    with io_checks(Check("first")), io_checks(Check("second")):
        checkpoint()
    assert calls == ["first", "second"]
