"""Atomically checkpoint feed validators and the events decoded from that response."""

import asyncio
import json
import sqlite3


async def run_local(fn, *args):
    # A stopped tick must relinquish its database transaction and its in-memory indices
    # before a reloaded plugin can observe the checkpoint.
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class Cache:
    def __init__(self, directory):
        self.path = directory / "feeds.sqlite3"

    def load(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, body TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS sources (key TEXT PRIMARY KEY, body TEXT)")
            return tuple(
                {
                    key: json.loads(body)
                    for key, body in db.execute("SELECT key, body FROM " + table)
                }
                for table in ("events", "sources")
            )
        finally:
            db.close()

    def save(self, events, sources, removed_events, removed_sources):
        db = sqlite3.connect(self.path)
        try:
            with db:
                for table, rows, removed in (
                    ("events", events, removed_events),
                    ("sources", sources, removed_sources),
                ):
                    db.executemany(
                        "DELETE FROM " + table + " WHERE key = ?", ((k,) for k in removed)
                    )
                    db.executemany(
                        "INSERT OR REPLACE INTO " + table + " VALUES (?, ?)",
                        ((key, json.dumps(body, ensure_ascii=False)) for key, body in rows.items()),
                    )
        finally:
            db.close()
