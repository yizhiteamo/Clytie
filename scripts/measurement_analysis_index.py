"""Offline, disk-indexed evidence predicates. Never a mechanism acceptance gate.

Call only AFTER measurement, with a fresh analysis directory. The original
files are opened read-only, hashed byte-for-byte and never rewritten. JSONL is
read one capped line at a time; retained analysis is schema-validated metadata
(no product content). SQLite page cache is bounded and sorts spill to disk.
"""

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import MutableMapping, MutableSet, Set
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from . import measurement_evidence as evidence
else:
    import measurement_evidence as evidence


class Invalid(ValueError):
    pass


class Missing(Invalid):
    """Missing proof is unverified, not evidence of a product violation."""


def require(condition, code):
    if not condition:
        raise Invalid(code)


def decode(raw):
    # Existing duplicate-key and non-finite-number rejection, never json.loads alone.
    return evidence._json(raw)


def stat_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def safe_stream(path):
    path = evidence._safe_path(path)
    before = path.stat()
    require(
        stat.S_ISREG(before.st_mode) and before.st_size <= evidence.MAX_FILE, "invalid_source_file"
    )
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        require(
            stat_identity(before) == stat_identity(opened) and opened.st_nlink == 1,
            "source_changed_or_linked",
        )
        yield stream
        after = os.fstat(stream.fileno())
        final = evidence._safe_path(path).stat()
        require(
            stat_identity(before) == stat_identity(after) == stat_identity(final)
            and after.st_nlink == final.st_nlink == 1,
            "source_changed_or_linked",
        )


def lines(path, digest):
    total = 0
    with safe_stream(path) as stream:
        while True:
            raw = stream.readline(evidence.MAX_LINE + 1)
            if not raw:
                return
            digest.update(raw)
            total += len(raw)
            require(total <= evidence.MAX_FILE, "file_limit")
            require(len(raw) <= evidence.MAX_LINE, "line_limit")
            require(raw.strip(), "blank_line")
            yield decode(raw)


def identity_matches(run, expected):
    evidence.validate_run(expected)
    immutable = set(evidence.V5_FIELDS) - {"result", "metrics"}
    require(all(run[key] == expected[key] for key in immutable), "expected_identity_mismatch")
    require(
        all(run["metrics"][key] == expected["metrics"][key] for key in ("logical", "ledger_rows")),
        "expected_workload_mismatch",
    )
    return sorted(immutable)


def analysis_target(source, target):
    """Validate the new sink before any mkdir, including path aliases.

    Inspect only the requested target's ancestry. A nonexistent tail is allowed;
    every existing ancestor must pass the same link/reparse check as sources.
    """
    target = Path(target).absolute()
    ancestor = target
    while True:
        try:
            ancestor.lstat()
            break
        except FileNotFoundError:
            require(ancestor != ancestor.parent, "analysis_target_ancestor_missing")
            ancestor = ancestor.parent
    evidence._safe_path(ancestor)
    resolved_source = evidence._safe_path(source).resolve()
    resolved_target = target.resolve(strict=False)
    require(
        resolved_target != resolved_source and resolved_source not in resolved_target.parents,
        "analysis_target_inside_source",
    )
    return resolved_target


def encode_key(key):
    return json.dumps(key, separators=(",", ":"))


class DiskSet(MutableSet):
    """Only small keys in Python; membership and retention stay on analysis disk."""

    def __init__(self, db, namespace):
        self.db, self.namespace = db, namespace
        db.execute("CREATE TABLE IF NOT EXISTS set_items(ns TEXT,k TEXT,PRIMARY KEY(ns,k))")

    def __contains__(self, value):
        return (
            self.db.execute(
                "SELECT 1 FROM set_items WHERE ns=? AND k=?", (self.namespace, encode_key(value))
            ).fetchone()
            is not None
        )

    def __len__(self):
        return self.db.execute(
            "SELECT count(*) FROM set_items WHERE ns=?", (self.namespace,)
        ).fetchone()[0]

    def __iter__(self):
        for (key,) in self.db.execute("SELECT k FROM set_items WHERE ns=?", (self.namespace,)):
            value = json.loads(key)
            yield tuple(value) if isinstance(value, list) else value

    def add(self, value):
        self.db.execute(
            "INSERT OR IGNORE INTO set_items VALUES(?,?)", (self.namespace, encode_key(value))
        )

    def discard(self, value):
        self.db.execute(
            "DELETE FROM set_items WHERE ns=? AND k=?", (self.namespace, encode_key(value))
        )

    def __or__(self, other):
        return SetUnion(self, other)


