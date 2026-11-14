"""固定大小的文件缓冲；小格式帧不各自创建一次管道往返。"""

from .io_budget import CHUNK
from .io_client import io_read, io_write


class ChunkReader:
    def __init__(self, stream):
        self.stream = stream
        self.pending = b""
        self.eof = False

    def read(self, count):
        if count < 0:
            raise ValueError("backup_io_unbounded_read")
        parts = []
        while count:
            if not self.pending:
                if self.eof:
                    break
                self.pending = io_read(self.stream, CHUNK)
                self.eof = len(self.pending) < CHUNK
                if not self.pending:
                    break
            take = min(count, len(self.pending))
            parts.append(self.pending[:take])
            self.pending = self.pending[take:]
            count -= take
        return b"".join(parts)


class ChunkWriter:
    def __init__(self, stream):
        self.stream = stream
        self.pending = bytearray()

    def write(self, value):
        view = memoryview(value)
        offset = 0
        while offset < len(view):
            size = min(CHUNK - len(self.pending), len(view) - offset)
            self.pending.extend(view[offset : offset + size])
            offset += size
            if len(self.pending) == CHUNK:
                self._flush_buffer()
        return len(value)

    def _flush_buffer(self):
        if self.pending:
            io_write(self.stream, self.pending)
            self.pending.clear()

    def flush(self):
        self._flush_buffer()
        self.stream.flush()

    def fileno(self):
        return self.stream.fileno()

    def close(self):
        try:
            self.flush()
        finally:
            self.stream.close()

    def abort(self):
        # No destructor can flush cancelled bytes after the budget scope ends.
        self.pending.clear()
        self.stream.close()
