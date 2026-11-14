"""实际 SQLite backup 批次：固定视图不变，下一批前取得双向逻辑预算。"""

import sqlite3

from .coordination import CaptureError
from .io_budget import CHUNK
from .io_client import current_client, heavy_io, io_checks


@heavy_io
def copy_database(source, target, *, pages, check):
    client = current_client()
    if client is None:
        source.backup(target, pages=pages, progress=lambda *_: check(), sleep=0)
        return
    page_size = source.execute("PRAGMA page_size").fetchone()[0]
    page_count = source.execute("PRAGMA page_count").fetchone()[0]
    batch = min(pages, max(1, CHUNK // page_size))
    if page_size > CHUNK or pages < 1:
        raise CaptureError("snapshot_unsupported")

    def reserve(remaining):
        check()
        size = min(batch, remaining) * page_size
        if size:
            client.charge("read", size)
            client.charge("write", size)
        check()

    previous = page_count

    def progress(status, remaining, total):
        nonlocal previous
        check()
        if status not in {sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE}:
            raise CaptureError("snapshot_inconsistent")
        if total != page_count or remaining >= previous or remaining < 0:
            raise CaptureError("snapshot_inconsistent")
        previous = remaining
        if remaining:
            reserve(remaining)

    with io_checks(check):
        reserve(page_count)
        source.backup(target, pages=batch, progress=progress, sleep=0)
        check()