class SetUnion(Set):
    def __init__(self, left, right):
        self.left, self.right = left, right

    def __contains__(self, value):
        return value in self.left or value in self.right

    def __iter__(self):
        yield from self.left
        for value in self.right:
            if value not in self.left:
                yield value

    def __len__(self):
        return sum(1 for _ in self)


class DiskMap(MutableMapping):
    """At most the few entries touched by one validator.add are cached.

    Call flush after every add. Iterating values never populates the cache.
    Only validator metadata is serialized; no pickle or executable decoding.
    """

    def __init__(self, db, namespace):
        self.db, self.namespace, self.cache = db, namespace, {}
        db.execute("CREATE TABLE IF NOT EXISTS map_items(ns TEXT,k TEXT,v TEXT,PRIMARY KEY(ns,k))")

    @staticmethod
    def dump(value):
        return json.dumps(value, default=lambda item: {"__analysis_set__": sorted(item)})

    @staticmethod
    def load(raw):
        return json.loads(
            raw,
            object_hook=lambda value: (
                set(value["__analysis_set__"]) if set(value) == {"__analysis_set__"} else value
            ),
        )

    def __getitem__(self, key):
        encoded = encode_key(key)
        if encoded in self.cache:
            return self.cache[encoded]
        row = self.db.execute(
            "SELECT v FROM map_items WHERE ns=? AND k=?", (self.namespace, encoded)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        require(len(self.cache) < 16, "analysis_map_cache_bound")
        self.cache[encoded] = self.load(row[0])
        return self.cache[encoded]

    def __setitem__(self, key, value):
        encoded = encode_key(key)
        self.db.execute(
            "INSERT OR REPLACE INTO map_items VALUES(?,?,?)",
            (self.namespace, encoded, self.dump(value)),
        )
        if encoded in self.cache:
            self.cache[encoded] = value

    def __delitem__(self, key):
        encoded = encode_key(key)
        if key not in self:
            raise KeyError(key)
        self.cache.pop(encoded, None)
        self.db.execute("DELETE FROM map_items WHERE ns=? AND k=?", (self.namespace, encoded))

    def __contains__(self, key):
        return (
            self.db.execute(
                "SELECT 1 FROM map_items WHERE ns=? AND k=?", (self.namespace, encode_key(key))
            ).fetchone()
            is not None
        )

    def __iter__(self):
        for (key,) in self.db.execute("SELECT k FROM map_items WHERE ns=?", (self.namespace,)):
            value = json.loads(key)
            yield tuple(value) if isinstance(value, list) else value

    def __len__(self):
        return self.db.execute(
            "SELECT count(*) FROM map_items WHERE ns=?", (self.namespace,)
        ).fetchone()[0]

    def values(self):
        self.flush()
        for (raw,) in self.db.execute("SELECT v FROM map_items WHERE ns=?", (self.namespace,)):
            yield self.load(raw)

    def flush(self):
        for key, value in self.cache.items():
            self.db.execute(
                "UPDATE map_items SET v=? WHERE ns=? AND k=?",
                (self.dump(value), self.namespace, key),
            )
        self.cache.clear()


class RequiredKeys(set):
    def issubset(self, other):
        # builtin set.issubset(mapping) would materialize all disk keys.
        return all(key in other for key in self)


class Index:
    def __init__(self, source, target, expected):
        self.db = None
        try:
            self._initialize(source, target, expected)
        except BaseException:
            if self.db is not None:
                self.db.close()
            raise

    def _initialize(self, source, target, expected):
        self.source = evidence._safe_path(source)
        require(
            self.source.is_dir() and {p.name for p in self.source.iterdir()} == set(evidence.FILES),
            "unexpected_file_set",
        )
        source_stat = stat_identity(self.source.stat())
        original_stats = {
            name: stat_identity(evidence._safe_path(self.source / name).stat())
            for name in evidence.FILES
        }
        target = analysis_target(self.source, target)
        target.mkdir(parents=True, exist_ok=False)
        self.db = sqlite3.connect(target / "analysis.sqlite")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.executescript("""
            CREATE TABLE rows(file TEXT, n INTEGER, id INTEGER, phase TEXT,
                event TEXT, t REAL, stage TEXT, budget INTEGER, transfer INTEGER,
                validation INTEGER, connection INTEGER, j TEXT);
            CREATE UNIQUE INDEX storage_ids ON rows(id)
                WHERE file='storage-phases.jsonl';
            CREATE INDEX events ON rows(phase,event);
            CREATE INDEX stages ON rows(stage,phase,event,t);
            CREATE INDEX validations ON rows(validation,phase,t);
            CREATE INDEX transfers ON rows(transfer,phase,t);
        """)
        self.hashes, self.counts = {}, {}
        path = self.source / "run-manifest.json"
        with safe_stream(path) as stream:
            raw = stream.read(evidence.MAX_LINE + 1)
        require(len(raw) <= evidence.MAX_LINE, "manifest_limit")
        self.hashes[path.name] = hashlib.sha256(raw).hexdigest()
        self.run = decode(raw)
        evidence.validate_run(self.run)
        require(self.run["format"] == 6 and self.run["arm"] == "candidate", "candidate_v6_required")
        self.expected_fields = identity_matches(self.run, expected)
        for name in evidence.FILES:
            if name == "run-manifest.json":
                continue
            digest, count = hashlib.sha256(), 0
            for count, row in enumerate(lines(self.source / name, digest), 1):
                evidence.validate_record(name, row, format_version=6)
                if name == "storage-phases.jsonl":
                    sequence = (row["id"] >> 40) - 1
                    require(sequence in self.run["observer_sequences"], "unplanned_process")
                    if "process_sequence" in row:
                        require(row["process_sequence"] == sequence, "process_namespace_mismatch")
                    for field in ("validation_id", "connection_id", "transfer_id", "budget_id"):
                        value = row.get(field)
                        if value:
                            require((value >> 40) - 1 == sequence, "object_namespace_mismatch")
                # Only schema-validated evidence metadata, never product content.
                self.db.execute(
                    "INSERT INTO rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        name,
                        count,
                        row.get("id"),
                        row.get("phase"),
                        row.get("event"),
                        row.get("at_s"),
                        row.get("stage_id"),
                        row.get("budget_id"),
                        row.get("transfer_id"),
                        row.get("validation_id"),
                        row.get("connection_id"),
                        json.dumps(row, separators=(",", ":")),
                    ),
                )
            self.hashes[name], self.counts[name] = digest.hexdigest(), count
        require(
            {p.name for p in self.source.iterdir()} == set(evidence.FILES)
            and stat_identity(evidence._safe_path(self.source).stat()) == source_stat,
            "source_directory_changed",
        )
        require(
            all(
                stat_identity(evidence._safe_path(self.source / name).stat()) == identity
                for name, identity in original_stats.items()
            ),
            "source_changed_between_files",
        )
        self.db.commit()

    def rows(self, where="1", args=(), order="n"):
        for (raw,) in self.db.execute(f"SELECT j FROM rows WHERE {where} ORDER BY {order}", args):
            yield json.loads(raw)

    def one(self, where, args=()):
        cursor = self.rows(where, args)
        row = next(cursor, None)
        if row is None:
            raise Missing("required_event_missing")
        require(next(cursor, None) is None, "required_event_duplicated")
        return row

    def count(self, where, args=()):
        return self.db.execute(f"SELECT count(*) FROM rows WHERE {where}", args).fetchone()[0]
