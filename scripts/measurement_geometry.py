"""Construct approved legal scales to inventory bytes before freezing a load plan.

No request timer, backup operation, performance statistic or acceptance claim.
All directories are new fictional fixtures. Old benchmark files are not read.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

try:
    from . import measurement_fixture as fixture
except ImportError:
    import measurement_fixture as fixture

from clytie.backup.coordination import run_sync
from clytie.backup.restore import MEMORY_TABLES


async def construct(directory, scales, *, seed=73):
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=False)
    results = {}
    for scale in scales:
        owner = None
        try:
            owner, metrics = await fixture.setup(directory / scale, scale, "candidate", seed)

            def verify_database():
                db = owner.store.db
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("fixture_database_invalid")
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if tables != set(MEMORY_TABLES):
                    raise ValueError("fixture_schema_invalid")
                if db.execute(
                    "SELECT count(*) FROM records r LEFT JOIN revisions v "
                    "ON r.id=v.record_id AND v.revision=1 WHERE v.record_id IS NULL"
                ).fetchone()[0]:
                    raise ValueError("fixture_revision_missing")

            await run_sync(owner.store, verify_database)
            results[scale] = metrics
        finally:
            if owner is not None:
                await fixture.close(owner)
    result = dict(
        format=1,
        source="generated_fictional_only",
        purpose="geometry_only_not_performance",
        fixture_version=fixture.FIXTURE_VERSION,
        fixture_sha256=hashlib.sha256(Path(fixture.__file__).read_bytes()).hexdigest(),
        seed=seed,
        scales=results,
    )
    (directory / "geometry.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(construct(args.directory, tuple(fixture.SCALES)))
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
