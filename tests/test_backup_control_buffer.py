import io
import json
import os
import threading

from clytie.backup.worker import Controls


class CountedRaw(io.RawIOBase):
    def __init__(self, data):
        self.source = io.BytesIO(data)
        self.calls = 0

    def readable(self):
        return True

    def readinto(self, target):
        self.calls += 1
        return self.source.readinto(target)


def test_worker_control_pipe_does_not_read_each_protocol_byte_separately():
    value = json.dumps({"type": "backup_io_reply", "id": "fictional", "result": {}}).encode()
    source = CountedRaw(b"start\n" + (value + b"\n") * 100 + b"cancel\n")
    control = Controls(None, None, emit=lambda _: None)
    control._read(source)
    assert control.received and control.started.is_set() and control.cancelled.is_set()
    assert source.calls <= 3


def test_raw_pipe_delivers_complete_lines_without_waiting_for_buffer_or_eof():
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb", buffering=0) as source,
        os.fdopen(write_fd, "wb", buffering=0) as destination,
    ):
        control = Controls(None, None, emit=lambda _: None)
        reading = threading.Thread(target=control._read, args=(source,))
        reading.start()
        try:
            destination.write(b"sta")
            assert not control.started.is_set()
            destination.write(b"rt\n")
            assert control.started.wait(2)
            assert control.received and not control.cancelled.is_set()
            destination.write(b"cancel\n")
            assert control.cancelled.wait(2)
        finally:
            destination.close()
            reading.join(2)
        assert not reading.is_alive()


def test_raw_pipe_eof_wakes_start_and_cancellation():
    source = CountedRaw(b"")
    control = Controls(None, None, emit=lambda _: None)
    control._read(source)
    assert not control.received
    assert control.started.is_set() and control.cancelled.is_set()
