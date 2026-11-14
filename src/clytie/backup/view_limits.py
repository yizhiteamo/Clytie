"""固定读视图的有界保留；WAL 代次变化不能抵销此前已观察的增长。"""

import os
import stat
from pathlib import Path

from .coordination import CaptureError

MAX_VIEW_SECONDS = 5 * 60
MAX_WAL_GROWTH = 256 * 1024 * 1024


def _wal_sample(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise CaptureError("snapshot_wal_unconfirmed")
    with path.open("rb", buffering=0) as stream:
        before = os.fstat(stream.fileno())
        header = stream.read(32)
        after = os.fstat(stream.fileno())
        stream.seek(0)
        repeated = stream.read(32)
    current = path.lstat()
    identity = (before.st_dev, before.st_ino)
    if (
        any((value.st_dev, value.st_ino) != identity for value in (info, after, current))
        or header != repeated
        or after.st_size < before.st_size
    ):
        raise CaptureError("snapshot_wal_unconfirmed")
    size = after.st_size
    if size and (
        len(header) != 32 or int.from_bytes(header[:4], "big") not in {0x377F0682, 0x377F0683}
    ):
        raise CaptureError("snapshot_wal_unconfirmed")
    # WAL header includes checkpoint sequence, page size and both generation salts.
    return identity + (header[:24],), size


class WalGrowth:
    def __init__(self, path):
        self.path = Path(path)
        self.last = _wal_sample(self.path)
        self.additional = 0

    def sample(self):
        current = _wal_sample(self.path)
        if current is None:
            if self.last is not None and self.last[1] > 0:
                raise CaptureError("snapshot_wal_unconfirmed")
            return self.additional
        identity, size = current
        if self.last is None or identity != self.last[0] or size < self.last[1]:
            self.additional += size
        else:
            self.additional += size - self.last[1]
        self.last = current
        return self.additional


class ViewLimits:
    def __init__(self, paths, clock, *, pinned_at):
        self.clock, self.pinned_at = clock, pinned_at
        self.wals = {name: WalGrowth(str(path) + "-wal") for name, path in paths.items()}
        self.peaks = {name: 0 for name in paths}

    def check(self):
        if self.clock.monotonic() - self.pinned_at >= MAX_VIEW_SECONDS:
            raise CaptureError("snapshot_view_expired")
        for name, growth in self.wals.items():
            self.peaks[name] = growth.sample()
            if self.peaks[name] >= MAX_WAL_GROWTH:
                raise CaptureError("snapshot_wal_limit")
